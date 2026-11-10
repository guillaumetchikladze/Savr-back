"""
Liens d'invitation personnels (« Tes amis avant ton premier plat », .claude-work/specs/density.md).

- Un code actif par utilisateur, créé à la demande, régénérable (`rotate_code`).
- Utiliser un code (`redeem`) : amitié mutuelle parrain ↔ invité (les deux suivis approuvés),
  sortie de la liste d'attente si `INVITE_BYPASSES_WAITLIST` (email ajouté à `AllowedEmail`),
  notification `invite_joined` au parrain, trace `InviteRedemption` (mesure « ≥ 3 amis à J7 »).
- Codes non devinables : 10 caractères dans un alphabet de 31 (≈ 50 bits) ; les vues limitent
  en plus le débit des recherches / utilisations.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.public_links import public_display_name
from notifications.services import notify

from ..blocks import are_blocked_either_way
from ..models import AllowedEmail, InviteCode, InviteRedemption
from .follow_service import make_mutual_friends

CODE_ALPHABET = 'abcdefghjkmnpqrstuvwxyz23456789'  # sans i, l, o, 0, 1
CODE_LENGTH = 10
CODE_MAX_LENGTH = 16
# Un invité « arrive grâce au lien » si c'est sa 1re invitation et que son compte a moins de N jours.
ACQUISITION_WINDOW_DAYS = 7


class InviteError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class RedeemResult:
    redemption: InviteRedemption
    created: bool


def normalize_code(raw) -> str:
    """Code saisi / extrait d'un lien → forme canonique (minuscules, sans espaces ni tirets)."""
    value = str(raw or '').strip().lower()
    if '/i/' in value:  # lien complet collé
        value = value.rsplit('/i/', 1)[1]
    value = value.split('?', 1)[0].strip('/ ')
    value = value.replace('-', '').replace(' ', '')
    return value[:CODE_MAX_LENGTH]


def _generate_code() -> str:
    return ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _create_code(user) -> InviteCode:
    for _ in range(5):
        try:
            with transaction.atomic():
                return InviteCode.objects.create(user=user, code=_generate_code())
        except IntegrityError:
            # Collision de code (improbable) ou code actif créé en parallèle.
            existing = InviteCode.objects.filter(user=user, is_active=True).first()
            if existing:
                return existing
    raise RuntimeError("Impossible de générer un code d'invitation unique")


def get_or_create_active_code(user) -> InviteCode:
    return InviteCode.objects.filter(user=user, is_active=True).first() or _create_code(user)


@transaction.atomic
def rotate_code(user) -> InviteCode:
    """Désactive le code actif (les liens déjà partagés ne marchent plus) et en crée un nouveau."""
    InviteCode.objects.filter(user=user, is_active=True).update(is_active=False, revoked_at=timezone.now())
    return _create_code(user)


def max_uses() -> int:
    return max(int(getattr(settings, 'INVITE_CODE_MAX_USES', 50) or 0), 0)


def invite_url(code: str) -> str:
    base = (getattr(settings, 'INVITE_BASE_URL', '') or 'https://tchikook.fr').rstrip('/')
    return f'{base}/i/{code}'


def app_invite_url(code: str) -> str:
    scheme = getattr(settings, 'NOTIFICATIONS_DEEP_LINK_SCHEME', 'tchikook')
    return f'{scheme}://invite/{code}'


def share_text(code: str) -> str:
    return f'Je note ce que je cuisine sur Tchikook. Rejoins-moi pour voir ce qu’on mange : {invite_url(code)}'


def invite_payload(user) -> dict:
    """`GET /api/invites/me/`."""
    invite = get_or_create_active_code(user)
    uses = invite.redemptions.count()
    limit = max_uses()
    return {
        'code': invite.code,
        'url': invite_url(invite.code),
        'app_url': app_invite_url(invite.code),
        'share_text': share_text(invite.code),
        'uses': uses,
        'max_uses': limit or None,
        'remaining_uses': max(limit - uses, 0) if limit else None,
        'joined_count': InviteRedemption.objects.filter(inviter=user, is_acquisition=True).count(),
        'created_at': invite.created_at,
    }


def resolve_code(raw) -> InviteCode | None:
    """Code actif d'un parrain actif et validé, sinon None (aucune distinction pour ne rien révéler)."""
    code = normalize_code(raw)
    if len(code) < 6:
        return None
    return (
        InviteCode.objects.select_related('user')
        .filter(code=code, is_active=True, user__is_active=True, user__validated_at__isnull=False)
        .first()
    )


def _is_new_member(invitee, now) -> bool:
    if InviteRedemption.objects.filter(invitee=invitee).exists():
        return False
    joined = getattr(invitee, 'created_at', None) or getattr(invitee, 'date_joined', None)
    return joined is None or joined >= now - timedelta(days=ACQUISITION_WINDOW_DAYS)


def redeem(invitee, raw_code, *, at_signup: bool = False) -> RedeemResult:
    """
    L'invité utilise le code d'un parrain. Idempotent par paire (2e appel : `created=False`).
    Lève `InviteError` (invalid_code 404, own_code 400, exhausted 410).
    """
    invite = resolve_code(raw_code)
    if invite is None or are_blocked_either_way(invite.user, invitee):
        raise InviteError('invalid_code', 'Ce lien d’invitation n’est plus valable.', 404)
    inviter = invite.user
    if inviter.id == invitee.id:
        raise InviteError('own_code', 'C’est ton propre lien d’invitation.', 400)

    existing = InviteRedemption.objects.filter(inviter=inviter, invitee=invitee).first()
    if existing:
        make_mutual_friends(inviter, invitee)  # rétablit l'amitié si elle a été coupée entre-temps
        return RedeemResult(existing, False)

    limit = max_uses()
    now = timezone.now()
    with transaction.atomic():
        # Verrou sur la ligne du code jusqu'à la fin de la transaction : deux utilisations simultanées ne
        # peuvent pas lire le même compteur et dépasser `max_uses` (la 2e attend la 1re puis recompte).
        locked = InviteCode.objects.select_for_update().filter(pk=invite.pk, is_active=True).first()
        if locked is None:  # régénéré entre-temps
            raise InviteError('invalid_code', 'Ce lien d’invitation n’est plus valable.', 404)
        if limit and locked.redemptions.count() >= limit:
            raise InviteError('exhausted', 'Ce lien a déjà beaucoup servi. Demande un nouveau lien.', 410)
        new_member = _is_new_member(invitee, now)
        bypassed = False
        if invitee.validated_at is None and getattr(settings, 'INVITE_BYPASSES_WAITLIST', True):
            email = (invitee.email or '').strip().lower()
            if email:
                AllowedEmail.objects.get_or_create(email=email, defaults={'note': f'invitation de {inviter.username}'})
            invitee.validated_at = now
            invitee.save(update_fields=['validated_at'])
            bypassed = True
        try:
            with transaction.atomic():
                redemption = InviteRedemption.objects.create(
                    code=locked,
                    inviter=inviter,
                    invitee=invitee,
                    at_signup=at_signup,
                    is_acquisition=new_member,
                    bypassed_waitlist=bypassed,
                )
        except IntegrityError:  # double tap concurrent
            return RedeemResult(InviteRedemption.objects.get(inviter=inviter, invitee=invitee), False)
        make_mutual_friends(inviter, invitee)

    notify(
        'invite_joined',
        inviter,
        actor=invitee,
        context={'new_member': new_member},
        dedupe_key=f'invite_joined:{invitee.id}',
        defer_push=True,  # appel Expo hors requête (inscription / redeem)
    )
    return RedeemResult(redemption, True)


def redemption_payload(result: RedeemResult | None, error: InviteError | None = None) -> dict:
    """Bloc `invite` des réponses (inscription, `POST /api/invites/redeem/`)."""
    if error is not None:
        return {'status': 'error', 'code': error.code, 'message': error.message}
    if result is None:
        return {'status': 'none'}
    r = result.redemption
    return {
        'status': 'redeemed' if result.created else 'already_redeemed',
        # display_name : prénom sinon pseudo (même règle que l'aperçu public) → « Tu es maintenant ami avec Léa ».
        'inviter': {
            'id': r.inviter_id,
            'username': r.inviter.username,
            'display_name': public_display_name(r.inviter),
        },
        'bypassed_waitlist': r.bypassed_waitlist,
        'is_acquisition': r.is_acquisition,
    }


# ---------------------------------------------------------------------------
# Limitation de débit (cache) : recherche / utilisation de codes, régénération.
# ---------------------------------------------------------------------------


def rate_limited(bucket: str, ident, *, limit: int, window_seconds: int) -> bool:
    """True si `ident` a dépassé `limit` appels sur la fenêtre (compteur fixe en cache)."""
    key = f'rl:invite:{bucket}:{ident}'
    if cache.add(key, 1, timeout=window_seconds):
        return False
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=window_seconds)
        return False
    return count > limit
