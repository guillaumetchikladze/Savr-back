from django.db.models import Q
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode


def get_accessible_meal_plan_filter(user):
    """
    Retourne un Q object pour filtrer les MealPlan auxquels un utilisateur a accès :
    - Les MealPlan dont il est le propriétaire
    - Les MealPlan auxquels il est invité avec une invitation acceptée
    """
    return Q(
        Q(user=user) |  # Propriétaire
        Q(invitations__invitee=user, invitations__status='accepted')  # Invité accepté
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


