from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


def get_meal_plan_people_count(meal_plan, group_meal_plans=None):
    """
    Retourne le nombre de personnes pour un meal plan (1 + participants acceptés/pending + guest_count).
    Utilisable depuis serializers sans importer views.
    """
    if group_meal_plans and len(group_meal_plans) > 1:
        from .models import MealPlan
        total_guest_count = sum(mp.guest_count or 0 for mp in group_meal_plans)
        all_participants = []
        for mp in group_meal_plans:
            invitations = mp.invitations.all() if hasattr(mp, 'invitations') else []
            for inv in invitations:
                all_participants.append({'user': inv.invitee, 'status': inv.status})
        active_participants_by_user = {}
        for p in all_participants:
            if p.get('status') in ['accepted', 'pending']:
                user_id = p['user'].id if hasattr(p['user'], 'id') else p['user']['id'] if isinstance(p['user'], dict) else None
                if user_id:
                    existing = active_participants_by_user.get(user_id)
                    if not existing or (p.get('status') == 'accepted' and existing != 'accepted'):
                        active_participants_by_user[user_id] = p.get('status')
        return len(group_meal_plans) + len(active_participants_by_user) + total_guest_count
    participants_count = meal_plan.invitations.filter(
        status__in=['accepted', 'pending']
    ).count() if hasattr(meal_plan, 'invitations') else 0
    guest_count = meal_plan.guest_count or 0
    return 1 + participants_count + guest_count


def get_batch_portions(meal_plan, mprb, people_count=None):
    """
    Retourne le nombre effectif de portions pour ce batch dans ce repas.
    Si mprb.portions est renseigné, on le retourne ; sinon people_count (nombre de personnes).
    """
    if mprb.portions is not None:
        return mprb.portions
    if people_count is not None:
        return people_count
    return get_meal_plan_people_count(meal_plan)


def get_accessible_meal_plan_filter(user):
    """
    Retourne un Q object pour filtrer les MealPlan auxquels un utilisateur a accès :
    - Les MealPlan dont il est le propriétaire
    - Les MealPlan auxquels il est invité avec une invitation acceptée ou en attente (lecture autorisée)
    """
    return Q(
        Q(user=user) |  # Propriétaire
        Q(invitations__invitee=user, invitations__status__in=['accepted', 'pending'])
    )


def get_invited_recipe_filter(user):
    """
    Recettes privées accessibles via un meal plan auquel l'utilisateur est invité.
    N'inclut pas les recettes publiques (gérées ailleurs) ni celles dont il est auteur.
    """
    return Q(
        batches__meal_plan_recipe_batches__meal_plan__invitations__invitee=user,
        batches__meal_plan_recipe_batches__meal_plan__invitations__status__in=['accepted', 'pending'],
    )


def canonicalize_import_url(url: str) -> str:
    """
    Normalise une URL d'import pour faciliter la déduplication.

    - Met le schéma et le host en minuscules
    - Supprime les ports par défaut (80/443)
    - Normalise le path (supprime le slash final sauf pour '/')
    - Supprime le fragment (#...)
    - Supprime uniquement les paramètres de tracking (utm_*, gclid, fbclid, mc_cid, mc_eid)
      et conserve tous les autres paramètres (id, recipeId, etc.)
    - Trie les paramètres de query par (clé, valeur) pour rendre l'ordre indifférent
    """
    if not url:
        return url

    url = url.strip()
    try:
        parsed = urlparse(url)
    except Exception:
        # Si l'URL est invalide, on retourne la version trim pour ne pas masquer l'erreur en amont
        return url

    scheme = (parsed.scheme or '').lower()
    netloc = (parsed.netloc or '').lower()

    # Supprimer les ports par défaut
    if netloc.endswith(':80') and scheme == 'http':
        netloc = netloc[:-3]
    elif netloc.endswith(':443') and scheme == 'https':
        netloc = netloc[:-4]

    # Normaliser le path
    path = parsed.path or ''

    # Cas particulier Instagram : /reel/{id} et /p/{id} doivent être considérés comme équivalents
    if 'instagram.com' in netloc or 'instagr.am' in netloc:
        segments = [s for s in path.split('/') if s]
        if len(segments) >= 2:
            # Unifier toutes les variantes (/reel/, /p/, /tv/, ...) vers /p/{id}
            slug = segments[1]
            path = f"/p/{slug}"

    # Supprimer le slash final sauf pour '/'
    if path != '/' and path.endswith('/'):
        path = path[:-1]

    # Filtrer les paramètres de query
    tracking_keys = {'gclid', 'fbclid', 'mc_cid', 'mc_eid'}
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    filtered_pairs = []

    if 'instagram.com' in netloc or 'instagr.am' in netloc:
        # Pour Instagram, aucun paramètre de query n'est utile pour identifier la recette.
        # On les supprime donc tous (utm_*, igsh, etc.).
        filtered_pairs = []
    else:
        # Pour les autres sites, ne supprimer que les paramètres de tracking connus.
        for key, value in query_pairs:
            key_lower = key.lower()
            if key_lower.startswith('utm_') or key_lower in tracking_keys:
                continue
            filtered_pairs.append((key, value))

    # Trier les paramètres de query pour rendre l'ordre indifférent
    if filtered_pairs:
        filtered_pairs.sort(key=lambda kv: (kv[0], kv[1]))
        query = urlencode(filtered_pairs, doseq=True)
    else:
        query = ''

    # Supprimer le fragment
    fragment = ''

    normalized = urlunparse((scheme, netloc, path, parsed.params, query, fragment))
    return normalized


def shopping_list_item_quantity_is_stale(quantity, now, hide_after: timedelta) -> bool:
    """
    True si cette sous-ligne (ShoppingListItemQuantity) est entièrement cochée
    et cochée depuis plus de hide_after — même règle que le masquage 24h des lignes
    dans with_quantities (évite d'exposer d'anciens batchs une fois le besoin réapparu).
    """
    qty = Decimal(str(quantity.quantity or 0))
    checked = Decimal(str(quantity.checked_quantity or 0))
    if qty - checked > Decimal('0'):
        return False
    if not quantity.checked_at:
        return False
    try:
        return now - quantity.checked_at > hide_after
    except Exception:
        return False


def meal_plan_slot_api_fields(meal_plan):
    """
    Champs custom_label + meal_time_display alignés sur MealPlanSerializer.get_meal_time_display,
    pour les entrées `meals` des payloads recipe-batches (le client affiche le nom du créneau perso).
    """
    custom = (getattr(meal_plan, 'custom_label', None) or '').strip()
    if meal_plan.meal_time == 'other' and custom:
        display = custom
    else:
        display = meal_plan.get_meal_time_display()
    return {
        'custom_label': custom or None,
        'meal_time_display': display,
    }

