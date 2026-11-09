"""
Convives d'un repas ou d'une envie du panier (« Foyer + invités », .claude-work/specs/household.md).

    total = 1 (cuisinier) + foyer non exclu + invités sans compte (`guest_count`)
            + invitations actives (acceptées, et en attente comme avant : les portions ne baissent pas
              tant que l'invité n'a pas répondu), sans compter deux fois un membre du foyer invité.

`total` est **le** nombre de personnes : les portions qui « suivent » (`MealPlanRecipeBatch.portions=None`)
et les quantités de la liste de courses en dérivent (`recipes.utils.get_meal_plan_people_count`).
Le foyer ne compte jamais comme convive pour les stats (`stats_service.household_member_ids`).

`cache` : dict par requête HTTP (`serializer.context`) → 1 requête foyer par cuisinier, pas par repas.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from accounts.services.household_service import HouseholdEntry, entry_payload, household_for, user_payload

ACTIVE_INVITATION_STATUSES = ('accepted', 'pending')


@dataclass
class Composition:
    cook_id: int
    household: list[HouseholdEntry]
    excluded: list[HouseholdEntry]
    guests: int
    invited_accepted: list = field(default_factory=list)  # Users
    invited_pending: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return 1 + len(self.household) + self.guests + len(self.invited_accepted) + len(self.invited_pending)


def context_cache(context: dict | None) -> dict | None:
    """Mémo foyer partagé par tous les serializers d'une même réponse."""
    if context is None:
        return None
    return context.setdefault('household_cache', {})


def _split_household(cook_id: int, excluded_ids, cache: dict | None):
    household = household_for(cook_id, cache)
    excluded_set = set()
    for v in excluded_ids or ():
        try:
            excluded_set.add(int(v))
        except (TypeError, ValueError):
            continue
    included = [e for e in household if e.id not in excluded_set]
    excluded = [e for e in household if e.id in excluded_set]
    return included, excluded


def _active_invitations(meal_plan):
    """Invitations actives ; utilise le prefetch `invitations` s'il existe (sinon 1 requête)."""
    try:
        invitations = meal_plan.invitations.all()
    except Exception:
        return []
    return [inv for inv in invitations if inv.status in ACTIVE_INVITATION_STATUSES]


def meal_plan_composition(meal_plan, cache: dict | None = None) -> Composition:
    included, excluded = _split_household(meal_plan.user_id, getattr(meal_plan, 'excluded_household_ids', ()), cache)
    skip_ids = {meal_plan.user_id} | {e.user_id for e in included if e.user_id}
    comp = Composition(meal_plan.user_id, included, excluded, max(0, int(meal_plan.guest_count or 0)))
    seen: set[int] = set()
    for inv in sorted(_active_invitations(meal_plan), key=lambda i: (i.status != 'accepted', i.id)):
        if inv.invitee_id in skip_ids or inv.invitee_id in seen:
            continue
        seen.add(inv.invitee_id)
        (comp.invited_accepted if inv.status == 'accepted' else comp.invited_pending).append(inv.invitee)
    return comp


def basket_item_composition(item, cache: dict | None = None) -> Composition:
    """Envie non datée : même règle, sans invitations. Datée : convives du repas."""
    if item.meal_plan_id:
        return meal_plan_composition(item.meal_plan, cache)
    included, excluded = _split_household(item.user_id, item.excluded_household_ids, cache)
    return Composition(item.user_id, included, excluded, max(0, int(item.guest_count or 0)))


def meal_plan_people_count(meal_plan, cache: dict | None = None) -> int:
    return meal_plan_composition(meal_plan, cache).total


def basket_item_people_count(item, cache: dict | None = None) -> int:
    return basket_item_composition(item, cache).total


def diners_payload(comp: Composition, context: dict | None = None) -> dict:
    """
    `diners` exposé par l'API : de quoi écrire « Pour 4 : toi, Pauline + 2 invités » sans calcul côté app.
    """
    return {
        'total': comp.total,
        'cook': 1,
        'cook_user_id': comp.cook_id,
        'household': [entry_payload(e, context) for e in comp.household],
        'household_excluded': [entry_payload(e, context) for e in comp.excluded],
        'guests': comp.guests,
        'invited_accepted': [user_payload(u, context) for u in comp.invited_accepted],
        'invited_pending': [user_payload(u, context) for u in comp.invited_pending],
    }
