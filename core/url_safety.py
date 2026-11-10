"""
Requêtes sortantes vers une URL fournie par un utilisateur (import de recette, image d'une page importée).

Garde-fou SSRF : on ne suit que du http(s) vers des hôtes dont **toutes** les adresses résolues sont
publiques (jamais localhost, réseau privé / Docker, lien local 169.254.x — métadonnées cloud —, multicast,
réservé). Les redirections sont suivies à la main (≤ `MAX_REDIRECTS`) et chaque saut est revérifié.

Limite connue : la résolution DNS est faite avant `requests` (une réponse DNS qui change entre les deux —
« DNS rebinding » — n'est pas couverte) ; le pare-feu réseau du serveur reste la défense de fond.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests

MAX_REDIRECTS = 5
ALLOWED_SCHEMES = ('http', 'https')


class UnsafeURLError(requests.RequestException):
    """URL refusée (schéma, hôte interne, trop de redirections) : attrapée comme une erreur réseau."""


def _resolved_ips(host: str, port: int) -> list:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return [ipaddress.ip_address(info[4][0].split('%', 1)[0]) for info in infos]


def is_public_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or '').strip())
        if parsed.scheme.lower() not in ALLOWED_SCHEMES or not parsed.hostname:
            return False
        port = parsed.port or (443 if parsed.scheme.lower() == 'https' else 80)
        ips = _resolved_ips(parsed.hostname, port)
    except (OSError, ValueError, UnicodeError):
        return False
    return bool(ips) and all(ip.is_global and not ip.is_multicast for ip in ips)


def safe_get(url: str, **kwargs) -> requests.Response:
    """`requests.get` limité aux URLs publiques, redirections revérifiées. Lève `UnsafeURLError` sinon."""
    kwargs.pop('allow_redirects', None)
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not is_public_url(current):
            raise UnsafeURLError(f'URL refusée : {current!r}')
        response = requests.get(current, allow_redirects=False, **kwargs)
        location = response.headers.get('Location') if response.is_redirect else None
        if not location:
            return response
        response.close()
        current = urljoin(current, location)
    raise UnsafeURLError('Trop de redirections')
