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

Mise en forme et import : ouverts à tous, plafonnés par mois (`ai_recipe_quota`).
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
_JOB_KIND = {JOB_FORMAT: 'format', JOB_IMPORT: 'import'}


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


def create_cooked_recipe(user, *, title: str, plan: CookedRecipePlan) -> Recipe:
    """Recette posée tout de suite (dans la transaction de `log_cooked`)."""
    recipe = Recipe.objects.create(
        title=(title or WRITTEN_RECIPE_DEFAULT_TITLE)[:RECIPE_TITLE_MAX_LENGTH],
        prep_time=0,
        cook_time=0,
        created_by=user,
        is_public=True,  # comme toute recette créée dans l'app (amis qui voient le post)
        source_type='imported' if plan.import_url else 'user_created',
        import_source_url=_storable_source_url(plan.import_url) if plan.import_url else None,
    )
    if plan.recipe_text:
        Step.objects.create(recipe=recipe, order=1, instruction=plan.recipe_text)
    return recipe


def enqueue_cooked_recipe_job(user, recipe: Recipe, *, plan: CookedRecipePlan, keep_title: bool) -> dict | None:
    """
    Mise en forme / import en arrière-plan de la recette posée par « J'ai cuisiné ».
    Renvoie le résumé `recipe_job` de la réponse (None si rien à faire).
    Doit être appelé dans la transaction : la tâche part au commit.
    """
    if not (plan.recipe_text or plan.import_url):
        return None
    job_type = JOB_IMPORT if plan.import_url else JOB_FORMAT
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
