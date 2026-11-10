"""
Badges loufoques (spec `.claude-work/specs/badges-quirky.md`) : beaucoup, drôles, rares, partageables.

Chaque badge = une condition absurde mais vérifiable sur l'historique « cuisiné » complet, évaluée sur
`badge_history.BadgeHistory` (construit une fois par évaluation, nombre de requêtes constant).
Copie : tutoiement, une pointe d'humour, jamais culpabilisant, jamais le mot interdit (test de copie).

Familles : même recette, calendrier, heures, défis absurdes, à plusieurs, matériel (la météo est dans
`badges_weather.py`, phase 2, derrière `WEATHER_BADGES_ENABLED`).

Non retenu : « Passeport tamponné » (20 cuisines) = doublon exact du badge historique `cuisines_20`.
"""

from __future__ import annotations

import bisect
import re
from collections import defaultdict
from datetime import date, timedelta

from ..text import fold
from .badge_history import MEAT_GROUPS, BadgeHistory, DishDay, history_of, week_monday
from .badge_types import TIER_COMMUN, TIER_LEGENDAIRE, TIER_RARE, TIER_TRES_RARE, BadgeDef, once, reached


def _h(fn):
    """Adapte une fonction de `BadgeHistory` en métrique de `StatsResult`."""

    def metric(stats):
        return fn(history_of(stats))

    return metric


def _first(predicate):
    """Badge « une fois » : premier plat-jour qui satisfait `predicate` (contexte : le plat)."""

    def fn(h: BadgeHistory):
        dd = next((dd for dd in h.dish_days if predicate(dd)), None)
        return once(dd.date, dish=dd.label) if dd else (0, None)

    return _h(fn)


def _count(predicate, target: int):
    """Nombre de plats-jours qui satisfont `predicate` (date de la `target`-ième occurrence)."""
    return _h(lambda h: reached(h.where(predicate), target))


def _rx(expr: str) -> re.Pattern:
    return re.compile(expr)


# ---------------------------------------------------------------------------
# Mots-clés (texte plié : minuscules, sans accents)
# ---------------------------------------------------------------------------
CREPES = _rx(r'\bcrepes?\b')
GALETTE_ROIS = _rx(r'\bgalettes? des rois\b|\bfrangipanes?\b|\b(brioche|gateau|couronne) des rois\b')
BEIGNETS = _rx(r'\b(beignets?|bugnes?|merveilles?|oreillettes?|ganses?|crepes?|boules de berlin|chichis?)\b')
CHOCOLAT = _rx(r'\bchocolats?\b|\bcacao\b|\bpralines?\b|\bganache\b')
RACLETTE = _rx(r'\b(raclettes?|tartiflettes?|croziflettes?|reblochonnade|mont d or (chaud|au four))\b|\bfondue\b(?! d)')
SOUPE = _rx(r'\b(soupes?|veloutes?|potages?|minestrone|garbure|pho|ramen|harira|bortsch|chorba|bouillabaisse)\b')
SOUPE_FROIDE = _rx(r'\b(gaspacho|gazpacho|salmorejo|vichyssoise|soupe froide|soupe de (fruits|fraises|melon|peches))\b')
BBQ = _rx(r'\b(barbecue|bbq|plancha|grillades?)\b')
BUCHE = _rx(r'\bbuches?\b(?! de chevre)(?! chevre)')
DESSERT = _rx(
    r'\b(gateaux?|cakes?|tarte tatin|tartes? (aux?|a la) (pommes|poires|fraises|framboises|citron|chocolat|'
    r'abricots|prunes|myrtilles|figues|rhubarbe|peches|mirabelles|noix|amandes|sucre|praline)|crumbles?|'
    r'cookies?|brownies?|muffins?|clafoutis|fondants?|moelleux|madeleines?|financiers?|quatre quarts|'
    r'cheesecakes?|tiramisus?|mousses? au chocolat|crepes?|gaufres?|far breton|kouign amann|flans?|'
    r'banana bread|scones?|cupcakes?|pancakes?|churros|beignets?|biscuits?|sables?|charlottes?|eclairs?|'
    r'paris brest|babas?|savarins?|pain d epices|brioches?|riz au lait|canneles?|macarons?|meringues?|'
    r'pavlovas?|profiteroles?|choux a la creme|galettes? des rois|buches?)\b'
)
ONE_POT = _rx(r'\bone ?pot\b|\bone pan\b|\bune seule casserole\b|\btout en un\b')
AIRFRYER = _rx(r'\bair ?fryer\b|\bfriteuse sans huile\b')
ROBOT = _rx(r'\b(thermomix|cookeo|monsieur cuisine|companion|robot cuiseur)\b')

AIL = _rx(r'\bail\b|\baillets?\b|\baioli\b')
BEURRE = _rx(r'\bbeurre\b(?! (de|d) (cacahuetes?|arachides?|cacao|amandes?|cajou|karite))')
PIMENT = _rx(
    r'\b(piments?|chili|chilli|harissa|sriracha|jalapenos?|cayenne|espelette|tabasco|gochujang|sambal|'
    r'habaneros?|chipotle|pili pili|piri piri|sauce piquante)\b(?! doux)'
)
FROMAGE = _rx(
    r'\b(fromages?(?! blanc)|parmesan|parmigiano|grana padano|pecorino|gruyere|emmental|comte|beaufort|'
    r'mozzarella|burrata|feta|chevre|brie|camembert|roquefort|bleu d|gorgonzola|reblochon|raclette|'
    r'mont d or|tomme|cantal|salers|morbier|munster|maroilles|epoisses|ricotta|mascarpone|cheddar|gouda|'
    r'edam|mimolette|halloumi|manchego|scamorza|provolone|taleggio|fontina|saint nectaire|ossau iraty|'
    r'abondance|vacherin|cancoillotte|boursin|saint marcellin|fourme|stilton|paneer|cream cheese|'
    r'philadelphia|coulommiers|chaource|langres|pont l eveque|livarot|neufchatel|rocamadour|crottin|'
    r'valencay|sainte maure|tete de moine|appenzeller|bleu)\b'
)

COURGE = _rx(r'\b(courges?|butternut|potirons?|potimarrons?|citrouilles?)\b')
OEUF = _rx(r'\boeufs?\b')
PATES = _rx(
    r'\b(pates?|spaghettis?|tagliatelles?|penne|coquillettes?|lasagnes?|linguines?|fusilli|rigatoni|macaronis?|farfalles?|gnocchis?)\b'
)
PATATE = _rx(r'\b(pommes? de terre|patates?|puree|frites?)\b')
POISSON = _rx(
    r'\b(poissons?|saumons?|cabillaud|thon|truite|sardines?|maquereaux?|crevettes?|moules?|gambas|calmars?|poulpes?)\b'
)


def easter(year: int) -> date:
    """Dimanche de Pâques (calendrier grégorien, algorithme anonyme de Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    lv = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lv) // 451
    month = (h + lv - 7 * m + 114) // 31
    day = (h + lv - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _local_hour_on(dd: DishDay, lo: int, hi: int, *, next_day: bool = False) -> bool:
    """Saisie (heure locale) dans [lo, hi[ le jour du plat (ou le lendemain si `next_day`)."""
    if dd.log_local is None:
        return False
    delta = (dd.log_local.date() - dd.date).days
    if delta not in ((0, 1) if next_day else (0,)):
        return False
    return lo <= dd.log_local.hour < hi


def _slot_between(dd: DishDay, lo: int, hi: int) -> bool:
    """Saisie le jour même ou créneau prévu (`scheduled_time`) dans [lo, hi[."""
    if _local_hour_on(dd, lo, hi):
        return True
    return dd.scheduled_time is not None and lo <= dd.scheduled_time.hour < hi


# ---------------------------------------------------------------------------
# La même recette, encore
# ---------------------------------------------------------------------------
def _repeat(n: int):
    def fn(h: BadgeHistory):
        best, achieved, dish = 0, None, None
        for dds in h.dates_by_dish.values():
            best = max(best, len(dds))
            if len(dds) >= n and (achieved is None or dds[n - 1].date < achieved):
                achieved, dish = dds[n - 1].date, dds[-1].label
        return (best, achieved, {'dish': dish}) if achieved else (best, None)

    return _h(fn)


def _repeat_days(n: int):
    def fn(h: BadgeHistory):
        best, achieved, dish = 0, None, None
        for dds in h.dates_by_dish.values():
            run, prev = 0, None
            for dd in dds:
                run = run + 1 if prev is not None and (dd.date - prev).days == 1 else 1
                prev = dd.date
                best = max(best, run)
                if run == n and (achieved is None or dd.date < achieved):
                    achieved, dish = dd.date, dd.label
        return (min(best, n), achieved, {'dish': dish}) if achieved else (min(best, n), None)

    return _h(fn)


def _grand_retour(h: BadgeHistory):
    achieved, dish = None, None
    for dds in h.dates_by_dish.values():
        for i in range(1, len(dds)):
            if (dds[i].date - dds[i - 1].date).days >= 180:
                if achieved is None or dds[i].date < achieved:
                    achieved, dish = dds[i].date, dds[i].label
                break
    return once(achieved, dish=dish)


def _rituel(h: BadgeHistory, target: int = 4):
    best, achieved, dish = 0, None, None
    for dds in h.dates_by_dish.values():
        by_weekday: dict[int, list[DishDay]] = defaultdict(list)
        for dd in dds:
            by_weekday[dd.date.weekday()].append(dd)
        for d_list in by_weekday.values():
            d_list.sort(key=lambda dd: dd.date)
            run = 1 if d_list else 0
            best = max(best, run)
            for i in range(1, len(d_list)):
                run = run + 1 if (d_list[i].date - d_list[i - 1].date).days == 7 else 1
                best = max(best, run)
                if run >= target and (achieved is None or d_list[i].date < achieved):
                    achieved, dish = d_list[i].date, d_list[i].label
    return (min(best, target), achieved, {'dish': dish}) if achieved else (min(best, target), None)


def _abonnement(h: BadgeHistory, target: int = 6):
    best, achieved, dish = 0, None, None
    for dds in h.dates_by_dish.values():
        by_month: dict[tuple[int, int], DishDay] = {}
        for dd in sorted(dds, key=lambda d: d.date):
            by_month.setdefault((dd.date.year, dd.date.month), dd)
        sorted_months = sorted(by_month.keys())
        run = 1 if sorted_months else 0
        best = max(best, run)
        for i in range(1, len(sorted_months)):
            y1, m1 = sorted_months[i - 1]
            y2, m2 = sorted_months[i]
            is_next = (y2 == y1 and m2 == m1 + 1) or (y2 == y1 + 1 and m1 == 12 and m2 == 1)
            run = run + 1 if is_next else 1
            best = max(best, run)
            if run >= target and (achieved is None or by_month[sorted_months[i]].date < achieved):
                achieved, dish = by_month[sorted_months[i]].date, by_month[sorted_months[i]].label
    return (min(best, target), achieved, {'dish': dish}) if achieved else (min(best, target), None)


def _menu_degustation(h: BadgeHistory, target: int = 10, days: int = 7):
    dish_events = [(dd.date, dd.key) for dd in h.dish_days if dd.key[0] != 'm']
    best, achieved = 0, None
    start = 0
    for end in range(len(dish_events)):
        while (dish_events[end][0] - dish_events[start][0]).days >= days:
            start += 1
        distinct = len({k for _, k in dish_events[start : end + 1]})
        best = max(best, distinct)
        if achieved is None and distinct >= target:
            achieved = dish_events[end][0]
    return min(best, target), achieved


def _anniversary(h: BadgeHistory):
    achieved, dish = None, None
    for dds in h.dates_by_dish.values():
        days = {dd.date for dd in dds}
        for dd in dds:
            # Un an plus tard, jour pour jour à un jour près (364 à 367 jours selon les années bissextiles).
            if any(dd.date - timedelta(days=delta) in days for delta in range(364, 368)):
                if achieved is None or dd.date < achieved:
                    achieved, dish = dd.date, dd.label
                break
    return once(achieved, dish=dish)


def _perfectionist(h: BadgeHistory):
    achieved, dish = None, None
    for dds in h.dates_by_dish.values():
        photos = [dd.date for dd in dds if dd.photo]
        for i in range(len(photos) - 3):
            if (photos[i + 3] - photos[i]).days <= 30:
                if achieved is None or photos[i + 3] < achieved:
                    achieved, dish = photos[i + 3], dds[-1].label
                break
    return once(achieved, dish=dish)


# ---------------------------------------------------------------------------
# Les heures et les durées
# ---------------------------------------------------------------------------
def _measured(predicate):
    """Plat-jour dont une préparation a une durée mesurée (mode cuisine) qui satisfait `predicate`."""

    def fn(h: BadgeHistory):
        dd = next((dd for dd in h.dish_days if any(predicate(m) for m in h.measured_minutes(dd))), None)
        return once(dd.date, dish=dd.label) if dd else (0, None)

    return _h(fn)


def _seven_days(h: BadgeHistory):
    weeks: dict[date, set[date]] = defaultdict(set)
    for day in h.cooked_days:
        weeks[week_monday(day)].add(day)
    best = max((len(days) for days in weeks.values()), default=0)
    full = sorted(max(days) for days in weeks.values() if len(days) == 7)
    return best, (full[0] if full else None)


def _three_meals(h: BadgeHistory):
    day = next((d for d in sorted(h.slots_by_day) if {'breakfast', 'lunch', 'dinner'} <= h.slots_by_day[d]), None)
    return once(day)


def _douze_mois(h: BadgeHistory, target: int = 12):
    months = sorted({(d.year, d.month) for d in h.cooked_days})
    if not months:
        return 0, None
    best, achieved = 1, None
    run = 1
    for i in range(1, len(months)):
        y1, m1 = months[i - 1]
        y2, m2 = months[i]
        is_next = (y2 == y1 and m2 == m1 + 1) or (y2 == y1 + 1 and m1 == 12 and m2 == 1)
        run = run + 1 if is_next else 1
        best = max(best, run)
        if run >= target and achieved is None:
            achieved = next(d for d in sorted(h.cooked_days) if (d.year, d.month) == months[i])
    return min(best, target), achieved


def _tchikiversaire(h: BadgeHistory):
    joined = getattr(h.user, 'date_joined', None)
    if not joined:
        return 0, None
    achieved = None
    for dd in h.dish_days:
        if (dd.date.month, dd.date.day) == (joined.month, joined.day) and dd.date.year > joined.year:
            achieved = dd.date if achieved is None else min(achieved, dd.date)
    return once(achieved)


def _brunch_dimanche(dd: DishDay) -> bool:
    if dd.date.weekday() != 6:
        return False
    has_egg = 'oeufs' in dd.groups or dd.has_ingredient(OEUF) or dd.matches(OEUF)
    if not has_egg:
        return False
    return _slot_between(dd, 10, 14)


def _midi_pile(dd: DishDay) -> bool:
    return dd.log_local is not None and dd.log_local.hour == 12 and dd.log_local.minute == 0


def _oiseau_de_nuit(h: BadgeHistory, target: int = 3):
    weeks: dict[date, list[date]] = defaultdict(list)
    for dd in h.dish_days:
        if dd.log_local and dd.log_local.hour >= 22:
            weeks[week_monday(dd.date)].append(dd.date)
    best = max((len(days) for days in weeks.values()), default=0)
    achieved = None
    for _mon, days in sorted(weeks.items()):
        if len(days) >= target:
            d = sorted(days)[target - 1]
            achieved = d if achieved is None else min(achieved, d)
    return min(best, target), achieved


# ---------------------------------------------------------------------------
# Les défis absurdes
# ---------------------------------------------------------------------------
RAINBOW_SET = frozenset({'rouge', 'orange', 'jaune', 'vert', 'violet'})


def _rainbow(h: BadgeHistory):
    weeks: dict[date, set[str]] = defaultdict(set)
    best, achieved = 0, None
    for dd in h.dish_days:
        colours = weeks[week_monday(dd.date)]
        colours.update(c for i in dd.ingredients.values() if (c := i.colour) in RAINBOW_SET)
        best = max(best, len(colours))
        if achieved is None and len(colours) >= len(RAINBOW_SET):
            achieved = dd.date
    return best, achieved


def _alphabet(n: int):
    def fn(h: BadgeHistory):
        first: dict[str, date] = {}
        for dd in h.dish_days:
            letter = (fold(dd.label) or ' ')[0]  # 1re lettre du plat (1re recette d'un repas à plusieurs)
            if 'a' <= letter <= 'z':
                first.setdefault(letter, dd.date)
        return reached(sorted(first.values()), n)

    return _h(fn)


def _cuisines_in_days(n: int, days: int):
    def fn(h: BadgeHistory):
        events = [(dd.date, c) for dd in h.dish_days for c in dd.cuisines]
        best, achieved = 0, None
        start = 0
        for end in range(len(events)):
            while (events[end][0] - events[start][0]).days >= days:
                start += 1
            distinct = len({c for _, c in events[start : end + 1]})
            best = max(best, distinct)
            if achieved is None and distinct >= n:
                achieved = events[end][0]
        return min(best, n), achieved

    return _h(fn)


def _distinct_plants_per(period, n: int):
    """`period(date)` → clé (mois, année) ; n végétaux différents (hors ingrédients de base) dans la même."""

    def fn(h: BadgeHistory):
        buckets: dict[tuple, set[int]] = defaultdict(set)
        best, achieved = 0, None
        for dd in h.dish_days:
            bucket = buckets[period(dd.date)]
            bucket.update(i.id for i in dd.ingredients.values() if i.plant and not i.staple)
            best = max(best, len(bucket))
            if achieved is None and len(bucket) >= n:
                achieved = dd.date
        return best, achieved

    return _h(fn)


def _cheeses(dd: DishDay) -> int:
    return sum(1 for i in dd.ingredients.values() if FROMAGE.search(i.name))


def _all_orange(dd: DishDay) -> bool:
    plants = [i for i in dd.ingredients.values() if i.plant and not i.staple]
    return len(plants) >= 2 and all(i.colour == 'orange' for i in plants)


def _veggie_month(h: BadgeHistory, *, days: int = 30, min_dishes: int = 8):
    """30 jours (révolus) sans viande ni poisson, avec au moins 8 plats-jours à recette identifiée."""
    veg, meat = [], []
    for dd in h.dish_days:
        groups = dd.groups
        if groups:  # plats sans ingrédient classé : inconnus, ni comptés ni bloquants
            (meat if groups & MEAT_GROUPS else veg).append(dd.date)
    for i, start in enumerate(veg):
        end = start + timedelta(days=days - 1)
        if end > h.today:
            break
        j = bisect.bisect_left(meat, start)
        if j < len(meat) and meat[j] <= end:
            continue
        if bisect.bisect_right(veg, end) - i >= min_dishes:
            return once(end)
    return 0, None


# ---------------------------------------------------------------------------
# À plusieurs
# ---------------------------------------------------------------------------
def _sundays_with_guests(h: BadgeHistory):
    sundays = sorted({dd.date for dd in h.dish_days if dd.date.weekday() == 6 and dd.people > 1})
    return reached(sundays, 4)


def _friends_dishes_redone(h: BadgeHistory):
    first: dict[int, date] = {}
    for dd in h.dish_days:
        for pid in dd.inspired_by_post_ids:
            first.setdefault(pid, dd.date)
    return reached(sorted(first.values()), 5)


def _miams(stats):
    total = int(stats.payload.get('miam_received') or 0)
    return total, (stats.today if total >= 100 else None)


def _table_weeks(h: BadgeHistory, tables: list[dict]):
    """Semaines où j'ai cuisiné, par tablée : (lundi, membres présents, {membre: 1re date cuisinée})."""
    mine = sorted({week_monday(d) for d in h.cooked_days})
    for table in tables:
        members, cooked = table['members'], table['cooked']
        for monday in mine:
            sunday = monday + timedelta(days=6)
            present = [uid for uid, joined in members.items() if joined <= sunday]
            if len(present) < 2 or h.user_id not in present:
                continue
            firsts = {}
            for uid in present:
                days = [d for d in cooked.get(uid, ()) if monday <= d <= sunday]
                if days:
                    firsts[uid] = min(days)
            yield monday, present, firsts


def _table_complete(h: BadgeHistory):
    achieved = None
    for _, present, firsts in _table_weeks(h, h.tables):
        if len(firsts) == len(present):
            day = max(firsts.values())
            achieved = day if achieved is None else min(achieved, day)
    return once(achieved)


def _opens_ball(h: BadgeHistory, *, weeks: int = 4):
    best, achieved = 0, None
    for table in h.tables:
        prev_monday, run = None, 0
        for monday, _, firsts in _table_weeks(h, [table]):
            me = firsts.get(h.user_id)
            others = [d for uid, d in firsts.items() if uid != h.user_id]
            # Au moins un·e autre a cuisiné cette semaine-là, et personne avant moi (égalité = ex æquo).
            if me is not None and others and all(me <= d for d in others):
                run = run + 1 if prev_monday == monday - timedelta(days=7) else 1
                best = max(best, run)
                if run >= weeks and (achieved is None or me < achieved):
                    achieved = me
            else:
                run = 0
            prev_monday = monday
    return min(best, weeks), achieved


# ---------------------------------------------------------------------------
# Le matériel
# ---------------------------------------------------------------------------
def _without_oven(h: BadgeHistory):
    kitchen = h.kitchen
    if kitchen is None or kitchen.has('four'):
        return 0, None
    return reached(h.where(lambda dd: dd.has_recipe and 'four' not in dd.equipment), 20)


def _homemade(h: BadgeHistory, target: int = 5):
    total, achieved = 0, None
    for dd in h.dish_days:
        total += dd.made_sub_recipes
        if achieved is None and total >= target:
            achieved = dd.date
    return total, achieved


# Articles en tête de libellé, ignorés pour l'initiale (« La paella » → p). Texte plié : « L'omelette » → « l omelette ».
LEADING_ARTICLE = _rx(r'^(?:(?:le|la|les|l|un|une|des|du|de la|d)\s+)')


def initial_of(label: str) -> str:
    """Initiale d'un plat, articles ignorés ('' si ce n'est pas une lettre)."""
    folded = LEADING_ARTICLE.sub('', fold(label))
    letter = folded[:1]
    return letter if 'a' <= letter <= 'z' else ''


def _alliteration(h: BadgeHistory, target: int = 5):
    # semaine → initiale → plat → premier jour de la semaine où il a été cuisiné
    weeks: dict[date, dict[str, dict[tuple, date]]] = defaultdict(lambda: defaultdict(dict))
    for dd in h.dish_days:
        if dd.key[0] == 'm':
            continue
        letter = initial_of(dd.label)
        if letter:
            weeks[week_monday(dd.date)][letter].setdefault(dd.key, dd.date)
    best, achieved = 0, None
    for _mon, by_letter in sorted(weeks.items()):
        for _letter, dishes in by_letter.items():
            best = max(best, len(dishes))
            if len(dishes) >= target:
                # Daté le jour du `target`-ième plat (jamais la fin de semaine, qui peut être dans le futur).
                day = sorted(dishes.values())[target - 1]
                achieved = day if achieved is None else min(achieved, day)
    return min(best, target), achieved


def _rien_ne_se_perd(h: BadgeHistory, shared: int = 4):
    """Deux jours de suite, deux plats différents (n'importe lesquels de chaque jour) avec `shared` ingrédients communs."""
    by_day: dict[date, list[tuple[tuple, set[int]]]] = defaultdict(list)
    for dd in h.dish_days:
        by_day[dd.date].append((dd.key, {iid for iid, ing in dd.ingredients.items() if not ing.staple}))
    for day in sorted(by_day):
        before = by_day.get(day - timedelta(days=1))
        if not before:
            continue
        for key, ings in by_day[day]:
            if len(ings) < shared:
                continue
            if any(k != key and len(ings & prev) >= shared for k, prev in before):
                return once(day)
    return 0, None


CONTINENT_MAP = {
    'francaise': 'europe',
    'italienne': 'europe',
    'espagnole': 'europe',
    'portugaise': 'europe',
    'grecque': 'europe',
    'britannique': 'europe',
    'allemande': 'europe',
    'scandinave': 'europe',
    'marocaine': 'afrique',
    'ethiopienne': 'afrique',
    'africaine_ouest': 'afrique',
    'indienne': 'asie',
    'thai': 'asie',
    'vietnamienne': 'asie',
    'chinoise': 'asie',
    'japonaise': 'asie',
    'coreenne': 'asie',
    'americaine': 'ameriques',
    'mexicaine': 'ameriques',
    'peruvienne': 'ameriques',
    'bresilienne': 'ameriques',
    'turque': 'moyen_orient',
    'libanaise': 'moyen_orient',
}


def _cinq_continents(h: BadgeHistory):
    continents_seen: set[str] = set()
    achieved = None
    for dd in h.dish_days:
        for c in dd.cuisines:
            cont = CONTINENT_MAP.get(c)
            if cont:
                continents_seen.add(cont)
                if len(continents_seen) >= 5 and achieved is None:
                    achieved = dd.date
    return len(continents_seen), achieved


def _al_dente(h: BadgeHistory, target: int = 5):
    weeks: dict[date, list[date]] = defaultdict(list)
    for dd in h.dish_days:
        if dd.matches(PATES) or dd.has_ingredient(PATES):
            weeks[week_monday(dd.date)].append(dd.date)
    best = max((len(days) for days in weeks.values()), default=0)
    achieved = None
    for _mon, days in sorted(weeks.items()):
        if len(days) >= target:
            d = sorted(days)[target - 1]
            achieved = d if achieved is None else min(achieved, d)
    return min(best, target), achieved


def _herboriste(h: BadgeHistory, target: int = 5):
    achieved = None
    for dd in h.dish_days:
        herbs = [i for i in dd.ingredients.values() if i.group == 'herbes_epices' and not i.staple]
        if len(herbs) >= target:
            achieved = dd.date if achieved is None else min(achieved, dd.date)
    return once(achieved)


def _pyramide(h: BadgeHistory, target: int = 6):
    achieved = None
    for dd in h.dish_days:
        if len(dd.groups) >= target:
            achieved = dd.date if achieved is None else min(achieved, dd.date)
    return once(achieved)


def _carton_plein(h: BadgeHistory, target: int = 10):
    meals = {m.id: m for m in h.stats.meals}
    for dd in h.dish_days:
        meal = meals.get(dd.meal_id)
        if meal and any(p.get('miam', 0) >= target for p in meal.posts):
            return 1, dd.date
    return 0, None


def _tablee_connectee(h: BadgeHistory, target: int = 3):
    dd = next((dd for dd in h.dish_days if len(h.stats.guests_by_meal.get(dd.meal_id, ())) >= target), None)
    return once(dd.date, dish=dd.label) if dd else (0, None)


def _pilote_auto(h: BadgeHistory, target: int = 20):
    """20 recettes suivies jusqu'au bout en mode cuisine, daté de la 20e (jamais « aujourd'hui » : rattrapage silencieux)."""
    total, achieved = reached([day for _batch, _minutes, day in h.cooking_completions], target)
    return min(total, target), achieved


# ---------------------------------------------------------------------------
# Registre
# ---------------------------------------------------------------------------
def _b(
    code,
    title,
    emoji,
    description,
    phrase,
    family,
    target,
    metric,
    rarity=TIER_COMMUN,
    hint='',
    hints=(),
    icon='',
    secret=None,
    mystery='',
):
    all_hints = tuple(hints) if hints else ((hint,) if hint else ())
    first_hint = all_hints[0] if all_hints else hint
    is_secret = bool(all_hints or hint) if secret is None else bool(secret)
    return BadgeDef(
        code=code,
        title=title,
        emoji=emoji,
        icon=icon,
        description=description,
        category=family,
        target=target,
        metric=metric,
        family=family,
        phrase=phrase,
        secret=is_secret,
        hint=first_hint,
        hints=all_hints,
        mystery=mystery or ('enigme' if all_hints else ''),
        rarity_guess=rarity,
    )


R, TR, L = TIER_RARE, TIER_TRES_RARE, TIER_LEGENDAIRE

QUIRKY_BADGES: tuple[BadgeDef, ...] = (
    # --- La même recette, encore ---
    _b(
        'repeat_5',
        'La fétiche',
        '💘',
        'Le même plat cuisiné 5 fois.',
        'Tu sais ce que tu aimes.',
        'meme_recette',
        5,
        _repeat(5),
        icon='heart-arrow',
    ),
    _b(
        'repeat_10',
        'Obsession',
        '🌀',
        'Le même plat cuisiné 10 fois.',
        'À ce stade, c’est une relation.',
        'meme_recette',
        10,
        _repeat(10),
        R,
        icon='cyclone',
    ),
    _b(
        'repeat_25',
        'Le plat signature',
        '✍️',
        'Le même plat cuisiné 25 fois.',
        'On devrait lui donner ton nom.',
        'meme_recette',
        25,
        _repeat(25),
        TR,
        icon='writing-hand',
    ),
    _b(
        'repeat_3_days',
        'Restes assumés',
        '🥡',
        'Le même plat 3 jours d’affilée.',
        'Le lendemain c’est meilleur, le surlendemain aussi.',
        'meme_recette',
        3,
        _repeat_days(3),
        R,
        icon='takeout-box',
    ),
    _b(
        'dish_anniversary',
        'Joyeux anniversaire, le plat',
        '🎂',
        'Le même plat cuisiné à un an d’écart, jour pour jour (à un jour près).',
        'Une tradition est née.',
        'meme_recette',
        1,
        _h(_anniversary),
        TR,
        icon='birthday-cake',
        hints=(
            'Certains plats méritent qu’on souffle leurs bougies.',
            'Regarde ce que tu cuisinais l’an passé à pareille date.',
            'Le même plat cuisiné à un an d’écart, jour pour jour.',
        ),
    ),
    _b(
        'perfectionist',
        'Le perfectionniste',
        '📐',
        'Le même plat 4 fois en un mois, avec une photo à chaque fois.',
        'Celle-ci, c’est la bonne. Ou la suivante.',
        'meme_recette',
        1,
        _h(_perfectionist),
        TR,
        icon='triangular-ruler',
        hints=(
            'Refaire, photographier, recommencer.',
            'Quatre fois la même recette dans le même mois avec photo.',
            'Le même plat 4 fois en un mois, chaque fois pris en photo.',
        ),
    ),
    _b(
        'grand_retour',
        'Le grand retour',
        '🪃',
        'Un plat recuisiné après 180 jours ou plus sans l’avoir fait.',
        'Un retour très attendu.',
        'meme_recette',
        1,
        _h(_grand_retour),
        R,
        icon='boomerang',
    ),
    _b(
        'rituel',
        'Le rituel',
        '🕯️',
        'Le même plat, le même jour de la semaine, 4 semaines de suite.',
        'Lundi, c’est sacré.',
        'meme_recette',
        4,
        _h(_rituel),
        TR,
        icon='candle',
        hints=(
            'Certains jours ont leur plat.',
            'Toujours le même jour, encore et encore.',
            'Même plat, même jour de la semaine, 4 semaines d’affilée.',
        ),
    ),
    _b(
        'abonnement',
        'L’abonnement',
        '📆',
        'Le même plat au moins une fois par mois, 6 mois de suite.',
        'Résiliation impossible.',
        'meme_recette',
        6,
        _h(_abonnement),
        TR,
        icon='calendar',
    ),
    _b(
        'menu_degustation',
        'Menu dégustation',
        '🍱',
        '10 plats différents en 7 jours.',
        'Dix plats, dix histoires, une seule semaine.',
        'meme_recette',
        10,
        _h(_menu_degustation),
        R,
        icon='bento-box',
    ),
    # --- Le calendrier ---
    _b(
        'chandeleur',
        'Chandeleur réglementaire',
        '🥞',
        'Des crêpes le 2 février.',
        'La pièce dans une main, la poêle dans l’autre.',
        'calendrier',
        1,
        _first(lambda dd: (dd.date.month, dd.date.day) == (2, 2) and dd.matches(CREPES)),
        R,
        icon='pancakes',
    ),
    _b(
        'galette',
        'Roi ou reine',
        '👑',
        'Une galette des rois cuisinée en janvier.',
        'La fève, c’était toi. Évidemment.',
        'calendrier',
        1,
        _first(lambda dd: dd.date.month == 1 and dd.matches(GALETTE_ROIS)),
        icon='crown',
    ),
    _b(
        'mardi_gras',
        'Mardi gras',
        '🎭',
        'Des beignets ou des crêpes le jour de mardi gras.',
        'Le déguisement est facultatif, la friture non.',
        'calendrier',
        1,
        _first(lambda dd: dd.date == easter(dd.date.year) - timedelta(days=47) and dd.matches(BEIGNETS)),
        R,
        icon='performing-arts',
    ),
    _b(
        'paques',
        'Chasseur d’œufs',
        '🐣',
        'Du chocolat le week-end de Pâques.',
        'Les cloches sont passées par ta cuisine.',
        'calendrier',
        1,
        _first(
            lambda dd: (
                abs((dd.date - easter(dd.date.year)).days) <= 1
                and (dd.matches(CHOCOLAT) or dd.has_ingredient(CHOCOLAT))
            )
        ),
        R,
        icon='hatching-chick',
    ),
    _b(
        'reveillon',
        'Réveillonneur',
        '🎄',
        'Cuisiné le 24 ou le 31 décembre pour 4 personnes ou plus.',
        'Le vrai cadeau, c’était le repas.',
        'calendrier',
        1,
        _first(lambda dd: (dd.date.month, dd.date.day) in ((12, 24), (12, 31)) and dd.people >= 4),
        R,
        icon='christmas-tree',
    ),
    _b(
        'jour_de_l_an',
        'Courageux du 1er janvier',
        '🥂',
        'Cuisiné le 1er janvier.',
        'Respect.',
        'calendrier',
        1,
        _first(lambda dd: (dd.date.month, dd.date.day) == (1, 1)),
        R,
        icon='clinking-glasses',
        hints=(
            'Un jour où presque tout le monde dort.',
            'Le premier midi de l’année aux fourneaux.',
            'Cuisiner un plat le 1er janvier.',
        ),
    ),
    _b(
        'vendredi_13',
        'Superstitieux ? Non.',
        '🐈‍⬛',
        'Cuisiné un vendredi 13.',
        'Le chat noir a eu sa part.',
        'calendrier',
        1,
        _first(lambda dd: dd.date.day == 13 and dd.date.weekday() == 4),
        R,
        icon='black-cat',
        hints=(
            'Certains jours portent malheur. Pas à table.',
            'Un vendredi pas comme les autres.',
            'Cuisiner un vendredi 13.',
        ),
    ),
    _b(
        'raclette_ete',
        'Raclette en juillet',
        '🫕',
        'Raclette, fondue ou tartiflette entre juin et août.',
        'Les règles sont faites pour être fondues.',
        'calendrier',
        1,
        _first(lambda dd: dd.date.month in (6, 7, 8) and dd.matches(RACLETTE)),
        TR,
        icon='fondue',
        hints=(
            'Le fromage fondu n’a pas de saison.',
            'Un plat d’hiver sous le soleil de l’été.',
            'Raclette, fondue ou tartiflette entre juin et août.',
        ),
    ),
    _b(
        'soupe_aout',
        'Soupe en août',
        '🥵',
        'Une soupe chaude en juillet ou en août.',
        'Le thermomètre dit non, ta cuillère dit oui.',
        'calendrier',
        1,
        _first(lambda dd: dd.date.month in (7, 8) and dd.matches(SOUPE) and not dd.matches(SOUPE_FROIDE)),
        R,
        icon='hot-face',
        hints=(
            'Un plat d’hiver en plein été.',
            'Quand la cuillère est chaude et le soleil aussi.',
            'Une soupe chaude en juillet ou en août.',
        ),
    ),
    _b(
        'bbq_hiver',
        'Barbecue de décembre',
        '🧣',
        'Barbecue ou plancha entre décembre et février.',
        'Gants de cuisine ou gants de ski ?',
        'calendrier',
        1,
        _first(lambda dd: dd.date.month in (12, 1, 2) and (dd.matches(BBQ) or 'barbecue_plancha' in dd.equipment)),
        TR,
        icon='scarf',
        hints=(
            'Les braises ne connaissent pas l’hiver.',
            'Faire des grillades en doudoune.',
            'Barbecue ou plancha entre décembre et février.',
        ),
    ),
    _b(
        'buche_hors_saison',
        'Bûche de Pâques',
        '🪵',
        'Une bûche cuisinée en dehors des fêtes de fin d’année.',
        'Noël, c’est quand tu veux.',
        'calendrier',
        1,
        _first(lambda dd: dd.date.month != 12 and not (dd.date.month == 1 and dd.date.day <= 6) and dd.matches(BUCHE)),
        TR,
        icon='wood',
        hints=(
            'Un dessert qui se trompe de saison.',
            'Noël au printemps ou en plein été.',
            'Une bûche en dehors des fêtes de fin d’année.',
        ),
    ),
    _b(
        'poisson_avril',
        'Poisson d’avril',
        '🐟',
        'Poisson ou fruits de mer le 1er avril.',
        'Ce n’est pas une blague, c’est un dîner.',
        'calendrier',
        1,
        _first(
            lambda dd: (
                (dd.date.month, dd.date.day) == (4, 1)
                and ('poissons_fruits_de_mer' in dd.groups or dd.has_ingredient(POISSON) or dd.matches(POISSON))
            )
        ),
        R,
        icon='fish',
        secret=True,
        mystery='introuvable',
    ),
    _b(
        'bissextile',
        'Le 29 février',
        '🐸',
        'Cuisiné un 29 février.',
        'Ça n’arrive que tous les quatre ans. Toi aussi.',
        'calendrier',
        1,
        _first(lambda dd: (dd.date.month, dd.date.day) == (2, 29)),
        L,
        icon='frog',
        secret=True,
        mystery='introuvable',
    ),
    _b(
        'tchikiversaire',
        'Joyeux Tchikiversaire',
        '🎉',
        'Cuisiné le jour anniversaire de ton inscription.',
        'Un an de cuisine ensemble. On s’embrasse ?',
        'calendrier',
        1,
        _h(_tchikiversaire),
        R,
        icon='party-popper',
        secret=True,
        mystery='introuvable',
    ),
    _b(
        'nuit_citrouilles',
        'La nuit des citrouilles',
        '🎃',
        'Courge, potiron ou citrouille le 31 octobre.',
        'Même les fantômes ont faim.',
        'calendrier',
        1,
        _first(
            lambda dd: (dd.date.month, dd.date.day) == (10, 31) and (dd.matches(COURGE) or dd.has_ingredient(COURGE))
        ),
        R,
        icon='jack-o-lantern',
        hints=(
            'Une nuit où les légumes font peur.',
            'Fin octobre, quelque chose d’orange.',
            'Une courge le 31 octobre.',
        ),
    ),
    _b(
        'douze_mois',
        'Douze mois sur douze',
        '🗓️',
        'Au moins un plat chaque mois, 12 mois d’affilée.',
        'Même en août. Surtout en août.',
        'calendrier',
        12,
        _h(_douze_mois),
        R,
        icon='spiral-calendar',
    ),
    _b(
        'feu_artifice',
        'Feu d’artifice',
        '🎆',
        'Cuisiné le 14 juillet pour 4 personnes ou plus.',
        'Le bouquet final, c’était le dessert.',
        'calendrier',
        1,
        _first(lambda dd: (dd.date.month, dd.date.day) == (7, 14) and dd.people >= 4),
        R,
        icon='fireworks',
    ),
    # --- Les heures ---
    _b(
        'minuit',
        'Fringale de minuit',
        '🌙',
        'Un plat noté cuisiné entre 23 h et 3 h.',
        'Les meilleures idées arrivent tard.',
        'heures',
        1,
        _first(lambda dd: _local_hour_on(dd, 23, 24, next_day=False) or _local_hour_on(dd, 0, 3, next_day=True)),
        R,
        icon='crescent-moon',
        hints=(
            'Certains plats se cuisinent à la lumière du frigo.',
            'Quand la nuit est bien avancée.',
            'Un plat noté cuisiné entre 23 h et 3 h.',
        ),
    ),
    _b(
        'leve_tot',
        'Lève-tôt',
        '🌅',
        'Un petit-déj cuisiné avant 7 h.',
        'Le soleil n’était même pas prêt.',
        'heures',
        1,
        _first(lambda dd: dd.meal_time == 'breakfast' and _slot_between(dd, 4, 7)),
        R,
        icon='sunrise',
    ),
    _b(
        'gouter_dimanche',
        'Goûter du dimanche',
        '🍰',
        'Un gâteau ou un dessert un dimanche entre 15 h et 18 h.',
        'L’heure sacrée.',
        'heures',
        1,
        _first(lambda dd: dd.date.weekday() == 6 and dd.matches(DESSERT) and _slot_between(dd, 15, 18)),
        icon='shortcake',
    ),
    _b(
        'sept_jours_sept',
        '7 jours sur 7',
        '📅',
        'Au moins un plat cuisiné chaque jour d’une même semaine.',
        'Ta cuisine ne prend pas de jour off.',
        'heures',
        7,
        _h(_seven_days),
        R,
        icon='calendar',
    ),
    _b(
        'trois_repas',
        'Grand chef d’un jour',
        '🧑‍🍳',
        'Petit-déj, déjeuner et dîner cuisinés le même jour.',
        'Trois services, zéro réservation.',
        'heures',
        1,
        _h(_three_meals),
        R,
        icon='cook',
    ),
    _b(
        'brunch_dimanche',
        'Brunch du dimanche',
        '🥯',
        'Un dimanche, un plat avec des œufs, entre 10 h et 14 h.',
        'Ni petit-déj, ni déjeuner. Parfait.',
        'heures',
        1,
        _first(_brunch_dimanche),
        icon='bagel',
    ),
    _b(
        'midi_pile',
        'Midi pile',
        '🕛',
        'Plat noté cuisiné à 12 h 00 pile.',
        'Le clocher et toi, même combat.',
        'heures',
        1,
        _first(_midi_pile),
        TR,
        icon='twelve-o-clock',
        secret=True,
        mystery='introuvable',
    ),
    _b(
        'oiseau_de_nuit',
        'Oiseau de nuit',
        '🦉',
        '3 plats saisis après 22 h dans la même semaine.',
        'La cuisine ferme quand tu le décides.',
        'heures',
        3,
        _h(_oiseau_de_nuit),
        R,
        icon='owl',
    ),
    # --- Les défis absurdes ---
    _b(
        'arc_en_ciel',
        'Arc-en-ciel',
        '🌈',
        'Des végétaux rouges, orange, jaunes, verts et violets la même semaine.',
        'Ton assiette a plus de couleurs que ta garde-robe.',
        'defis',
        5,
        _h(_rainbow),
        R,
        icon='rainbow',
    ),
    _b(
        'alphabet_10',
        'Abécédaire',
        '🔤',
        'Des plats qui commencent par 10 lettres différentes.',
        'A comme appétit, B comme bravo.',
        'defis',
        10,
        _alphabet(10),
        icon='input-latin-letters',
    ),
    _b(
        'alphabet_26',
        'De A à Z',
        '🔠',
        'Des plats qui commencent par chacune des 26 lettres.',
        'Même le W. Surtout le W.',
        'defis',
        26,
        _alphabet(26),
        L,
        icon='input-latin-uppercase',
        hints=(
            'Une histoire de lettres.',
            '26 plats, 26 initiales de l’alphabet.',
            'Cuisiner un plat pour chaque lettre de A à Z.',
        ),
    ),
    _b(
        'minimaliste',
        'Le minimaliste',
        '🥄',
        'Une recette de 3 ingrédients ou moins.',
        'Moins, c’est plus. Surtout moins de vaisselle.',
        'defis',
        1,
        _first(lambda dd: any(2 <= n <= 3 for n in dd.recipe_sizes)),
        icon='spoon',
    ),
    _b(
        'marathon',
        'Le marathon',
        '🏃',
        'Une recette de 20 ingrédients ou plus.',
        'Ton plan de travail s’en souvient encore.',
        'defis',
        1,
        _first(lambda dd: any(n >= 20 for n in dd.recipe_sizes)),
        R,
        icon='running-person',
        hints=(
            'Une liste de courses qui n’en finit pas.',
            'Beaucoup d’ingrédients sur le plan de travail.',
            'Une recette comptant au moins 20 ingrédients.',
        ),
    ),
    _b(
        'patience',
        'Patience d’ange',
        '⏳',
        'Une cuisson mesurée de 4 h ou plus en mode cuisine.',
        'Les bonnes choses prennent du temps. Beaucoup.',
        'defis',
        1,
        _measured(lambda m: 240 <= m <= 720),
        R,
        icon='hourglass',
        hints=(
            'Il faut savoir attendre.',
            'Plusieurs heures au four ou sur le feu.',
            'Une cuisson de 4 h ou plus en mode cuisine.',
        ),
    ),
    _b(
        'express',
        'Éclair',
        '⚡',
        'Un plat cuisiné en 15 minutes ou moins en mode cuisine.',
        'Plus rapide qu’une livraison.',
        'defis',
        1,
        _measured(lambda m: 1 <= m <= 15),
        icon='high-voltage',
    ),
    _b(
        'tour_monde_semaine',
        'Tour du monde en 7 jours',
        '🧭',
        '5 cuisines du monde différentes en 7 jours.',
        'Phileas Fogg est jaloux.',
        'defis',
        5,
        _cuisines_in_days(5, 7),
        TR,
        icon='compass',
    ),
    _b(
        'vampire',
        'Anti-vampire',
        '🧄',
        'De l’ail dans 20 plats.',
        'Aucun vampire ne t’approchera. Tes amis, on verra.',
        'defis',
        20,
        _count(lambda dd: dd.has_ingredient(AIL), 20),
        icon='garlic',
    ),
    _b(
        'breton',
        'Breton de cœur',
        '🧈',
        'Du beurre dans 30 plats.',
        'Le beurre n’est pas un ingrédient, c’est un mode de vie.',
        'defis',
        30,
        _count(lambda dd: dd.has_ingredient(BEURRE), 30),
        icon='butter',
    ),
    _b(
        'trois_fromages',
        'Trois fromages, sinon rien',
        '🧀',
        '3 fromages différents dans un même plat.',
        'Pourquoi choisir ?',
        'defis',
        1,
        _first(lambda dd: _cheeses(dd) >= 3),
        R,
        icon='cheese',
    ),
    _b(
        'tout_orange',
        'Monochrome orange',
        '🟠',
        'Un plat dont tous les végétaux sont orange.',
        'Très cohérent. Très orange.',
        'defis',
        1,
        _first(_all_orange),
        TR,
        icon='orange-circle',
        hints=(
            'Une seule couleur dans l’assiette.',
            'Carotte, potiron, patate douce...',
            'Un plat dont tous les végétaux sont orange.',
        ),
    ),
    _b(
        'trente_vegetaux',
        'Le potager',
        '🥕',
        '30 végétaux différents dans le même mois.',
        'Ton frigo ressemble à un marché.',
        'defis',
        30,
        _distinct_plants_per(lambda d: (d.year, d.month), 30),
        R,
        icon='carrot',
    ),
    _b(
        'cent_vegetaux',
        'La forêt',
        '🌳',
        '100 végétaux différents dans la même année.',
        'Tu cuisines ou tu fais de la botanique ?',
        'defis',
        100,
        _distinct_plants_per(lambda d: d.year, 100),
        L,
        icon='deciduous-tree',
    ),
    _b(
        'piment',
        'Langue de feu',
        '🌶️',
        'Du piment dans 10 plats.',
        'Toi, tu appelles ça « relevé ».',
        'defis',
        10,
        _count(lambda dd: dd.has_ingredient(PIMENT), 10),
        icon='hot-pepper',
    ),
    _b(
        'sans_viande_mois',
        'Le mois vert',
        '🥦',
        '30 jours sans viande ni poisson, avec au moins 8 plats notés.',
        'Un mois entier côté potager.',
        'defis',
        1,
        _h(_veggie_month),
        R,
        icon='broccoli',
    ),
    _b(
        'alliteration',
        'Allitération',
        '🅿️',
        '5 plats différents qui commencent par la même lettre, dans la même semaine.',
        'Pâtes, pizza, poulet, purée, paella.',
        'defis',
        5,
        _h(_alliteration),
        R,
        icon='letter-p',
        hints=(
            'Une semaine qui bégaie.',
            'Regarde la première lettre de tes plats.',
            '5 plats, même initiale, même semaine.',
        ),
    ),
    _b(
        'rien_ne_se_perd',
        'Rien ne se perd',
        '♻️',
        '2 jours de suite, 2 plats différents qui partagent au moins 4 ingrédients.',
        'Les restes d’hier, l’inspiration d’aujourd’hui.',
        'defis',
        1,
        _h(_rien_ne_se_perd),
        R,
        icon='recycling',
        hints=(
            'Hier n’a pas dit son dernier mot.',
            'Ce qui reste d’un jour sert le lendemain.',
            '2 jours de suite, 2 plats, 4 ingrédients en commun.',
        ),
    ),
    _b(
        'poupees_russes',
        'Poupées russes',
        '🪆',
        '3 sous-recettes faites maison dans un même plat.',
        'Une recette dans une recette dans une recette.',
        'defis',
        1,
        _first(lambda dd: dd.made_sub_recipes >= 3),
        TR,
        icon='nesting-dolls',
        hints=(
            'Une recette peut en cacher une autre.',
            'Et celle-là, une autre encore.',
            '3 préparations maison dans un même plat.',
        ),
    ),
    _b(
        'herboriste',
        'L’herboriste',
        '🌿',
        '5 herbes ou épices différentes dans un même plat.',
        'Ça sent bon jusque dans l’escalier.',
        'defis',
        1,
        _h(_herboriste),
        R,
        icon='herb',
    ),
    _b(
        'pyramide',
        'La pyramide',
        '🔺',
        '6 groupes d’aliments différents dans un même plat.',
        'Tout le marché dans une seule assiette.',
        'defis',
        1,
        _h(_pyramide),
        R,
        icon='pyramid',
    ),
    _b(
        'cinq_continents',
        'Cinq continents',
        '🗺️',
        'Au moins une cuisine d’Europe, d’Afrique, d’Asie, des Amériques et du Moyen-Orient.',
        'Ta cuisine n’a pas de frontières.',
        'defis',
        5,
        _h(_cinq_continents),
        R,
        icon='world-map',
    ),
    _b(
        'team_patate',
        'Team patate',
        '🥔',
        'Pomme de terre dans 20 plats.',
        'Frite, purée, gratin : tu les aimes toutes.',
        'defis',
        20,
        _count(lambda dd: dd.has_ingredient(PATATE) or dd.matches(PATATE), 20),
        icon='potato',
    ),
    _b(
        'al_dente',
        'Al dente',
        '🍝',
        'Des pâtes dans 5 plats la même semaine.',
        'Quelque part en Italie, une nonna est fière.',
        'defis',
        5,
        _h(_al_dente),
        R,
        icon='spaghetti',
    ),
    # --- À plusieurs ---
    _b(
        'cantine',
        'La cantine',
        '🏫',
        'Un repas pour 12 personnes ou plus (ton foyer non compris).',
        'Il ne reste plus qu’à sortir les plateaux.',
        'a_plusieurs',
        1,
        _first(lambda dd: dd.people >= 12),
        TR,
        icon='school',
    ),
    _b(
        'hote_dimanche',
        'L’hôte du dimanche',
        '🍗',
        '4 dimanches avec des invités.',
        'Le dimanche, c’est chez toi.',
        'a_plusieurs',
        4,
        _h(_sundays_with_guests),
        R,
        icon='poultry-leg',
    ),
    _b(
        'influenceur',
        'Influenceur',
        '📣',
        'Tes plats refaits 5 fois par d’autres grâce à « À mon tour ».',
        'Tu lances des tendances.',
        'a_plusieurs',
        5,
        _h(lambda h: reached(h.inspired_others_dates, 5)),
        TR,
        icon='megaphone',
    ),
    _b(
        'copieur',
        'Le bon élève',
        '📝',
        '5 plats d’amis refaits avec « À mon tour ».',
        'Copier, c’est aussi cuisiner.',
        'a_plusieurs',
        5,
        _h(_friends_dishes_redone),
        icon='memo',
    ),
    _b(
        'miams_100',
        '100 miams',
        '😋',
        '100 miams reçus sur tes plats.',
        'Ça fait beaucoup de bouches qui salivent.',
        'a_plusieurs',
        100,
        _miams,
        R,
        icon='face-savoring-food',
    ),
    _b(
        'tablee_complete',
        'Tablée complète',
        '🪑',
        'Tout le monde a cuisiné dans ta tablée la même semaine.',
        'Tout le monde aux fourneaux.',
        'a_plusieurs',
        1,
        _h(_table_complete),
        R,
        icon='chair',
    ),
    _b(
        'ouvre_bal',
        'Ouvre le bal',
        '💃',
        'Premier ou première à cuisiner de ta tablée 4 semaines de suite.',
        'Il faut bien que quelqu’un commence.',
        'a_plusieurs',
        4,
        _h(_opens_ball),
        TR,
        icon='woman-dancing',
    ),
    _b(
        'inviteur',
        'Le rabatteur',
        '📨',
        '3 amis inscrits grâce à ton lien.',
        'Plus on est de fous, plus on mange.',
        'a_plusieurs',
        3,
        _h(lambda h: reached(h.invite_join_dates, 3)),
        R,
        icon='incoming-envelope',
    ),
    _b(
        'carton_plein',
        'Carton plein',
        '💯',
        '10 miams sur un même plat.',
        'Ton assiette a son fan-club.',
        'a_plusieurs',
        1,
        _h(_carton_plein),
        R,
        icon='hundred-points',
    ),
    _b(
        'tablee_connectee',
        'La tablée connectée',
        '📲',
        'Un repas avec 3 invités qui ont un compte (hors foyer).',
        'Tout le monde à table, tout le monde sur Tchikook.',
        'a_plusieurs',
        1,
        _h(_tablee_connectee),
        R,
        icon='mobile-phone',
    ),
    _b(
        'telepathie',
        'Télépathie',
        '🔮',
        'Un même plat cuisiné le même jour qu’un membre de ta tablée.',
        'Une même idée au même moment. Flippant.',
        'a_plusieurs',
        1,
        _h(lambda h: once(h.telepathie_date)),
        TR,
        icon='crystal-ball',
        secret=True,
        mystery='introuvable',
    ),
    _b(
        'bouche_a_oreille',
        'Le bouche-à-oreille',
        '🗣️',
        'Un de tes plats refait à deux niveaux de partage.',
        'Ta recette voyage plus loin que prévu.',
        'a_plusieurs',
        1,
        _h(lambda h: once(h.bouche_a_oreille_date)),
        L,
        icon='speaking-head',
        secret=True,
        mystery='introuvable',
    ),
    _b(
        'pique_assiette',
        'Pique-assiette',
        '🍴',
        'Invité ou invitée à 5 repas partagés.',
        'Ton talent : arriver pile pour le dessert.',
        'a_plusieurs',
        5,
        _h(lambda h: reached(h.accepted_invitations_dates, 5)),
        R,
        icon='fork-and-knife',
    ),
    _b(
        'debat_culinaire',
        'Débat culinaire',
        '💬',
        '10 commentaires d’autres cuisiniers sur un même plat.',
        'Tout le monde a un avis sur ton plat.',
        'a_plusieurs',
        1,
        _h(lambda h: once(h.debated_post_date)),
        TR,
        icon='speech-balloon',
    ),
    # --- Le matériel ---
    _b(
        'one_pot',
        'Une seule casserole',
        '🍲',
        '10 plats « one pot ».',
        'La vaisselle te dit merci.',
        'materiel',
        10,
        _count(lambda dd: dd.matches(ONE_POT), 10),
        R,
        icon='pot-of-food',
    ),
    _b(
        'airfryer',
        'Air fryer power',
        '🍟',
        '10 plats à l’airfryer.',
        'Le croustillant sans la friteuse.',
        'materiel',
        10,
        _count(lambda dd: 'airfryer' in dd.equipment or dd.matches(AIRFRYER), 10),
        icon='french-fries',
    ),
    _b(
        'sans_four',
        'Sans four, sans problème',
        '🔥',
        '20 plats sans four, dans une cuisine sans four.',
        'Qui a besoin d’un four, franchement ?',
        'materiel',
        20,
        _h(_without_oven),
        R,
        icon='fire',
    ),
    _b(
        'robot',
        'Le robot fait tout',
        '🤖',
        '10 plats au robot cuiseur.',
        'Toi, tu as surtout appuyé sur le bouton. Bravo.',
        'materiel',
        10,
        _count(lambda dd: 'robot_cuiseur' in dd.equipment or dd.matches(ROBOT), 10),
        icon='robot',
    ),
    _b(
        'fait_maison',
        'Fait maison',
        '🫙',
        '5 sous-recettes faites maison (pâte, sauce…).',
        'Acheter la pâte ? Jamais entendu parler.',
        'materiel',
        5,
        _h(_homemade),
        R,
        icon='jar',
    ),
    _b(
        'pilote_auto',
        'Pilote automatique',
        '🛫',
        '20 recettes suivies jusqu’au bout en mode cuisine.',
        'Étape par étape, comme un pro.',
        'materiel',
        20,
        _h(_pilote_auto),
        R,
        icon='airplane',
    ),
    _b(
        'chef_orchestre',
        'Chef d’orchestre',
        '⏲️',
        '3 minuteurs qui tournent en même temps.',
        'Trois minuteurs, zéro panique.',
        'materiel',
        1,
        _h(lambda h: once(h.concurrent_timers_date)),
        TR,
        icon='timer-clock',
        hints=(
            'Ça sonne de partout.',
            'Une cuisine, plusieurs comptes à rebours.',
            '3 minuteurs en même temps.',
        ),
    ),
    # --- Le carnet ---
    _b(
        'paparazzi',
        'Paparazzi des assiettes',
        '🤳',
        '30 plats en photo.',
        'Ton téléphone mange avant toi.',
        'carnet',
        30,
        _count(lambda dd: dd.photo, 30),
        icon='camera-flash',
    ),
    _b(
        'making_of',
        'Making-of',
        '🎬',
        'Un même plat avec une photo en cuisine et une photo au repas.',
        'Les coulisses, c’est toi la star.',
        'carnet',
        1,
        _h(lambda h: once(h.making_of_date)),
        R,
        icon='clapper-board',
    ),
    _b(
        'chasseur_recettes',
        'Le chasseur de recettes',
        '🧲',
        '10 recettes importées cuisinées.',
        'Internet est ton livre de cuisine.',
        'carnet',
        10,
        _count(lambda dd: dd.is_imported, 10),
        icon='magnet',
    ),
)
