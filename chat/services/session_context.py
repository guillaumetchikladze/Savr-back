"""
Contexte dynamique injecté à chaque tour agent : dates + « Profil culinaire » (saison, préférences,
foyer, cuisine, tablées, panier de la semaine, cuisinés récents, nouveautés du mois).

Calculé une fois par tour (consumer → `database_sync_to_async`), jamais par appel LLM. Budget
compact : chaque section est plafonnée (≈ 2 000 caractères au pire, ~600 tokens) et une section en
erreur est simplement omise (le chat ne doit jamais échouer pour son contexte).
"""

import logging
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser
from django.db.models import Prefetch
from django.utils import timezone

from chat.services.seasonality import MONTHS_FR, in_season_produce, season_for

logger = logging.getLogger(__name__)

_WEEKDAY_FR = (
    'lundi',
    'mardi',
    'mercredi',
    'jeudi',
    'vendredi',
    'samedi',
    'dimanche',
)
_WEEKDAY_SHORT = ('lun.', 'mar.', 'mer.', 'jeu.', 'ven.', 'sam.', 'dim.')

MAX_PREF_LABELS = 8
MAX_HOUSEHOLD_NAMES = 6
MAX_BASKET_ITEMS = 10
MAX_RECENT_COOKS = 10
RECENT_COOKS_DAYS = 90
MAX_TITLE_CHARS = 40
MAX_PROFILE_CHARS = 2200

_REGIME_FR = {
    'vegetarian': 'végétarien',
    'pescatarian': 'pescétarien',
    'vegan': 'végan',
    'gluten_free': 'sans gluten',
    'halal': 'halal',
    'kosher': 'casher',
    'keto': 'cétogène',
}
_BASKET_STATUS_FR = {
    'todo': 'à faire',
    'planned': 'prévu',
    'cooked': 'cuisiné',
    'carried_over': 'reporté',
    'not_done': 'pas fait',
}


def _week_bounds(anchor):
    """Semaine lun→dim contenant anchor."""
    start = anchor - timedelta(days=anchor.weekday())
    end = start + timedelta(days=6)
    return start, end


def _short(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + '…'


def _join_capped(values, cap: int) -> str:
    values = [_short(v, 30) for v in values if v]
    extra = len(values) - cap
    out = ', '.join(values[:cap])
    return f'{out} (+{extra})' if extra > 0 else out


# ---------------------------------------------------------------------------
# Sections du profil (chacune renvoie des lignes « - … » ou [])
# ---------------------------------------------------------------------------
def _season_lines(today) -> list[str]:
    produce = ', '.join(in_season_produce(today))
    return [f'- Saison : {season_for(today)} ({MONTHS_FR[today.month - 1]}, France). De saison : {produce}.']


def _preference_lines(user) -> list[str]:
    from recipes.dietary_filters import declared_labels_for_kind
    from recipes.models import PreferenceIngredientException

    parts = []
    diets = [_REGIME_FR.get(r, r) for r in declared_labels_for_kind(user, 'diet')]
    if diets:
        parts.append(f'régime {_join_capped(diets, MAX_PREF_LABELS)}')
    allergies = declared_labels_for_kind(user, 'allergy')
    if allergies:
        parts.append(f'allergies (JAMAIS) {_join_capped(allergies, MAX_PREF_LABELS)}')
    dislikes = declared_labels_for_kind(user, 'dislike')
    if dislikes:
        parts.append(f"n'aime pas (à éviter) {_join_capped(dislikes, MAX_PREF_LABELS)}")
    if not parts:
        return ['- Préférences : aucune déclarée.']
    lines = ['- Préférences : ' + ' ; '.join(parts) + '.']

    exceptions = [
        f'« {_short(e.ingredient.name if e.ingredient_id else e.ingredient_pattern, 30)} » OK malgré « {e.label} »'
        for e in PreferenceIngredientException.objects.filter(user=user)
        .select_related('ingredient')
        .order_by('-id')[:5]
        if e.ingredient_id or e.ingredient_pattern
    ]
    if exceptions:
        lines.append('- Exceptions validées : ' + ', '.join(exceptions) + '.')
    return lines


def _household_lines(user) -> list[str]:
    from accounts.services.household_service import household_for
    from recipes.dietary_filters import declared_labels_for_kind

    entries = household_for(user.id)
    if not entries:
        return ['- Foyer : tu cuisines pour toi seul·e (pas de foyer déclaré).']
    names = [e.name + (' (enfant)' if e.is_child else '') for e in entries]
    shown = ', '.join(_short(n, 24) for n in names[:MAX_HOUSEHOLD_NAMES])
    if len(names) > MAX_HOUSEHOLD_NAMES:
        shown += f' (+{len(names) - MAX_HOUSEHOLD_NAMES})'
    lines = [f'- Foyer : tu cuisines pour {len(entries) + 1} : toi, {shown}.']
    constraints = []
    for e in entries[:MAX_HOUSEHOLD_NAMES]:
        if e.user is None:
            continue
        member = []
        allergies = declared_labels_for_kind(e.user, 'allergy')
        if allergies:
            member.append(f'allergies {_join_capped(allergies, 4)}')
        diets = [_REGIME_FR.get(r, r) for r in declared_labels_for_kind(e.user, 'diet')]
        if diets:
            member.append(f'régime {_join_capped(diets, 3)}')
        if member:
            constraints.append(f'{_short(e.name, 24)} : {", ".join(member)}')
    if constraints:
        lines.append('- Contraintes du foyer : ' + ' ; '.join(constraints) + '.')
    return lines


def _equipment_lines(user) -> list[str]:
    """« Ta cuisine » : équipements présents + absents qui comptent (four, plaques, barbecue…)."""
    from recipes.services import equipment_catalog as catalog
    from recipes.services.equipment_service import effective_kitchen

    kitchen = effective_kitchen(user)
    if kitchen is None:
        return ['- Cuisine : équipements non renseignés (suppose une cuisine classique : four, plaques, micro-ondes).']
    owned = []
    for key in catalog.sort_keys(kitchen.keys):
        name = catalog.label(key).split(' (')[0].lower()
        if key == 'plaques' and kitchen.burners:
            name += f' ({kitchen.burners} feux{", induction" if kitchen.induction else ""})'
        if key == 'four' and kitchen.ovens > 1:
            name = f'{kitchen.ovens} fours'
        owned.append(name)
    whose = 'celle de ton foyer' if kitchen.source == 'household' else 'la tienne'
    lines = [f'- Cuisine ({whose}) : ' + (', '.join(owned) if owned else 'aucun équipement') + '.']
    absent = [
        e.short_label
        for e in catalog.CATALOG
        if not kitchen.has(e.key) and (e.hard or e.key in ('robot_cuiseur', 'airfryer', 'micro_ondes'))
    ]
    if absent:
        lines.append(
            "- Tu n'as pas : " + ', '.join(absent) + ". Ne propose pas de plat qui en demande, ou propose l'adaptation."
        )
    return lines


def _table_lines(user) -> list[str]:
    """Ses tablées (invite_to_table) : nom + #id, plafonnées comme le réglage TABLES_MAX_PER_USER."""
    from tables.models import TableMember

    rows = list(
        TableMember.objects.filter(user=user, status=TableMember.STATUS_ACCEPTED)
        .select_related('table')
        .order_by('table__name', 'table_id')[:5]
    )
    if not rows:
        return []
    return ['- Tablées : ' + ' ; '.join(f'{_short(m.table.name, 30)} [#{m.table_id}]' for m in rows) + '.']


def _basket_lines(user) -> list[str]:
    from recipes.services import basket_service as basket

    week_now = basket.current_week_start(user)
    items = basket.week_items(user, week_now)
    if not items:
        return ['- Panier « Cette semaine » : vide.']
    rendered = []
    for item in items[:MAX_BASKET_ITEMS]:
        status = basket.item_status(item, week_now)
        kind = f'recette {item.recipe_id}' if item.recipe_id else 'plat libre'
        when = ''
        if item.meal_plan_id and status == 'planned':
            when = f' {_WEEKDAY_SHORT[item.meal_plan.date.weekday()]} {item.meal_plan.date.strftime("%d/%m")}'
        rendered.append(f'{_short(item.display_title)} [#{item.id}, {kind}, {_BASKET_STATUS_FR[status]}{when}]')
    extra = len(items) - MAX_BASKET_ITEMS
    tail = f' (+{extra} autres)' if extra > 0 else ''
    return [f'- Panier « Cette semaine » ({len(items)}) : ' + ' ; '.join(rendered) + tail + '.']


def _recent_cook_lines(user, today) -> list[str]:
    from recipes.models import MealPlanRecipeBatch
    from recipes.services.cooked_service import cooked_meal_plans
    from stats.cuisines import CUISINES_BY_CODE

    plans = list(
        cooked_meal_plans(user, today - timedelta(days=RECENT_COOKS_DAYS), today)
        .order_by('-date', '-id')
        .prefetch_related(
            Prefetch(
                'meal_plan_recipe_batches',
                queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe').order_by('order'),
            ),
            'posts',
        )[:MAX_RECENT_COOKS]
    )
    if not plans:
        return ['- Cuisiné récemment : rien de noté ces 3 derniers mois.']
    rendered = []
    for plan in plans:
        recipes = [m.recipe_batch.recipe for m in plan.meal_plan_recipe_batches.all() if m.recipe_batch.recipe_id]
        if recipes:
            title = ' + '.join(r.title for r in recipes[:2])
            cuisine = CUISINES_BY_CODE.get(recipes[0].cuisine or '')
        else:
            labels = [p.label for p in plan.posts.all() if (p.label or '').strip()]
            title = labels[0] if labels else plan.custom_label
            cuisine = None
        if not (title or '').strip():
            continue
        suffix = f' ({cuisine.label.lower()})' if cuisine and cuisine.code not in ('autre', 'fusion') else ''
        post_ids = sorted(p.id for p in plan.posts.all() if p.user_id == plan.user_id)
        ref = f' [post {post_ids[0]}]' if post_ids else ''
        rendered.append(f'{plan.date.strftime("%d/%m")} {_short(title)}{suffix}{ref}')
    if not rendered:
        return []
    return ['- Cuisiné récemment (du plus récent, à ne pas reproposer tel quel) : ' + ' ; '.join(rendered) + '.']


def _novelty_lines(user, today) -> list[str]:
    from stats.services.stats_service import get_stats_payload, resolve_period

    payload = get_stats_payload(user, resolve_period('month', today=today), today=today)
    cooked = payload.get('cooked_count') or 0
    new_ings = payload.get('new_ingredients_in_period') or 0
    sample = [s for s in (payload.get('new_ingredients_sample') or [])[:3] if s]
    cuisines = payload.get('cuisines_count') or 0
    text = f'- Ce mois-ci : {cooked} plat(s) cuisiné(s), {new_ings} nouvel(s) ingrédient(s)'
    if sample:
        text += f' (ex. {", ".join(_short(s, 20) for s in sample)})'
    text += f', {cuisines} cuisine(s) du monde.'
    return [text]


def _safe(section, *args) -> list[str]:
    try:
        return section(*args)
    except Exception:  # pragma: no cover - garde-fou : une section en erreur est omise
        logger.exception('[agent context] section %s en échec', getattr(section, '__name__', section))
        return []


def build_profile_context(user: AbstractBaseUser, today=None) -> str:
    """Bloc « Profil culinaire » (compact, borné à MAX_PROFILE_CHARS)."""
    today = today or timezone.localdate()
    lines = ['Profil culinaire (référence interne, ne pas recopier en bloc) :']
    lines += _safe(_season_lines, today)
    lines += _safe(_preference_lines, user)
    lines += _safe(_household_lines, user)
    lines += _safe(_equipment_lines, user)
    lines += _safe(_table_lines, user)
    lines += _safe(_basket_lines, user)
    lines += _safe(_recent_cook_lines, user, today)
    lines += _safe(_novelty_lines, user, today)
    text = '\n'.join(lines)
    if len(text) > MAX_PROFILE_CHARS:
        text = text[: MAX_PROFILE_CHARS - 1].rsplit(' ', 1)[0] + '…'
    return text


def build_session_context_prompt(user: AbstractBaseUser, *, with_profile: bool = True) -> str:
    """
    Bloc système recalculé à chaque tour (date du jour, semaines, user, profil culinaire).
    Fait des requêtes DB : depuis du code async, l'appeler via `database_sync_to_async`.
    """
    today = timezone.localdate()
    now = timezone.localtime()
    week_start, week_end = _week_bounds(today)
    next_week_start = week_end + timedelta(days=1)
    next_week_end = next_week_start + timedelta(days=6)
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    username = getattr(user, 'username', None) or getattr(user, 'email', 'utilisateur')

    dates = f"""Contexte Tchikook Agent (référence interne — ne pas recopier tel quel à l'utilisateur) :
- Utilisateur : {username} (id {user.pk})
- Maintenant : {now.strftime('%Y-%m-%d %H:%M')} ({_WEEKDAY_FR[today.weekday()]}) — fuseau {timezone.get_current_timezone()}
- Aujourd'hui : {today.isoformat()}
- Hier : {yesterday.isoformat()} | Demain : {tomorrow.isoformat()}
- Cette semaine (lun→dim) : {week_start.isoformat()} → {week_end.isoformat()}
- Semaine prochaine (lun→dim) : {next_week_start.isoformat()} → {next_week_end.isoformat()}

Règles dates et planning :
- Interprète toi-même « cette semaine », « la semaine prochaine », « demain », « ce week-end », etc. avec les dates ci-dessus.
- Ne demande PAS à l'utilisateur des dates au format AAAA-MM-JJ si une expression relative ou le contexte suffit.
- Pour une question sur le planning, appelle get_meal_plans tout de suite avec start_date et end_date calculés (YYYY-MM-DD).
- Ne demande des précisions de dates que si la période est vraiment ambiguë (ex. « en été » sans autre indice)."""
    if not with_profile:
        return dates
    return f'{dates}\n\n{build_profile_context(user, today)}'
