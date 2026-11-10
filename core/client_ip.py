"""
IP du client et accès serveur du site aux aperçus publics.

`get_client_ip` : derrière `TRUSTED_PROXY_COUNT` proxys de confiance (Caddy en prod : 1), chaque proxy
ajoute l'adresse de son pair à droite de `X-Forwarded-For` ; l'IP du client est donc la N-ième entrée
en partant de la **droite**. Les entrées plus à gauche viennent du client et sont falsifiables : elles ne
sont jamais lues. `TRUSTED_PROXY_COUNT=0` (Django exposé directement) → `REMOTE_ADDR` seulement.

`public_rate_limited` : le site (`tchikook-landing`, fonctions Vercel) rend les pages `/i/<code>`, `/r/<id>`,
`/p/<id>`, `/u/<username>` côté serveur ; vues de l'API, toutes ses requêtes viennent de quelques IP Vercel.
Avec la clé partagée `X-Tchikook-Preview-Key` (= `PUBLIC_PREVIEW_SERVER_KEY`, vide = désactivé), la limite
porte sur le visiteur relayé (`X-Tchikook-Visitor-IP`, compteur séparé) sous un plafond global de la clé
(`PUBLIC_PREVIEW_SERVER_LIMIT_PER_HOUR`) ; sans clé valide, limite par IP habituelle.
"""

from __future__ import annotations

import hmac
import ipaddress

from django.conf import settings

PREVIEW_KEY_HEADER = 'HTTP_X_TCHIKOOK_PREVIEW_KEY'
VISITOR_IP_HEADER = 'HTTP_X_TCHIKOOK_VISITOR_IP'
HOUR = 3600


def _valid_ip(value) -> str | None:
    value = (value or '').strip()
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def get_client_ip(request) -> str:
    remote = _valid_ip(request.META.get('REMOTE_ADDR')) or 'unknown'
    try:
        count = int(getattr(settings, 'TRUSTED_PROXY_COUNT', 1) or 0)
    except (TypeError, ValueError):
        count = 1
    if count <= 0:
        return remote
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR') or ''
    entries = [part.strip() for part in forwarded.split(',') if part.strip()]
    if len(entries) < count:
        # Moins d'entrées que de proxys : la requête n'est pas passée par toute la chaîne.
        return remote
    return _valid_ip(entries[-count]) or remote


def has_preview_server_key(request) -> bool:
    expected = (getattr(settings, 'PUBLIC_PREVIEW_SERVER_KEY', '') or '').strip()
    if not expected:
        return False
    provided = (request.META.get(PREVIEW_KEY_HEADER) or '').strip()
    if not provided:
        return False
    return hmac.compare_digest(provided.encode(), expected.encode())


def public_rate_limited(request, bucket: str, *, limit_per_ip: int, window_seconds: int = HOUR) -> bool:
    """True si la requête dépasse sa limite (`bucket` : `public`, `lookup` → compteurs `<bucket>:ip` / `:visitor`)."""
    from accounts.services.invite_service import rate_limited

    if not has_preview_server_key(request):
        return rate_limited(f'{bucket}:ip', get_client_ip(request), limit=limit_per_ip, window_seconds=window_seconds)
    cap = int(getattr(settings, 'PUBLIC_PREVIEW_SERVER_LIMIT_PER_HOUR', 5000) or 0)
    if cap and rate_limited('preview-key:global', 'site', limit=cap, window_seconds=HOUR):
        return True
    visitor = _valid_ip(request.META.get(VISITOR_IP_HEADER))
    if visitor is None:
        return False
    return rate_limited(f'{bucket}:visitor', visitor, limit=limit_per_ip, window_seconds=window_seconds)
