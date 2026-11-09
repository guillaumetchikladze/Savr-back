"""
« Plan de bataille » : dans quel ordre cuisiner plusieurs plats (repas à ≥ 2 recettes, batch cooking du panier).

Contrat front : `.claude-work/specs/battle-plan.md`. Tout est **déterministe** (aucun LLM requis) ; la reformulation
optionnelle (`battle_plan_polish`) ne touche jamais à l'ordre ni aux horaires.

Données exploitées (modèle `Step`) : `order`, `title`, `instruction`, `has_timer` / `timer_duration` (minutes),
`step_ingredients` ; `Recipe.prep_time` / `cook_time` pour estimer les durées absentes. Il n'existe ni champ
actif / passif, ni équipement, ni température : ils sont **déduits du texte** (mots-clés, durées, « 180 °C », « th. 6 »).

Algorithme :
1. Chaque étape → tâche : durée (minuteur > durée lue dans le texte > estimation depuis prep/cook), active (le cuisinier
   est pris) ou passive (four, repos, refroidissement, marinade, mijotage… : tourne en parallèle), ressource (`oven`,
   `hob`, `fridge`), température du four.
2. Contraintes : étapes d'une recette dans l'ordre (sauf « Pendant ce temps… » et le préchauffage, qui laissent
   l'étape suivante démarrer en même temps), un seul cuisinier (deux étapes actives ne se chevauchent pas), un seul
   four (deux cuissons à des températures différentes ne se chevauchent pas : conflit signalé).
3. Ordonnancement par liste (priorité = plus long chemin restant, passif compris : le gâteau qui doit refroidir part
   en premier), puis chaque recette est décalée au plus tard possible pour que tout soit prêt ensemble, et le
   préchauffage est placé juste avant sa cuisson.
4. Repos / marinades longs (≥ 4 h, « une nuit », marinade ≥ 2 h) → section « la veille » (hors durée totale).
5. Prépa commune : ingrédients (hors basiques) taillés dans ≥ 2 recettes → une seule ligne « Émince 3 oignons pour… ».
6. Sous-recettes faites maison (`parent_idx`) : une recette de plus, dont toutes les étapes finissent avant l'étape de
   la recette parente qui utilise la ligne (`used_ingredient_id` ou son nom dans le texte ; sinon sa 1re étape).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from recipes.services.equipment_inference import PREHEAT, step_resource
from stats.text import fold

MAX_RECIPES = 8
DEFAULT_ACTIVE_STEP_MIN = 5
OVERNIGHT_MIN = 240
MARINADE_DAY_BEFORE_MIN = 120
OVEN_TEMP_TOLERANCE = 10
SHARED_PREP_MAX = 8

DAY_SAME = 'same_day'
DAY_BEFORE = 'day_before'

# ---------------------------------------------------------------------------
# Lecture du texte d'une étape
# ---------------------------------------------------------------------------
_NUMBER_WORDS = {
    'une': 1,
    'un': 1,
    'deux': 2,
    'trois': 3,
    'quatre': 4,
    'cinq': 5,
    'six': 6,
    'sept': 7,
    'huit': 8,
    'neuf': 9,
    'dix': 10,
    'douze': 12,
    'quinze': 15,
    'vingt': 20,
    'trente': 30,
    'quarante': 40,
    'cinquante': 50,
}
_NUMBER_WORD_RE = re.compile(r'\b(' + '|'.join(_NUMBER_WORDS) + r')\s+(heures?|minutes?|h\b|min\b)')
_HALF_HOUR_RE = re.compile(r'(\d+)\s*h(?:eures?)?\s*et\s*demie')
_DURATION_RE = re.compile(
    r'(?P<a>\d+(?:[.,]\d+)?)\s*'
    r'(?:(?:à|a|-|–|ou)\s*(?P<b>\d+(?:[.,]\d+)?)\s*)?'
    r'(?P<unit>h(?:eures?)?(?![a-zà-ÿ])|min(?:ute)?s?(?![a-zà-ÿ])|mn(?![a-zà-ÿ])|sec(?:onde)?s?(?![a-zà-ÿ]))'
    r'(?:\s*(?P<m>\d{1,2})(?!\s*(?:°|degr)))?'
)
_WORD_DURATIONS = (
    (re.compile(r'\b(une\s+)?demi[- ]heure'), 30),
    (re.compile(r"\bun\s+quart\s+d'?\s*heure"), 15),
    (re.compile(r'\bquelques\s+minutes'), 3),
)
_OVERNIGHT_RE = re.compile(r'\b(la veille|une nuit|toute la nuit|la nuit|pendant la nuit|nuit au frais)\b')
_TEMP_RE = re.compile(r'(\d{2,3})\s*(?:°|degr)')
_THERMOSTAT_RE = re.compile(r'\bth(?:ermostat)?\.?\s*(\d{1,2})\b')

# Sur texte « fold » (minuscules, sans accents ni ponctuation). Four / plaques / frigo : module partagé avec
# `Recipe.required_equipment` (`equipment_inference`).
_PREHEAT = PREHEAT
_KIND_PATTERNS = (  # ordre = priorité du « type » de temps passif
    ('marinate', re.compile(r'\bmarin(?:er|ez|ade|ades)\b')),
    ('rise', re.compile(r'\blaiss\w*\s+(?:\w+\s+)?(?:lever|pousser|gonfler)\b|\bdoubler de volume\b')),
    ('chill', re.compile(r'\b(refrigerat\w*|frigo|au frais|congelat\w*|laisser prendre)\b')),
    ('cool', re.compile(r'\b(refroidi\w*|tiedi\w*)\b')),
    ('rest', re.compile(r'\brepos\w*\b')),
    ('bake', re.compile(r'\benfourn\w*|\bau four\b|\bcui\w*\s+(?:\w+\s+){0,3}four\b|\bgratin(?:er|ez)\b')),
    ('soak', re.compile(r'\b(tremper|trempez|infuser|infusez|degorger|degorgez|decongeler)\b')),
    ('simmer', re.compile(r'\b(mijot\w*|fremi\w*|compot\w*|confire|brais\w*)\b')),
    ('cook', re.compile(r'\b(cuire|cuisez|cuisson|cuit|cuits|bouillir|rotir|vapeur)\b')),
)
_STRONG_PASSIVE = {'marinate', 'rise', 'chill', 'cool', 'rest', 'bake', 'soak'}
_ACTIVE_CONTINUOUS = re.compile(
    r'\b(sans cesse|sans arret|constamment|louche par louche|en fouettant|en remuant(?! de temps| regulierement))\b'
)
_ACTIVE_HANDS = re.compile(
    r'\b(faire sauter|faites sauter|faire revenir|faites revenir|faire dorer|faites dorer|saisir|saisissez|'
    r'poeler|poelez|retourn\w*|fouett\w*|remu\w*|melang\w*)\b'
)
_OCCASIONAL = re.compile(r'\bde temps (en temps|a autre)\b|\bregulierement\b')
_PARALLEL = re.compile(
    r'^(?:\w+\s+){0,2}(pendant ce temps|pendant que|en attendant|pendant la cuisson|durant la cuisson|'
    r'pendant le repos|pendant la cuisson)\b'
)

_DEFAULT_PASSIVE_MIN = {
    'marinate': 60,
    'rise': 60,
    'chill': 60,
    'cool': 30,
    'rest': 15,
    'soak': 20,
    'simmer': 30,
    'cook': 15,
    'bake': 30,
    'preheat': 10,
}
_PASSIVE_NOTE = {
    'bake': 'Au four, tu as les mains libres',
    'cool': 'Ça refroidit tout seul',
    'chill': 'Au frais, rien à faire',
    'rest': 'Temps de repos',
    'marinate': 'Ça marine tout seul',
    'rise': 'La pâte lève',
    'soak': 'Ça trempe',
    'simmer': 'Ça mijote, remue de temps en temps',
    'cook': 'Ça cuit, garde un œil dessus',
    'preheat': 'Préchauffe le four',
}
_WHILE_VERB = {
    'bake': 'cuit au four',
    'cool': 'refroidit',
    'chill': 'est au frais',
    'rest': 'repose',
    'marinate': 'marine',
    'rise': 'lève',
    'soak': 'trempe',
    'simmer': 'mijote',
    'cook': 'cuit',
}
_WHY_FIRST = {
    'bake': 'il passe {d} au four',
    'cool': 'il doit refroidir ({d})',
    'chill': 'il doit prendre au frais ({d})',
    'rest': 'il doit reposer ({d})',
    'marinate': 'il doit mariner ({d})',
    'rise': 'la pâte doit lever ({d})',
    'soak': 'il doit tremper ({d})',
    'simmer': 'il mijote {d}',
    'cook': 'il cuit {d}',
}


def _to_number(raw: str) -> float:
    return float(raw.replace(',', '.'))


def parse_duration_minutes(text: str) -> int | None:
    """Plus longue durée citée dans le texte, en minutes (« 1 h 30 », « 20 à 25 min », « une nuit » → 600)."""
    raw = (text or '').lower()
    if not raw.strip():
        return None
    raw = _NUMBER_WORD_RE.sub(lambda m: f'{_NUMBER_WORDS[m.group(1)]} {m.group(2)}', raw)
    found: list[float] = []
    for m in _HALF_HOUR_RE.finditer(raw):
        found.append(int(m.group(1)) * 60 + 30)
    for m in _DURATION_RE.finditer(raw):
        value = _to_number(m.group('b') or m.group('a'))
        unit = m.group('unit')
        if unit.startswith('h'):
            minutes = value * 60 + (int(m.group('m')) if m.group('m') else 0)
        elif unit.startswith('s'):
            minutes = value / 60
        else:
            minutes = value
        if 0 < minutes <= 72 * 60:
            found.append(minutes)
    for pattern, minutes in _WORD_DURATIONS:
        if pattern.search(raw):
            found.append(minutes)
    if _OVERNIGHT_RE.search(fold(raw)):
        found.append(600)
    if not found:
        return None
    return max(1, math.ceil(max(found)))


def parse_temperature(text: str) -> int | None:
    raw = (text or '').lower()
    for m in _TEMP_RE.finditer(raw):
        value = int(m.group(1))
        if 50 <= value <= 300:
            return value
    m = _THERMOSTAT_RE.search(raw)
    if m and 1 <= int(m.group(1)) <= 10:
        return int(m.group(1)) * 30
    return None


@dataclass
class StepFacts:
    duration: int | None
    active: bool
    kind: str  # 'active' ou type de temps passif (bake, cool, rest, …, preheat)
    resource: str | None
    temperature: int | None
    overnight: bool
    parallel: bool
    preheat_note: str = ''


def analyze_step(
    title: str, instruction: str, *, has_timer: bool = False, timer_duration: int | None = None
) -> StepFacts:
    text = f'{title or ""}. {instruction or ""}'.strip(' .')
    folded = fold(text)
    sentences = [s for s in re.split(r'(?<=[.!;])\s+', text) if fold(s)]
    preheat_sentences = [s for s in sentences if _PREHEAT.search(fold(s))]
    temperature = parse_temperature(text)
    is_preheat = bool(preheat_sentences) and len(preheat_sentences) == len(sentences)
    preheat_note = ''
    if preheat_sentences and not is_preheat:
        # « Préchauffer le four. Mélanger… » : étape active, le préchauffage n'est qu'une remarque.
        rest = [s for s in sentences if s not in preheat_sentences]
        t_preheat = parse_temperature(' '.join(preheat_sentences))
        preheat_note = 'Allume le four' + (f' à {t_preheat} °C' if t_preheat else '') + ' au passage'
        text = ' '.join(rest)
        folded = fold(text)
        temperature = parse_temperature(text)

    duration = timer_duration if has_timer and timer_duration and timer_duration > 0 else parse_duration_minutes(text)
    resource, oven = step_resource(folded)

    if is_preheat:
        return StepFacts(duration, False, 'preheat', 'oven', temperature, False, False)

    kind = next((k for k, pattern in _KIND_PATTERNS if pattern.search(folded)), None)
    continuous = bool(_ACTIVE_CONTINUOUS.search(folded))
    hands = bool(_ACTIVE_HANDS.search(folded)) and not _OCCASIONAL.search(folded)
    if kind in _STRONG_PASSIVE:
        passive = not continuous
    elif kind in ('simmer', 'cook'):
        passive = not continuous and not hands and (duration or 0) >= 8
    else:
        passive = False
    if passive and kind != 'bake' and oven:
        kind = kind if kind in ('cool', 'rest') else 'bake'
    overnight = passive and (
        bool(_OVERNIGHT_RE.search(folded))
        or (duration or 0) >= OVERNIGHT_MIN
        or (kind == 'marinate' and (duration or 0) >= MARINADE_DAY_BEFORE_MIN)
    )
    return StepFacts(
        duration=duration,
        active=not passive,
        kind=kind if passive else 'active',
        resource=resource,
        temperature=temperature if oven else None,
        overnight=overnight,
        parallel=bool(_PARALLEL.search(folded)),
        preheat_note=preheat_note,
    )


# ---------------------------------------------------------------------------
# Entrées (sans base de données : testables en isolation)
# ---------------------------------------------------------------------------
@dataclass
class StepInput:
    step_id: int | None
    order: int
    title: str = ''
    instruction: str = ''
    has_timer: bool = False
    timer_duration: int | None = None
    ingredient_ids: tuple[int, ...] = ()


@dataclass
class IngredientInput:
    ingredient_id: int
    name: str
    quantity: float
    unit: str
    is_staple: bool = False


@dataclass
class RecipeInput:
    recipe_id: int
    title: str
    prep_time: int | None = None
    cook_time: int | None = None
    servings: int = 4
    base_servings: int = 4
    steps: list[StepInput] = field(default_factory=list)
    ingredients: list[IngredientInput] = field(default_factory=list)
    image_url: str | None = None
    version: str = ''  # updated_at de la recette (clé de cache de la reformulation)
    # Sous-recette faite maison : index (dans la liste) de la recette qui l'utilise, et sa ligne d'ingrédient.
    # La liste doit placer une sous-recette avant sa parente.
    parent_idx: int | None = None
    used_line_id: int | None = None
    used_ingredient_id: int | None = None
    used_ingredient_name: str = ''


@dataclass
class KitchenResources:
    """
    Ressources réelles de la cuisine (« Ta cuisine ») : `burners` = feux utilisables en même temps (None = sans
    limite : cuisine inconnue), `ovens` (1 ou 2 températures en parallèle), four absent → l'airfryer le remplace
    (une fournée à la fois) s'il y en a un, sinon avertissement.
    """

    burners: int | None = None
    ovens: int = 1
    has_oven: bool = True
    airfryer: bool = False
    has_hob: bool = True
    induction: bool = False
    known: bool = False

    def payload(self) -> dict:
        return {
            'known': self.known,
            'burners': self.burners,
            'induction': self.induction,
            'ovens': self.ovens if self.has_oven else 0,
            'has_oven': self.has_oven,
            'airfryer': self.airfryer,
            'has_hob': self.has_hob,
        }


DEFAULT_KITCHEN = KitchenResources()


@dataclass
class Task:
    idx: int
    recipe_idx: int
    recipe_id: int
    step_id: int | None
    order: int
    label: str
    instruction: str
    duration: int
    estimated: bool
    active: bool
    kind: str
    resource: str | None
    temperature: int | None
    day: str
    fs: list[int] = field(default_factory=list)  # fin → début
    ss: list[int] = field(default_factory=list)  # début → début
    note: str = ''
    parallel: bool = False
    start: int | None = None
    alternative: str | None = None  # 'airfryer' : étape « four » faite à l'airfryer (pas de four)

    @property
    def end(self) -> int:
        return (self.start or 0) + self.duration

    @property
    def bakes(self) -> bool:
        """Cuisson « four » de la recette (préchauffage exclu), quel que soit l'appareil réel."""
        return self.resource == 'oven' and self.kind != 'preheat'

    @property
    def uses_oven(self) -> bool:
        return self.bakes and self.alternative is None

    @property
    def uses_airfryer(self) -> bool:
        return self.bakes and self.alternative == 'airfryer'


def short_label(title: str, instruction: str, limit: int = 110) -> str:
    base = (title or '').strip()
    if not base:
        first = re.split(r'(?<=[.!?])\s+', (instruction or '').strip(), maxsplit=1)[0]
        base = first.strip()
    if len(base) > limit:
        base = base[: limit - 1].rsplit(' ', 1)[0].rstrip(',;:') + '…'
    return base


def _recipe_tasks(recipe: RecipeInput, recipe_idx: int) -> list[Task]:
    steps = sorted(recipe.steps, key=lambda s: s.order)
    if not steps:  # recette sans étapes : préparation + cuisson d'après les temps de la fiche
        steps = [StepInput(None, 1, 'Préparation', f'Prépare {recipe.title}.')]
        if recipe.cook_time:
            mode = 'Cuisson' if recipe.cook_time >= 15 else 'Cuire'
            steps.append(StepInput(None, 2, mode, f'Cuisson {recipe.cook_time} min.'))
        facts = [analyze_step(s.title, s.instruction) for s in steps]
        facts[0].duration = max(recipe.prep_time or DEFAULT_ACTIVE_STEP_MIN, 1)
        if len(facts) > 1:
            facts[1].duration = recipe.cook_time
            facts[1].active = recipe.cook_time < 15
            facts[1].kind = 'active' if facts[1].active else 'cook'
        estimated_flags = [True] * len(steps)
    else:
        facts = [
            analyze_step(s.title, s.instruction, has_timer=s.has_timer, timer_duration=s.timer_duration) for s in steps
        ]
        estimated_flags = [f.duration is None for f in facts]
        _estimate_missing_durations(recipe, facts)

    tasks = []
    for step, fact, estimated in zip(steps, facts, estimated_flags, strict=True):
        tasks.append(
            Task(
                idx=-1,
                recipe_idx=recipe_idx,
                recipe_id=recipe.recipe_id,
                step_id=step.step_id,
                order=step.order,
                label=short_label(step.title, step.instruction),
                instruction=(step.instruction or '').strip(),
                duration=max(1, int(fact.duration or DEFAULT_ACTIVE_STEP_MIN)),
                estimated=estimated,
                active=fact.active,
                kind=fact.kind,
                resource=fact.resource,
                temperature=fact.temperature,
                day=DAY_BEFORE if fact.overnight else DAY_SAME,
                note=fact.preheat_note,
                parallel=fact.parallel,
            )
        )
    # Tout ce qui précède le dernier temps « de la veille » se fait la veille.
    last_overnight = max((i for i, t in enumerate(tasks) if t.day == DAY_BEFORE), default=-1)
    for t in tasks[: last_overnight + 1]:
        t.day = DAY_BEFORE
    return tasks


def _estimate_missing_durations(recipe: RecipeInput, facts: list[StepFacts]) -> None:
    unknown_active = [f for f in facts if f.duration is None and f.active]
    unknown_passive = [f for f in facts if f.duration is None and not f.active]
    if unknown_active:
        known = sum(f.duration or 0 for f in facts if f.active and f.duration)
        budget = (recipe.prep_time or 0) - known
        share = budget / len(unknown_active) if budget > 0 else DEFAULT_ACTIVE_STEP_MIN
        for f in unknown_active:
            f.duration = int(min(max(round(share), 2), 20))
    if unknown_passive:
        known = sum(
            f.duration or 0 for f in facts if not f.active and f.duration and f.kind in ('bake', 'cook', 'simmer')
        )
        cook_budget = (recipe.cook_time or 0) - known
        cooking = [f for f in unknown_passive if f.kind in ('bake', 'cook', 'simmer')]
        for f in unknown_passive:
            if f in cooking and cook_budget > 0:
                f.duration = max(5, round(cook_budget / len(cooking)))
            else:
                f.duration = _DEFAULT_PASSIVE_MIN.get(f.kind, 15)


def _link(tasks: list[Task]) -> None:
    """
    Dépendances intra-recette : chaque étape suit la précédente (fin → début), sauf « Pendant ce temps… »
    (début → début avec l'étape précédente). Un préchauffage ne retarde que la cuisson au four qui le suit.
    """
    by_recipe: dict[int, list[Task]] = {}
    for t in tasks:
        by_recipe.setdefault(t.recipe_idx, []).append(t)
    for seq in by_recipe.values():
        open_ends: list[int] = []  # étapes dont la fin conditionne la prochaine étape « normale »
        preheats: list[int] = []
        prev: Task | None = None
        for t in seq:
            if t.kind == 'preheat':
                t.fs.extend(open_ends)
                preheats.append(t.idx)
                prev = t
                continue
            if t.bakes:
                t.fs.extend(preheats)
                preheats = []
            if t.parallel and prev is not None and prev.idx in open_ends:
                t.ss.append(prev.idx)
                t.fs.extend(i for i in open_ends if i != prev.idx)
                open_ends = [*open_ends, t.idx]
            else:
                t.fs.extend(i for i in open_ends if i not in t.fs)
                open_ends = [t.idx]
            if prev is not None and prev.kind == 'preheat' and prev.idx not in t.fs:
                t.ss.append(prev.idx)
            prev = t


def _using_task(parent_tasks: list[Task], parent: RecipeInput, sub: RecipeInput) -> Task | None:
    """Étape de la parente qui utilise la sous-recette : ingrédient lié à l'étape ou cité, sinon la 1re."""
    if not parent_tasks:
        return None
    if sub.used_ingredient_id is not None:
        ingredient = IngredientInput(sub.used_ingredient_id, sub.used_ingredient_name, 0, '')
        steps = {s.step_id: s for s in parent.steps if s.step_id is not None}
        for t in parent_tasks:
            step = steps.get(t.step_id)
            if step is not None and _mentions(step, ingredient):
                return t
    return parent_tasks[0]


def _link_sub_recipes(tasks: list[Task], recipes: list[RecipeInput]) -> dict[int, Task]:
    """
    Dépendances sous-recette → parente (fin → début), seulement dans un même jour (la veille est faite avant).
    Renvoie {index de la sous-recette: étape de la parente qui l'attend}.
    """
    by_recipe: dict[int, list[Task]] = {}
    for t in tasks:
        by_recipe.setdefault(t.recipe_idx, []).append(t)
    waits: dict[int, Task] = {}
    for sub_idx, sub in enumerate(recipes):
        if sub.parent_idx is None or sub.parent_idx <= sub_idx:
            continue
        parent_tasks = by_recipe.get(sub.parent_idx, [])
        using = _using_task(parent_tasks, recipes[sub.parent_idx], sub)
        if using is None:
            continue
        sub_tasks = [t for t in by_recipe.get(sub_idx, []) if t.day == using.day]
        using.fs.extend(t.idx for t in sub_tasks if t.idx not in using.fs)
        waits[sub_idx] = using
    return waits


def _oven_compatible(a: int | None, b: int | None) -> bool:
    return a is None or b is None or abs(a - b) <= OVEN_TEMP_TOLERANCE


def _oven_groups(temperatures: list[int | None]) -> int:
    """Nombre de fours nécessaires pour des cuissons simultanées (températures à ±10 °C = même four)."""
    groups: list[int] = []
    has_any = False
    for temp in temperatures:
        has_any = True
        if temp is None or any(_oven_compatible(g, temp) for g in groups):
            continue
        groups.append(temp)
    return max(len(groups), 1 if has_any else 0)


def _load_ok(running: list[Task], kitchen: KitchenResources) -> bool:
    """Charge simultanée compatible avec la cuisine : fours (températures), airfryer (1), feux."""
    if _oven_groups([o.temperature for o in running if o.uses_oven]) > max(kitchen.ovens, 1):
        return False
    if sum(1 for o in running if o.uses_airfryer) > 1:
        return False
    if kitchen.burners is not None and sum(1 for o in running if o.resource == 'hob') > kitchen.burners:
        return False
    return True


def _uses_capacity(t: Task) -> bool:
    return t.uses_oven or t.uses_airfryer or t.resource == 'hob'


def _schedule(tasks: list[Task], kitchen: KitchenResources = DEFAULT_KITCHEN) -> None:
    """
    Ordonnancement par liste, sans délai : actif = un seul cuisinier ; fours, airfryer et feux = capacités de la
    cuisine (un four par défaut : une température à la fois ; feux sans limite si la cuisine est inconnue).
    """
    if not tasks:
        return
    pos = {t.idx: i for i, t in enumerate(tasks)}
    succ_fs: dict[int, list[int]] = {t.idx: [] for t in tasks}
    succ_ss: dict[int, list[int]] = {t.idx: [] for t in tasks}
    for t in tasks:
        for d in t.fs:
            succ_fs[d].append(t.idx)
        for d in t.ss:
            succ_ss[d].append(t.idx)
    tail: dict[int, int] = {}
    for t in reversed(tasks):  # dépendances toujours vers une étape antérieure de la même recette
        after_fs = max((tail[j] for j in succ_fs[t.idx]), default=0)
        after_ss = max((tail[j] for j in succ_ss[t.idx]), default=0)
        tail[t.idx] = max(t.duration + after_fs, after_ss)
    for t in tasks:
        t.start = None

    now, cook_free = 0, 0
    remaining = {t.idx for t in tasks}
    started: list[Task] = []
    while remaining:
        progressed = True
        while progressed:
            progressed = False
            ready = [
                tasks[pos[i]]
                for i in remaining
                if all(tasks[pos[d]].start is not None and tasks[pos[d]].end <= now for d in tasks[pos[i]].fs)
                and all(tasks[pos[d]].start is not None and tasks[pos[d]].start <= now for d in tasks[pos[i]].ss)
            ]
            ready.sort(key=lambda t: (-tail[t.idx], t.recipe_idx, t.order))
            for t in ready:
                if t.active and cook_free > now:
                    continue
                if _uses_capacity(t) and not _load_ok([o for o in started if o.end > now] + [t], kitchen):
                    continue
                t.start = now
                remaining.discard(t.idx)
                started.append(t)
                if t.active:
                    cook_free = now + t.duration
                progressed = True
                break
        if not remaining:
            break
        future = [o.end for o in started if o.end > now] + ([cook_free] if cook_free > now else [])
        if not future:  # garde-fou (dépendance impossible) : on libère la plus prioritaire
            t = min((tasks[pos[i]] for i in remaining), key=lambda t: (t.recipe_idx, t.order))
            t.fs, t.ss = [], []
            continue
        now = min(future)


def _overlaps(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def _capacity_ok(group: list[Task], others: list[Task], delta: int, kitchen: KitchenResources) -> bool:
    """Capacités (fours, airfryer, feux) respectées à chaque début d'étape pendant les étapes décalées."""
    spans = [(t.start + delta, t.end + delta, t) for t in group] + [(o.start, o.end, o) for o in others]
    for s, e, t in spans[: len(group)]:
        if not _uses_capacity(t):
            continue
        for p in {s} | {xs for xs, _, _ in spans if s < xs < e}:
            if not _load_ok([x for xs, xe, x in spans if xs <= p < xe and _uses_capacity(x)], kitchen):
                return False
    return True


def _feasible_shift(
    group: list[Task], others: list[Task], delta: int, kitchen: KitchenResources = DEFAULT_KITCHEN
) -> bool:
    for t in group:
        s, e = t.start + delta, t.end + delta
        for o in others:
            # Dépendance entre recettes (sous-recette → étape de la parente) : jamais après son successeur.
            if (t.idx in o.fs and e > o.start) or (t.idx in o.ss and s > o.start):
                return False
            if not _overlaps(s, e, o.start, o.end):
                continue
            if t.active and o.active:
                return False
    return _capacity_ok(group, others, delta, kitchen)


def _align_to_finish(tasks: list[Task], kitchen: KitchenResources = DEFAULT_KITCHEN) -> None:
    """Décale chaque recette au plus tard (sans conflit) : tout est prêt en même temps."""
    if not tasks:
        return
    makespan = max(t.end for t in tasks)
    recipes = sorted({t.recipe_idx for t in tasks}, key=lambda r: max(t.end for t in tasks if t.recipe_idx == r))
    by_idx = {t.idx: t for t in tasks}
    cross = any(by_idx.get(d) is not None and by_idx[d].recipe_idx != t.recipe_idx for t in tasks for d in t.fs)
    for _ in range(2 if cross else 1):  # 2e passe : une sous-recette suit sa parente décalée
        for r in recipes:
            group = [t for t in tasks if t.recipe_idx == r]
            others = [t for t in tasks if t.recipe_idx != r]
            for delta in range(makespan - max(t.end for t in group), 0, -1):
                if _feasible_shift(group, others, delta, kitchen):
                    for t in group:
                        t.start += delta
                    break
    # Préchauffage juste avant sa cuisson.
    for t in tasks:
        if t.kind != 'preheat':
            continue
        bakes = [o for o in tasks if t.idx in o.fs and o.bakes]
        if bakes:
            t.start = max(t.start, min(b.start for b in bakes) - t.duration)


def _makespan(tasks: list[Task]) -> int:
    return max((t.end for t in tasks), default=0)


# ---------------------------------------------------------------------------
# Textes
# ---------------------------------------------------------------------------
def format_minutes(minutes: int) -> str:
    minutes = int(round(minutes))
    if minutes < 60:
        return f'{minutes} min'
    h, m = divmod(minutes, 60)
    return f'{h} h {m:02d}' if m else f'{h} h'


def format_clock(t: time | datetime) -> str:
    return f'{t.hour} h {t.minute:02d}' if t.minute else f'{t.hour} h'


def join_fr(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return ''.join(items)
    return ', '.join(items[:-1]) + ' et ' + items[-1]


def _notes(tasks: list[Task], titles: dict[int, str]) -> None:
    for t in tasks:
        if not t.active:
            base = _PASSIVE_NOTE.get(t.kind, 'Temps passif')
            if t.kind in ('bake', 'preheat') and t.temperature:
                base += f' ({t.temperature} °C)'
            if t.alternative == 'airfryer':
                base = base.replace('Au four', 'À l’airfryer').replace('Préchauffe le four', 'Préchauffe l’airfryer')
            t.note = f'{base} · {t.note}' if t.note else base
            continue
        others = [
            o
            for o in tasks
            if o.recipe_idx != t.recipe_idx and not o.active and o.kind != 'preheat' and o.start <= t.start < o.end
        ]
        if others:
            o = max(others, key=lambda o: o.duration)
            while_note = f'Pendant que {titles[o.recipe_idx]} {_WHILE_VERB.get(o.kind, "cuit")}'
            t.note = f'{while_note} · {t.note}' if t.note else while_note


def _oven_conflicts(tasks: list[Task], titles: dict[int, str]) -> list[dict]:
    conflicts, seen = [], set()
    oven = sorted((t for t in tasks if t.uses_oven and t.temperature), key=lambda t: t.start)
    for i, a in enumerate(oven):
        for b in oven[i + 1 :]:
            if a.recipe_idx == b.recipe_idx or _oven_compatible(a.temperature, b.temperature):
                continue
            key = tuple(sorted((a.recipe_idx, b.recipe_idx)))
            if key in seen:
                continue
            seen.add(key)
            message = (
                f'Un seul four : {titles[a.recipe_idx]} à {a.temperature} °C puis {titles[b.recipe_idx]} '
                f'à {b.temperature} °C, pas en même temps.'
            )
            conflicts.append(
                {
                    'type': 'oven_temperature',
                    'recipe_ids': [a.recipe_id, b.recipe_id],
                    'step_ids': [a.step_id, b.step_id],
                    'temperatures': [a.temperature, b.temperature],
                    'message': message,
                }
            )
            extra = f'Four libéré par {titles[a.recipe_idx]} : règle-le à {b.temperature} °C'
            b.note = f'{b.note} · {extra}' if b.note else extra
    return conflicts


# ---------------------------------------------------------------------------
# Prépa commune (batch cooking)
# ---------------------------------------------------------------------------
_PREP_VERBS = (
    (re.compile(r'\bemin(?:c|ce|cer|cez|ces|cee|cees)\b'), 'Émince'),
    (re.compile(r'\bcisel(?:er|ez|e|es|ee|ees)\b'), 'Cisèle'),
    (re.compile(r'\bhach(?:er|ez|e|es|ee|ees)\b'), 'Hache'),
    (re.compile(r'\brap(?:er|ez|e|es|ee|ees)\b'), 'Râpe'),
    (re.compile(r'\b(?:eplucher|epluchez|epluche|epluches|epluchee|epluchees)\b'), 'Épluche'),
    (re.compile(r'\bconcass(?:er|ez|e|es|ee|ees)\b'), 'Concasse'),
    (re.compile(r'\b(?:detaill|taill)(?:er|ez|e|es|ee|ees)\b'), 'Taille'),
    (re.compile(r'\b(?:de)?coup(?:er|ez|e|es|ee|ees)\b'), 'Coupe'),
    (re.compile(r'\bpel(?:er|ez|e|es|ee|ees)\b'), 'Pèle'),
    (re.compile(r'\bpress(?:er|ez)\b'), 'Presse'),
)
_UNIT_LABELS = {
    'g': 'g',
    'kg': 'kg',
    'ml': 'ml',
    'l': 'l',
    'tsp': 'c. à café',
    'tbsp': 'c. à soupe',
    'cup': 'tasse(s)',
    'pinch': 'pincée(s)',
}
_PIECE_UNITS = {'piece', 'clove', ''}
_PREPOSITIONS = {'de', 'du', 'des', 'd', 'a', 'au', 'aux', 'en', 'la', 'le'}


def _format_qty(value: float) -> str:
    rounded = round(value, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f'{rounded}'.replace('.', ',')


def _pluralize(name: str) -> str:
    words, out, stop = name.split(' '), [], False
    for w in words:
        if fold(w) in _PREPOSITIONS or "'" in w or '’' in w:
            stop = True
        if not stop and len(w) >= 3 and w.isalpha() and not w.endswith(('s', 'x', 'z')):
            w = w + ('x' if w.endswith(('eau', 'au')) else 's')
        out.append(w)
    return ' '.join(out)


def _quantity_text(name: str, totals: dict[str, float]) -> str:
    name = name[:1].lower() + name[1:]
    parts = []
    for unit, qty in totals.items():
        if unit in _PIECE_UNITS:
            if unit == 'clove':
                parts.append(f'{_format_qty(qty)} gousse{"s" if qty > 1 else ""} d’{name}')
            else:
                parts.append(f'{_format_qty(qty)} {_pluralize(name) if qty > 1 else name}' if qty else name)
        else:
            de = 'd’' if fold(name)[:1] in 'aeiouyh' else 'de '
            parts.append(f'{_format_qty(qty)} {_UNIT_LABELS.get(unit, unit)} {de}{name}')
    return ' + '.join(parts)


def _mentions(step: StepInput, ingredient: IngredientInput) -> bool:
    if ingredient.ingredient_id in step.ingredient_ids:
        return True
    name = fold(ingredient.name)
    if len(name) < 3:
        return False
    text = fold(f'{step.title} {step.instruction}')
    singular = name[:-1] if name.endswith('s') else name
    return bool(re.search(rf'\b{re.escape(singular)}s?\b', text))


def _prep_verb(recipe: RecipeInput, ingredient: IngredientInput) -> str | None:
    for step in sorted(recipe.steps, key=lambda s: s.order):
        if not _mentions(step, ingredient):
            continue
        text = fold(f'{step.title} {step.instruction}')
        for pattern, verb in _PREP_VERBS:
            if pattern.search(text):
                return verb
    return None


def shared_prep(recipes: list[RecipeInput]) -> list[dict]:
    """Ingrédients (hors basiques) taillés dans ≥ 2 recettes : une seule préparation, quantités additionnées."""
    entries: dict[int, dict] = {}
    for recipe in recipes:
        ratio = (recipe.servings / recipe.base_servings) if recipe.base_servings else 1
        for ing in recipe.ingredients:
            if ing.is_staple:
                continue
            entry = entries.setdefault(
                ing.ingredient_id,
                {'ingredient': ing, 'recipes': [], 'totals': {}, 'verb': None},
            )
            if recipe.recipe_id in [r.recipe_id for r in entry['recipes']]:
                continue
            entry['recipes'].append(recipe)
            unit = ing.unit or ''
            entry['totals'][unit] = entry['totals'].get(unit, 0) + float(ing.quantity or 0) * ratio
            entry['verb'] = entry['verb'] or _prep_verb(recipe, ing)
    out = []
    for entry in entries.values():
        if len(entry['recipes']) < 2 or not entry['verb']:
            continue
        ing = entry['ingredient']
        titles = [r.title for r in entry['recipes']]
        what = _quantity_text(ing.name, entry['totals'])
        out.append(
            {
                'ingredient_id': ing.ingredient_id,
                'name': ing.name,
                'verb': entry['verb'],
                'label': f'{entry["verb"]} {what} pour {join_fr(titles)}',
                'totals': [{'quantity': round(q, 2), 'unit': u} for u, q in entry['totals'].items()],
                'recipe_ids': [r.recipe_id for r in entry['recipes']],
            }
        )
    out.sort(key=lambda e: (-len(e['recipe_ids']), fold(e['name'])))
    return out[:SHARED_PREP_MAX]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
def _task_payload(
    t: Task, titles: dict[int, str], origin: datetime | None, waits_for: dict[int, list[int]] | None = None
) -> dict:
    payload = {
        'recipe_id': t.recipe_id,
        'recipe_title': titles[t.recipe_idx],
        'step_id': t.step_id,
        'step_order': t.order,
        'label': t.label,
        'instruction': t.instruction,
        'start_offset_min': t.start,
        'duration_min': t.duration,
        'end_offset_min': t.end,
        'active': t.active,
        'kind': t.kind,
        'resource': t.resource,
        'alternative_resource': t.alternative,
        'temperature_c': t.temperature,
        'estimated': t.estimated,
        'note': t.note,
        'day': t.day,
        'waits_for_recipe_ids': (waits_for or {}).get(t.idx, []),
    }
    if origin is not None and t.day == DAY_SAME:
        payload['start_at'] = (origin + timedelta(minutes=t.start)).isoformat()
    return payload


def _summary(
    same_day: list[Task],
    day_before: list[Task],
    titles: dict[int, str],
    conflicts: list[dict],
    prep: list[dict],
) -> list[str]:
    lines: list[str] = []
    if day_before:
        names = list(dict.fromkeys(titles[t.recipe_idx] for t in day_before))
        long_step = max(day_before, key=lambda t: t.duration)
        what = {'marinate': 'mets à mariner', 'rise': 'lance la pâte', 'chill': 'mets au frais'}.get(
            long_step.kind, 'prépare'
        )
        lines.append(f'La veille : {what} {join_fr(names)}.')
    if prep:
        lines.append(f'Prépa commune d’abord : {prep[0]["label"]}.')
    recipe_count = len({t.recipe_idx for t in same_day})
    if same_day and recipe_count >= 2:
        first = min(same_day, key=lambda t: (t.start, t.recipe_idx, t.order))
        passives = [t for t in same_day if t.recipe_idx == first.recipe_idx and not t.active and t.kind != 'preheat']
        if passives:
            longest = max(passives, key=lambda t: t.duration)
            why = _WHY_FIRST.get(longest.kind, 'il prend le plus de temps ({d})').format(
                d=format_minutes(longest.duration)
            )
            lines.append(f'Lance d’abord {titles[first.recipe_idx]} : {why}.')
            others = [
                t
                for t in same_day
                if t.active and t.recipe_idx != first.recipe_idx and longest.start <= t.start < longest.end
            ]
            if others:
                o = min(others, key=lambda t: t.start)
                verb = _WHILE_VERB.get(longest.kind, 'cuit')
                lines.append(f'Pendant qu’il {verb}, attaque {titles[o.recipe_idx]}.')
        else:
            lines.append(f'Commence par {titles[first.recipe_idx]}.')
    lines.extend(c['message'] for c in conflicts)
    return lines


def _apply_kitchen(
    tasks: list[Task], titles: dict[int, str], recipes: list[RecipeInput], kitchen: KitchenResources
) -> list[dict]:
    """Four absent → airfryer (une fournée à la fois) ou avertissement ; plaques absentes → avertissement."""
    warnings: list[dict] = []

    def recipe_ids(idxs) -> list[int]:
        return [recipes[i].recipe_id for i in idxs]

    if not kitchen.has_oven:
        oven_recipes = sorted({t.recipe_idx for t in tasks if t.resource == 'oven'})
        if oven_recipes:
            names = join_fr([titles[i] for i in oven_recipes])
            if kitchen.airfryer:
                for t in tasks:
                    if t.resource == 'oven':
                        t.alternative = 'airfryer'
                verb = 'passent' if len(oven_recipes) > 1 else 'passe'
                warnings.append(
                    {
                        'type': 'oven_to_airfryer',
                        'recipe_ids': recipe_ids(oven_recipes),
                        'message': f'Pas de four : {names} {verb} à l’airfryer, une fournée à la fois.',
                    }
                )
            else:
                warnings.append(
                    {
                        'type': 'missing_oven',
                        'recipe_ids': recipe_ids(oven_recipes),
                        'message': f'Il te manque un four pour {names}.',
                    }
                )
    if not kitchen.has_hob:
        hob_recipes = sorted({t.recipe_idx for t in tasks if t.resource == 'hob'})
        if hob_recipes:
            warnings.append(
                {
                    'type': 'missing_hob',
                    'recipe_ids': recipe_ids(hob_recipes),
                    'message': f'Il te manque des plaques pour {join_fr([titles[i] for i in hob_recipes])}.',
                }
            )
    return warnings


def build_plan(
    recipes: list[RecipeInput],
    *,
    start_at: datetime | None = None,
    ready_at: datetime | None = None,
    with_shared_prep: bool = True,
    kitchen: KitchenResources | None = None,
) -> dict:
    """
    Plan déterministe (pur, sans base) ; `start_at` ou `ready_at` (aware) fixent l'horaire. `kitchen` : ressources
    réelles (feux, fours, airfryer) ; None = cuisine inconnue (un four, feux sans limite).
    """
    kitchen = kitchen or DEFAULT_KITCHEN
    titles = {i: r.title for i, r in enumerate(recipes)}
    all_tasks: list[Task] = []
    per_recipe: list[list[Task]] = []
    for i, recipe in enumerate(recipes):
        tasks = _recipe_tasks(recipe, i)
        per_recipe.append(tasks)
        all_tasks.extend(tasks)
    for n, t in enumerate(all_tasks):
        t.idx = n
    _link(all_tasks)
    equipment_warnings = _apply_kitchen(all_tasks, titles, recipes, kitchen)

    same_day = [t for t in all_tasks if t.day == DAY_SAME]
    day_before = [t for t in all_tasks if t.day == DAY_BEFORE]
    for group in (same_day, day_before):
        ids = {t.idx for t in group}
        for t in group:  # une étape de la veille est faite avant le jour J : dépendance satisfaite
            t.fs = [d for d in t.fs if d in ids]
            t.ss = [d for d in t.ss if d in ids]

    # Durée « en enchaînant les recettes » : chaque recette seule, l'une après l'autre.
    alone_durations = []
    for tasks in per_recipe:
        alone = [t for t in tasks if t.day == DAY_SAME]
        _schedule(alone, kitchen)
        alone_durations.append(_makespan(alone))
    sequential = sum(alone_durations)

    waits = _link_sub_recipes(all_tasks, recipes)
    waits_for: dict[int, list[int]] = {}
    for sub_idx, using in waits.items():
        waits_for.setdefault(using.idx, []).append(recipes[sub_idx].recipe_id)

    _schedule(day_before, kitchen)
    _schedule(same_day, kitchen)
    _align_to_finish(same_day, kitchen)
    total = _makespan(same_day)
    conflicts = _oven_conflicts(same_day, titles) if kitchen.ovens <= 1 else []
    _notes(same_day, titles)
    _notes(day_before, titles)

    if ready_at is not None:
        start_at = ready_at - timedelta(minutes=total)
    elif start_at is not None:
        ready_at = start_at + timedelta(minutes=total)

    order_key = lambda t: (t.start, t.recipe_idx, t.order)  # noqa: E731
    same_day.sort(key=order_key)
    day_before.sort(key=order_key)
    prep = shared_prep(recipes) if with_shared_prep and len(recipes) >= 2 else []
    summary = _summary(same_day, day_before, titles, conflicts, prep)
    summary.extend(w['message'] for w in equipment_warnings)
    for sub_idx, using in waits.items():
        parent_title = titles[recipes[sub_idx].parent_idx]
        summary.append(f'{titles[sub_idx]} maison : à terminer avant « {using.label} » ({parent_title}).')

    if start_at is not None and ready_at is not None:
        headline = f'Commence à {format_clock(start_at)} pour manger à {format_clock(ready_at)}'
    else:
        headline = f'Tout est prêt en {format_minutes(total)}'
    saved = len(recipes) >= 2 and sequential > total
    if start_at is not None and saved:
        timing = f'{headline} ({format_minutes(total)} en tout, au lieu de {format_minutes(sequential)} en enchaînant les recettes).'
    elif start_at is not None:
        timing = f'{headline} ({format_minutes(total)} en tout).'
    elif saved:
        timing = f'{headline} (au lieu de {format_minutes(sequential)} en enchaînant les recettes).'
    else:
        timing = f'{headline}.'
    summary.append(timing)

    return {
        'recipes': [
            {
                'id': r.recipe_id,
                'title': r.title,
                'servings': r.servings,
                'image_url': r.image_url,
                'duration_min': alone_durations[i],
                'role': 'sub_recipe' if r.parent_idx is not None else 'main',
                'parent_recipe_id': recipes[r.parent_idx].recipe_id if r.parent_idx is not None else None,
                'line_id': r.used_line_id,
            }
            for i, r in enumerate(recipes)
        ],
        'steps': [_task_payload(t, titles, start_at, waits_for) for t in same_day],
        'day_before': [_task_payload(t, titles, None, waits_for) for t in day_before],
        'shared_prep': prep,
        'conflicts': conflicts,
        'kitchen': kitchen.payload(),
        'equipment_warnings': equipment_warnings,
        'total_duration_min': total,
        'sequential_duration_min': sequential,
        'saved_min': max(0, sequential - total),
        'active_duration_min': sum(t.duration for t in same_day if t.active),
        'start_at': start_at.isoformat() if start_at else None,
        'ready_at': ready_at.isoformat() if ready_at else None,
        'start_label': format_clock(start_at) if start_at else None,
        'ready_label': format_clock(ready_at) if ready_at else None,
        'headline': headline,
        'summary': summary,
    }
