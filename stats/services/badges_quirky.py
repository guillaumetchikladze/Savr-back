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


# ---------------------------------------------------------------------------
# Registre
# ---------------------------------------------------------------------------
def _b(code, title, emoji, description, phrase, family, target, metric, rarity=TIER_COMMUN, hint=''):
    return BadgeDef(
        code=code,
        title=title,
        emoji=emoji,
        description=description,
        category=family,
        target=target,
        metric=metric,
        family=family,
        phrase=phrase,
        secret=bool(hint),
        hint=hint,
        rarity_guess=rarity,
    )


R, TR, L = TIER_RARE, TIER_TRES_RARE, TIER_LEGENDAIRE

QUIRKY_BADGES: tuple[BadgeDef, ...] = (
    # --- La même recette, encore ---
    _b('repeat_5', 'La fétiche', '💘', 'Le même plat cuisiné 5 fois.', 'Tu sais ce que tu aimes.',
       'meme_recette', 5, _repeat(5)),
    _b('repeat_10', 'Obsession', '🌀', 'Le même plat cuisiné 10 fois.', 'À ce stade, c’est une relation.',
       'meme_recette', 10, _repeat(10), R),
    _b('repeat_25', 'Le plat signature', '✍️', 'Le même plat cuisiné 25 fois.', 'On devrait lui donner ton nom.',
       'meme_recette', 25, _repeat(25), TR),
    _b('repeat_3_days', 'Restes assumés', '🥡', 'Le même plat 3 jours d’affilée.',
       'Le lendemain c’est meilleur, le surlendemain aussi.', 'meme_recette', 3, _repeat_days(3), R),
    _b('dish_anniversary', 'Joyeux anniversaire, le plat', '🎂',
       'Le même plat cuisiné à un an d’écart, jour pour jour (à un jour près).', 'Une tradition est née.',
       'meme_recette', 1, _h(_anniversary), TR, hint='Certains plats méritent qu’on souffle leurs bougies.'),
    _b('perfectionist', 'Le perfectionniste', '📐', 'Le même plat 4 fois en un mois, avec une photo à chaque fois.',
       'Celle-ci, c’est la bonne. Ou la suivante.', 'meme_recette', 1, _h(_perfectionist), TR,
       hint='Refaire, photographier, recommencer.'),
    # --- Le calendrier ---
    _b('chandeleur', 'Chandeleur réglementaire', '🥞', 'Des crêpes le 2 février.',
       'La pièce dans une main, la poêle dans l’autre.', 'calendrier', 1,
       _first(lambda dd: (dd.date.month, dd.date.day) == (2, 2) and dd.matches(CREPES)), R),
    _b('galette', 'Roi ou reine', '👑', 'Une galette des rois cuisinée en janvier.',
       'La fève, c’était toi. Évidemment.', 'calendrier', 1,
       _first(lambda dd: dd.date.month == 1 and dd.matches(GALETTE_ROIS))),
    _b('mardi_gras', 'Mardi gras', '🎭', 'Des beignets ou des crêpes le jour de mardi gras.',
       'Le déguisement est facultatif, la friture non.', 'calendrier', 1,
       _first(lambda dd: dd.date == easter(dd.date.year) - timedelta(days=47) and dd.matches(BEIGNETS)), R),
    _b('paques', 'Chasseur d’œufs', '🐣', 'Du chocolat le week-end de Pâques.',
       'Les cloches sont passées par ta cuisine.', 'calendrier', 1,
       _first(lambda dd: abs((dd.date - easter(dd.date.year)).days) <= 1
              and (dd.matches(CHOCOLAT) or dd.has_ingredient(CHOCOLAT))), R),
    _b('reveillon', 'Réveillonneur', '🎄', 'Cuisiné le 24 ou le 31 décembre pour 4 personnes ou plus.',
       'Le vrai cadeau, c’était le repas.', 'calendrier', 1,
       _first(lambda dd: (dd.date.month, dd.date.day) in ((12, 24), (12, 31)) and dd.people >= 4), R),
    _b('jour_de_l_an', 'Courageux du 1er janvier', '🥂', 'Cuisiné le 1er janvier.', 'Respect.', 'calendrier', 1,
       _first(lambda dd: (dd.date.month, dd.date.day) == (1, 1)), R, hint='Un jour où presque tout le monde dort.'),
    _b('vendredi_13', 'Superstitieux ? Non.', '🐈‍⬛', 'Cuisiné un vendredi 13.', 'Le chat noir a eu sa part.',
       'calendrier', 1, _first(lambda dd: dd.date.day == 13 and dd.date.weekday() == 4), R,
       hint='Certains jours portent malheur. Pas à table.'),
    _b('raclette_ete', 'Raclette en juillet', '🫕', 'Raclette, fondue ou tartiflette entre juin et août.',
       'Les règles sont faites pour être fondues.', 'calendrier', 1,
       _first(lambda dd: dd.date.month in (6, 7, 8) and dd.matches(RACLETTE)), TR,
       hint='Le fromage fondu n’a pas de saison.'),
    _b('soupe_aout', 'Soupe en août', '🥵', 'Une soupe chaude en juillet ou en août.',
       'Le thermomètre dit non, ta cuillère dit oui.', 'calendrier', 1,
       _first(lambda dd: dd.date.month in (7, 8) and dd.matches(SOUPE) and not dd.matches(SOUPE_FROIDE)), R,
       hint='Un plat d’hiver en plein été.'),
    _b('bbq_hiver', 'Barbecue de décembre', '🧣', 'Barbecue ou plancha entre décembre et février.',
       'Gants de cuisine ou gants de ski ?', 'calendrier', 1,
       _first(lambda dd: dd.date.month in (12, 1, 2) and (dd.matches(BBQ) or 'barbecue_plancha' in dd.equipment)),
       TR, hint='Les braises ne connaissent pas l’hiver.'),
    _b('buche_hors_saison', 'Bûche de Pâques', '🪵', 'Une bûche cuisinée en dehors des fêtes de fin d’année.',
       'Noël, c’est quand tu veux.', 'calendrier', 1,
       _first(lambda dd: dd.date.month != 12 and not (dd.date.month == 1 and dd.date.day <= 6)
              and dd.matches(BUCHE)), TR, hint='Un dessert qui se trompe de saison.'),
    # --- Les heures ---
    _b('minuit', 'Fringale de minuit', '🌙', 'Un plat noté cuisiné entre 23 h et 3 h.',
       'Les meilleures idées arrivent tard.', 'heures', 1,
       _first(lambda dd: _local_hour_on(dd, 23, 24, next_day=False) or _local_hour_on(dd, 0, 3, next_day=True)),
       R, hint='Certains plats se cuisinent à la lumière du frigo.'),
    _b('leve_tot', 'Lève-tôt', '🌅', 'Un petit-déj cuisiné avant 7 h.', 'Le soleil n’était même pas prêt.',
       'heures', 1, _first(lambda dd: dd.meal_time == 'breakfast' and _slot_between(dd, 4, 7)), R),
    _b('gouter_dimanche', 'Goûter du dimanche', '🍰', 'Un gâteau ou un dessert un dimanche entre 15 h et 18 h.',
       'L’heure sacrée.', 'heures', 1,
       _first(lambda dd: dd.date.weekday() == 6 and dd.matches(DESSERT) and _slot_between(dd, 15, 18))),
    _b('sept_jours_sept', '7 jours sur 7', '📅', 'Au moins un plat cuisiné chaque jour d’une même semaine.',
       'Ta cuisine ne prend pas de jour off.', 'heures', 7, _h(_seven_days), R),
    _b('trois_repas', 'Grand chef d’un jour', '🧑‍🍳', 'Petit-déj, déjeuner et dîner cuisinés le même jour.',
       'Trois services, zéro réservation.', 'heures', 1, _h(_three_meals), R),
    # --- Les défis absurdes ---
    _b('arc_en_ciel', 'Arc-en-ciel', '🌈', 'Des végétaux rouges, orange, jaunes, verts et violets la même semaine.',
       'Ton assiette a plus de couleurs que ta garde-robe.', 'defis', 5, _h(_rainbow), R),
    _b('alphabet_10', 'Abécédaire', '🔤', 'Des plats qui commencent par 10 lettres différentes.',
       'A comme appétit, B comme bravo.', 'defis', 10, _alphabet(10)),
    _b('alphabet_26', 'De A à Z', '🔠', 'Des plats qui commencent par chacune des 26 lettres.',
       'Même le W. Surtout le W.', 'defis', 26, _alphabet(26), L, hint='Une histoire de lettres.'),
    _b('minimaliste', 'Le minimaliste', '🥄', 'Une recette de 3 ingrédients ou moins.',
       'Moins, c’est plus. Surtout moins de vaisselle.', 'defis', 1,
       _first(lambda dd: any(2 <= n <= 3 for n in dd.recipe_sizes))),
    _b('marathon', 'Le marathon', '🏃', 'Une recette de 20 ingrédients ou plus.',
       'Ton plan de travail s’en souvient encore.', 'defis', 1,
       _first(lambda dd: any(n >= 20 for n in dd.recipe_sizes)), R, hint='Une liste de courses qui n’en finit pas.'),
    _b('patience', 'Patience d’ange', '⏳', 'Une cuisson mesurée de 4 h ou plus en mode cuisine.',
       'Les bonnes choses prennent du temps. Beaucoup.', 'defis', 1,
       _measured(lambda m: 240 <= m <= 720), R, hint='Il faut savoir attendre.'),  # > 12 h : minuteur oublié
    _b('express', 'Éclair', '⚡', 'Un plat cuisiné en 15 minutes ou moins en mode cuisine.',
       'Plus rapide qu’une livraison.', 'defis', 1, _measured(lambda m: 1 <= m <= 15)),
    _b('tour_monde_semaine', 'Tour du monde en 7 jours', '🧭', '5 cuisines du monde différentes en 7 jours.',
       'Phileas Fogg est jaloux.', 'defis', 5, _cuisines_in_days(5, 7), TR),
    _b('vampire', 'Anti-vampire', '🧄', 'De l’ail dans 20 plats.',
       'Aucun vampire ne t’approchera. Tes amis, on verra.', 'defis', 20, _count(lambda dd: dd.has_ingredient(AIL), 20)),
    _b('breton', 'Breton de cœur', '🧈', 'Du beurre dans 30 plats.',
       'Le beurre n’est pas un ingrédient, c’est un mode de vie.', 'defis', 30,
       _count(lambda dd: dd.has_ingredient(BEURRE), 30)),
    _b('trois_fromages', 'Trois fromages, sinon rien', '🧀', '3 fromages différents dans un même plat.',
       'Pourquoi choisir ?', 'defis', 1, _first(lambda dd: _cheeses(dd) >= 3), R),
    _b('tout_orange', 'Monochrome orange', '🟠', 'Un plat dont tous les végétaux sont orange.',
       'Très cohérent. Très orange.', 'defis', 1, _first(_all_orange), TR, hint='Une seule couleur dans l’assiette.'),
    _b('trente_vegetaux', 'Le potager', '🥕', '30 végétaux différents dans le même mois.',
       'Ton frigo ressemble à un marché.', 'defis', 30, _distinct_plants_per(lambda d: (d.year, d.month), 30), R),
    _b('cent_vegetaux', 'La forêt', '🌳', '100 végétaux différents dans la même année.',
       'Tu cuisines ou tu fais de la botanique ?', 'defis', 100, _distinct_plants_per(lambda d: d.year, 100), L),
    _b('piment', 'Langue de feu', '🌶️', 'Du piment dans 10 plats.', 'Toi, tu appelles ça « relevé ».',
       'defis', 10, _count(lambda dd: dd.has_ingredient(PIMENT), 10)),
    _b('sans_viande_mois', 'Le mois vert', '🥦', '30 jours sans viande ni poisson, avec au moins 8 plats notés.',
       'Un mois entier côté potager.', 'defis', 1, _h(_veggie_month), R),
    # --- À plusieurs ---
    _b('cantine', 'La cantine', '🏫', 'Un repas pour 12 personnes ou plus (ton foyer non compris).',
       'Il ne reste plus qu’à sortir les plateaux.', 'a_plusieurs', 1, _first(lambda dd: dd.people >= 12), TR),
    _b('hote_dimanche', 'L’hôte du dimanche', '🍗', '4 dimanches avec des invités.',
       'Le dimanche, c’est chez toi.', 'a_plusieurs', 4, _h(_sundays_with_guests), R),
    _b('influenceur', 'Influenceur', '📣', 'Tes plats refaits 5 fois par d’autres grâce à « À mon tour ».',
       'Tu lances des tendances.', 'a_plusieurs', 5, _h(lambda h: reached(h.inspired_others_dates, 5)), TR),
    _b('copieur', 'Le bon élève', '📝', '5 plats d’amis refaits avec « À mon tour ».',
       'Copier, c’est aussi cuisiner.', 'a_plusieurs', 5, _h(_friends_dishes_redone)),
    _b('miams_100', '100 miams', '😋', '100 miams reçus sur tes plats.',
       'Ça fait beaucoup de bouches qui salivent.', 'a_plusieurs', 100, _miams, R),
    _b('tablee_complete', 'Tablée complète', '🪑', 'Tout le monde a cuisiné dans ta tablée la même semaine.',
       'Tout le monde aux fourneaux.', 'a_plusieurs', 1, _h(_table_complete), R),
    _b('ouvre_bal', 'Ouvre le bal', '💃', 'Premier ou première à cuisiner de ta tablée 4 semaines de suite.',
       'Il faut bien que quelqu’un commence.', 'a_plusieurs', 4, _h(_opens_ball), TR),
    _b('inviteur', 'Le rabatteur', '📨', '3 amis inscrits grâce à ton lien.',
       'Plus on est de fous, plus on mange.', 'a_plusieurs', 3, _h(lambda h: reached(h.invite_join_dates, 3)), R),
    # --- Le matériel ---
    _b('one_pot', 'Une seule casserole', '🍲', '10 plats « one pot ».', 'La vaisselle te dit merci.',
       'materiel', 10, _count(lambda dd: dd.matches(ONE_POT), 10), R),
    _b('airfryer', 'Air fryer power', '🍟', '10 plats à l’airfryer.', 'Le croustillant sans la friteuse.',
       'materiel', 10, _count(lambda dd: 'airfryer' in dd.equipment or dd.matches(AIRFRYER), 10)),
    _b('sans_four', 'Sans four, sans problème', '🔥', '20 plats sans four, dans une cuisine sans four.',
       'Qui a besoin d’un four, franchement ?', 'materiel', 20, _h(_without_oven), R),
    _b('robot', 'Le robot fait tout', '🤖', '10 plats au robot cuiseur.',
       'Toi, tu as surtout appuyé sur le bouton. Bravo.', 'materiel', 10,
       _count(lambda dd: 'robot_cuiseur' in dd.equipment or dd.matches(ROBOT), 10)),
    _b('fait_maison', 'Fait maison', '🫙', '5 sous-recettes faites maison (pâte, sauce…).',
       'Acheter la pâte ? Jamais entendu parler.', 'materiel', 5, _h(_homemade), R),
)  # fmt: skip
