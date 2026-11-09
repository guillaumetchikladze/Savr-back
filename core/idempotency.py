"""
Idempotence des requêtes mutantes (file hors-ligne du front : liste de courses, puis d'autres endpoints).

Usage, sur une méthode de vue DRF (après `@action` le cas échéant) :

    @action(detail=True, methods=['post'])
    @idempotent
    def check(self, request, pk=None): ...

Si la requête porte `X-Idempotency-Key` (ou `Idempotency-Key`) et que l'utilisateur est authentifié :
- 1re requête : exécutée normalement ; une réponse 2xx est mémorisée (méthode, chemin, empreinte du
  corps, statut, corps JSON) 48 h ; une erreur (exception, 4xx/5xx) n'est pas mémorisée → rejouable ;
- même clé, même méthode / chemin / corps : la réponse mémorisée est renvoyée telle quelle
  (en-tête `Idempotent-Replayed: true`), sans ré-appliquer l'opération ;
- même clé, autre méthode / chemin / corps : 409 `idempotency_key_reused`.
Doublons concurrents : la contrainte unique (user, key) sérialise les deux requêtes (insertion d'abord,
la 2e attend la fin de la 1re puis rejoue sa réponse). Sans clé : comportement inchangé.
Nettoyage : `core.tasks.purge_expired_idempotency_records` (Celery beat) + une clé expirée est réutilisable.
"""

from __future__ import annotations

import functools
import hashlib
import json
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

IDEMPOTENCY_TTL = timedelta(hours=48)
HEADER_NAMES = ('X-Idempotency-Key', 'Idempotency-Key')
MAX_KEY_LENGTH = 255
REPLAY_HEADER = 'Idempotent-Replayed'


def get_idempotency_key(request) -> str:
    for name in HEADER_NAMES:
        value = (request.headers.get(name) or '').strip()
        if value:
            return value
    return ''


def request_fingerprint(request) -> str:
    """Empreinte stable du corps + paramètres de requête (l'ordre des clés n'importe pas)."""
    data = request.data
    if hasattr(data, 'lists'):  # QueryDict (form / multipart / corps vide)
        data = {k: v for k, v in data.lists()}
    query = {k: v for k, v in request.query_params.lists()}
    payload = json.dumps({'body': data, 'query': query}, sort_keys=True, default=str, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def _claim(user, key: str, method: str, path: str, fingerprint: str):
    """Réserve la clé (insertion d'abord). Retourne (record, True) si cette requête doit s'exécuter."""
    from .models import IdempotencyRecord

    fields = {'method': method, 'path': path, 'request_hash': fingerprint}
    try:
        with transaction.atomic():
            return IdempotencyRecord.objects.create(user=user, key=key, **fields), True
    except IntegrityError:
        pass
    # La clé existe (éventuellement après attente de la transaction concurrente) : on la verrouille.
    record = IdempotencyRecord.objects.select_for_update().filter(user=user, key=key).first()
    if record is None:  # purgée entre-temps
        return IdempotencyRecord.objects.create(user=user, key=key, **fields), True
    if record.status_code is None or record.created_at < timezone.now() - IDEMPOTENCY_TTL:
        # Expirée (pas encore purgée) : réutilisable comme une clé neuve.
        for name, value in fields.items():
            setattr(record, name, value)
        record.status_code = None
        record.response_body = None
        record.created_at = timezone.now()
        record.save()
        return record, True
    return record, False


def _json_body(response):
    data = getattr(response, 'data', None)
    if data is None:
        return None
    return json.loads(JSONRenderer().render(data) or b'null')


def idempotent(view_method):
    """Décorateur de méthode de vue DRF (voir docstring du module)."""

    @functools.wraps(view_method)
    def wrapper(self, request, *args, **kwargs):
        key = get_idempotency_key(request)
        user = getattr(request, 'user', None)
        if not key or user is None or not user.is_authenticated:
            return view_method(self, request, *args, **kwargs)
        if len(key) > MAX_KEY_LENGTH:
            return Response(
                {'error': 'invalid_idempotency_key', 'detail': f'Clé trop longue (max {MAX_KEY_LENGTH}).'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        method, path = request.method.upper(), request.path[:512]
        fingerprint = request_fingerprint(request)
        with transaction.atomic():
            record, fresh = _claim(user, key, method, path, fingerprint)
            if not fresh:
                if (record.method, record.path, record.request_hash) != (method, path, fingerprint):
                    return Response(
                        {
                            'error': 'idempotency_key_reused',
                            'detail': 'Cette clé a déjà servi pour une autre requête.',
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                return Response(record.response_body, status=record.status_code, headers={REPLAY_HEADER: 'true'})

            # Une exception ici annule aussi la réservation (rien n'est mémorisé).
            response = view_method(self, request, *args, **kwargs)
            if 200 <= response.status_code < 300:
                record.status_code = response.status_code
                record.response_body = _json_body(response)
                record.save(update_fields=['status_code', 'response_body'])
            else:
                record.delete()
            return response

    return wrapper


def purge_expired(now=None) -> int:
    from .models import IdempotencyRecord

    cutoff = (now or timezone.now()) - IDEMPOTENCY_TTL
    deleted, _ = IdempotencyRecord.objects.filter(created_at__lt=cutoff).delete()
    return deleted
