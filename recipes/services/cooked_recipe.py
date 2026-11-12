"""
« J'ai cuisiné » crée toujours une recette (ckd2 lot 2).

`POST /posts/cooked/` sans `recipe_id` : la recette du plat est posée tout de suite, puis complétée
en arrière-plan si besoin.

- nom seul → recette « coquille » au nom du plat (réutilise sa propre recette du même nom : un plat
  cuisiné deux fois reste une seule recette) ;
- `recipe_text` (« Ta recette » écrite) → recette au nom du plat dont le texte brut est l'unique étape,
  puis mise en forme IA (tâche `process_cooked_recipe_text`) ; échec ou plafond : le texte reste ;
- `import_url` (lien collé) → recette déjà importée depuis ce lien réutilisée, sinon recette
  « Recette de <domaine> » (ou le nom donné) qui garde le lien, puis import en tâche
  (`process_cooked_recipe_import`) : renommée seulement si l'utilisateur n'a pas nommé le plat.

Import au collage (ckd2 lot 2 bis) : `POST /recipes/import-link/` pose la recette dès que le lien est
collé dans « La recette » (privée tant que le plat n'est pas posté, réutilisée si le lien est déjà
importé) et lance l'import (`JOB_LINK`) ; l'app suit `/recipes/formalize/status/<id>/` et envoie
`recipe_id` à Valider. `log_cooked` la rend publique (et renvoie l'import en cours en `recipe_job`) ;
Annuler la rend privée ; ✕ / autre lien → `DELETE /recipes/import-link/<recipe_id>/` la supprime si
elle n'a jamais été postée.

Mise en forme et import : ouverts à tous, plafonnés par mois sauf premium (`ai_recipe_quota`).
Les anciens posts sans recette restent tels quels.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.db.models import Q

from recipes.models import Recipe, RecipeImportRequest, Step

from . import ai_recipe_quota

RECIPE_TEXT_MAX_LENGTH = 5000
IMPORT_URL_MAX_LENGTH = 2000
RECIPE_TITLE_MAX_LENGTH = 200
WRITTEN_RECIPE_DEFAULT_TITLE = 'Ta recette'

JOB_FORMAT = 'cooked_format'
JOB_IMPORT = 'cooked_import'
JOB_LINK = 'cooked_link'  # import lancé au collage, avant le post (lot 2 bis)
_JOB_KIND = {JOB_FORMAT: 'format', JOB_IMPORT: 'import', JOB_LINK: 'import'}
_RUNNING = ('pending', 'processing')


@dataclass
class CookedRecipePlan:
    """Ce que `log_cooked` doit faire de la recette quand aucune `recipe_id` n'est donnée."""

    recipe_text: str = ''
    import_url: str = ''
    host: str = ''


def link_host(url: str) -> str:
    """« marmiton.org » pour https://www.marmiton.org/… (même règle que l'app)."""
    host = (urlparse(url).hostname or '').lower()
    return host[4:] if host.startswith('www.') else host


def default_link_title(host: str) -> str:
    return f'Recette de {host}' if host else WRITTEN_RECIPE_DEFAULT_TITLE


def clean_recipe_text(value) -> str:
    return str(value or '').strip()[:RECIPE_TEXT_MAX_LENGTH]


def clean_import_url(value) -> str | None:
    """URL http(s) valide, sinon None (l'appelant répond 400)."""
    url = str(value or '').strip()
    if not url:
        return ''
    if len(url) > IMPORT_URL_MAX_LENGTH:
        return None
    try:
        URLValidator(schemes=['http', 'https'])(url)
    except ValidationError:
        return None
    return url


def is_default_link_label(label: str, host: str) -> bool:
    """L'app envoie le nom par défaut « Recette de marmiton.org » : ce n'est pas un nom choisi."""
    return bool(label) and label.strip().casefold() == default_link_title(host).casefold()


def find_imported_recipe(user, url: str) -> Recipe | None:
    """Recette déjà importée depuis ce lien (publique ou à soi), comme `POST /recipes/import_from_url/`."""
    from recipes.utils import canonicalize_import_url

    canonical = canonicalize_import_url(url)
    candidates = {canonical, url, canonical.rstrip('/'), canonical + '/'}
    return (
        Recipe.objects.filter(Q(is_public=True) | Q(created_by=user))
        .filter(import_source_url__in=list(candidates))
        .order_by('-created_at')
        .first()
    )


def find_own_recipe_named(user, title: str) -> Recipe | None:
    title = (title or '').strip()[:RECIPE_TITLE_MAX_LENGTH]
    if not title:
        return None
    return Recipe.objects.filter(created_by=user, title__iexact=title).order_by('-created_at').first()


def _storable_source_url(url: str) -> str | None:
    from recipes.utils import canonicalize_import_url

    canonical = canonicalize_import_url(url)
    max_length = Recipe._meta.get_field('import_source_url').max_length
    return canonical if len(canonical) <= max_length else None


def create_cooked_recipe(user, *, title: str, plan: CookedRecipePlan, is_public: bool = True) -> Recipe:
    """Recette posée tout de suite (dans la transaction de `log_cooked`, ou au collage : privée)."""
    recipe = Recipe.objects.create(
        title=(title or WRITTEN_RECIPE_DEFAULT_TITLE)[:RECIPE_TITLE_MAX_LENGTH],
        prep_time=0,
        cook_time=0,
        created_by=user,
        is_public=is_public,  # comme toute recette créée dans l'app (amis qui voient le post)
        source_type='imported' if plan.import_url else 'user_created',
        import_source_url=_storable_source_url(plan.import_url) if plan.import_url else None,
    )
    if plan.recipe_text:
        Step.objects.create(recipe=recipe, order=1, instruction=plan.recipe_text)
    return recipe


def enqueue_cooked_recipe_job(
    user,
    recipe: Recipe,
    *,
    plan: CookedRecipePlan,
    keep_title: bool,
    job_type: str | None = None,
    extra_payload: dict | None = None,
) -> dict | None:
    """
    Mise en forme / import en arrière-plan de la recette posée par « J'ai cuisiné ».
    Renvoie le résumé `recipe_job` de la réponse (None si rien à faire).
    Doit être appelé dans la transaction : la tâche part au commit.
    """
    if not (plan.recipe_text or plan.import_url):
        return None
    job_type = job_type or (JOB_IMPORT if plan.import_url else JOB_FORMAT)
    kind = _JOB_KIND[job_type]
    if not ai_recipe_quota.has_quota(user):
        return {
            'request_id': None,
            'kind': kind,
            'status': 'skipped',
            'code': ai_recipe_quota.QUOTA_EXCEEDED_CODE,
        }

    from core.celery_dispatch import publish
    from recipes.tasks import process_cooked_recipe_import, process_cooked_recipe_text

    payload = {
        'quota': ai_recipe_quota.QUOTA_KEY,
        'job_type': job_type,
        'target_recipe_id': recipe.id,
        'keep_title': keep_title,
        'title': recipe.title,
        **(extra_payload or {}),
    }
    if plan.import_url:
        payload.update({'url': plan.import_url, 'source_type': 'imported'})
    else:
        payload.update({'raw_text': plan.recipe_text, 'source_type': 'user_created'})
    import_request = RecipeImportRequest.objects.create(
        user=user, payload=payload, recipe=recipe, status=RecipeImportRequest.STATUS_PENDING
    )
    task = process_cooked_recipe_import if plan.import_url else process_cooked_recipe_text
    request_id = str(import_request.id)

    def dispatch():
        if not publish(task, args=[request_id]):
            # Broker injoignable : la recette reste telle quelle (texte brut / nom par défaut), non décomptée.
            RecipeImportRequest.objects.filter(id=request_id).update(
                status=RecipeImportRequest.STATUS_ERROR, error_message='queue_unavailable'
            )

    transaction.on_commit(dispatch)
    return {'request_id': str(import_request.id), 'kind': kind, 'status': import_request.status}


# ---------------------------------------------------------------------------
# Import au collage (lot 2 bis) : la recette avant le post
# ---------------------------------------------------------------------------
class LinkImportError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


def _created_by_link(recipe_id):
    """Demandes de la recette posée au collage (marquées à sa création, même au-delà du plafond)."""
    return RecipeImportRequest.objects.filter(payload__target_recipe_id=recipe_id, payload__created_by_link=True)


def is_unposted_link_recipe(user, recipe: Recipe | None) -> bool:
    """Recette posée au collage, jamais postée (ou dépostée par Annuler) : privée, à soi."""
    return bool(
        recipe is not None
        and recipe.created_by_id == getattr(user, 'id', None)
        and not recipe.is_public
        and _created_by_link(recipe.id).exists()
    )


def running_link_job(user, recipe: Recipe | None) -> dict | None:
    """Import de lien encore en cours pour cette recette → `recipe_job` (import-link, `log_cooked`)."""
    if recipe is None or recipe.created_by_id != getattr(user, 'id', None):
        return None
    req = (
        RecipeImportRequest.objects.filter(
            payload__target_recipe_id=recipe.id, payload__job_type__in=[JOB_LINK, JOB_IMPORT], status__in=_RUNNING
        )
        .order_by('-created_at')
        .first()
    )
    if req is None:
        return None
    return {'request_id': str(req.id), 'kind': 'import', 'status': req.status}


def _has_content(recipe: Recipe) -> bool:
    return recipe.steps.exists() or recipe.recipe_ingredients.exists()


def _link_response(recipe: Recipe, host: str, job: dict | None, *, already_imported: bool) -> dict:
    job = job or {'request_id': None, 'status': 'success'}
    out = {
        'recipe_id': recipe.id,
        'request_id': job.get('request_id'),
        'status': job['status'],
        'host': host,
        'already_imported': already_imported,
        'recipe': {'id': recipe.id, 'title': recipe.title, 'image_url': recipe.image_url},
    }
    if job.get('code'):
        out['code'] = job['code']
    return out


def start_link_import(user, raw_url) -> tuple[dict, bool]:
    """
    Lien collé dans « La recette » : recette posée tout de suite et import lancé (sans droit `ai`,
    compté dans le plafond sauf premium). Renvoie (réponse, créée ?).

    - lien déjà importé (recette publique ou à soi) : réutilisée, son import en cours renvoyé s'il y
      en a un ; sa propre recette restée vide (import raté, plafond) est réimportée ;
    - sinon recette privée « Recette de <domaine> » qui garde le lien ; au-delà du plafond : gardée
      telle quelle, `status = skipped`, `code = ai_quota_exceeded`.
    """
    url = clean_import_url(raw_url)
    if not url:
        raise LinkImportError('url must be a valid http(s) URL', 'invalid_import_url')
    host = link_host(url)
    plan = CookedRecipePlan(import_url=url, host=host)

    existing = find_imported_recipe(user, url)
    if existing is not None:
        job = running_link_job(user, existing)
        if job is None and existing.created_by_id == user.id and not _has_content(existing):
            with transaction.atomic():
                job = enqueue_cooked_recipe_job(user, existing, plan=plan, keep_title=False, job_type=JOB_LINK)
        return _link_response(existing, host, job, already_imported=job is None), False

    marker = {'created_by_link': True}
    with transaction.atomic():
        recipe = create_cooked_recipe(user, title=default_link_title(host), plan=plan, is_public=False)
        job = enqueue_cooked_recipe_job(
            user, recipe, plan=plan, keep_title=False, job_type=JOB_LINK, extra_payload=marker
        )
        if job and job['status'] == 'skipped':
            # Au-delà du plafond : rien ne part vers le modèle, mais la recette reste reconnue comme
            # « du collage » (publiée au post, supprimable au ✕). Pas de clé `quota` : non décomptée.
            RecipeImportRequest.objects.create(
                user=user,
                recipe=recipe,
                status=RecipeImportRequest.STATUS_ERROR,
                error_message=ai_recipe_quota.QUOTA_EXCEEDED_CODE,
                payload={'job_type': JOB_LINK, 'target_recipe_id': recipe.id, 'url': url, **marker},
            )
    return _link_response(recipe, host, job, already_imported=False), True


def discard_link_recipe(user, recipe_id) -> bool:
    """✕ / autre lien / autre recette : la recette du collage part si elle n'a jamais été postée
    (privée, à soi, dans aucun repas). Son import en cours s'arrête avec elle."""
    from recipes.models import RecipeBatch

    recipe = Recipe.objects.filter(id=recipe_id, created_by=user, is_public=False).first()
    if recipe is None or not is_unposted_link_recipe(user, recipe):
        return False
    if RecipeBatch.objects.filter(recipe_id=recipe.id).exists():
        return False
    recipe.delete()
    return True


def publish_link_recipe(recipe: Recipe) -> None:
    """Posté (`log_cooked`) : la recette du collage devient visible comme toute recette de l'app."""
    Recipe.objects.filter(id=recipe.id).update(is_public=True)
    recipe.is_public = True


def unpublish_link_recipe(user, recipe_id) -> None:
    """Annuler : la recette du collage redevient privée (le brouillon peut encore la poster)."""
    from django.db.models import Exists, OuterRef

    from recipes.models import RecipeBatch

    if not recipe_id:
        return
    Recipe.objects.filter(id=recipe_id, created_by=user).exclude(
        Exists(RecipeBatch.objects.filter(recipe_id=OuterRef('pk')))
    ).update(is_public=False)
