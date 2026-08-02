"""
Billing / licence premium — architecture Tchikook.

Couches :
1. Waitlist (validated_at) — accès app
2. Entitlements (plan free/premium) — features
3. Checkout provider — pluggable, hors IAP pour l'instant

Modes (`BILLING_CHECKOUT_MODE`) :
- contact_email : CTA in-app → mailto support (défaut actuel)
- web_handoff   : token signé → tchikook.fr (Stancer plus tard)
- iap           : App Store / Play (rebuild + lib native plus tard)

Toute activation premium passe par `activate_premium_license`
(admin manuel, webhook web, IAP receipt, etc.).
"""

from __future__ import annotations

import secrets

from django.conf import settings
from django.core import signing
from django.utils import timezone

CHECKOUT_MODE_CONTACT_EMAIL = 'contact_email'
CHECKOUT_MODE_WEB_HANDOFF = 'web_handoff'
CHECKOUT_MODE_IAP = 'iap'
VALID_CHECKOUT_MODES = {
    CHECKOUT_MODE_CONTACT_EMAIL,
    CHECKOUT_MODE_WEB_HANDOFF,
    CHECKOUT_MODE_IAP,
}

HANDOFF_SALT = 'tchikook.billing.handoff.v1'


def get_checkout_mode() -> str:
    mode = (getattr(settings, 'BILLING_CHECKOUT_MODE', '') or CHECKOUT_MODE_CONTACT_EMAIL).strip()
    if mode not in VALID_CHECKOUT_MODES:
        return CHECKOUT_MODE_CONTACT_EMAIL
    return mode


def get_support_email() -> str:
    return (
        (getattr(settings, 'BILLING_SUPPORT_EMAIL', '') or '').strip()
        or (getattr(settings, 'EMAIL_FROM_ADDRESS', '') or '').strip()
        or 'contact@tchikook.fr'
    )


def get_web_checkout_base_url() -> str:
    return (
        (getattr(settings, 'BILLING_WEB_CHECKOUT_BASE_URL', '') or '').strip().rstrip('/')
        or 'https://tchikook.fr/licence'
    )


def get_license_offer() -> dict:
    """Offre simple : mensuel vs annuel (plancher 39,99 €/an)."""
    monthly_label = getattr(settings, 'BILLING_MONTHLY_PRICE_LABEL', '4,99 €')
    yearly_label = getattr(settings, 'BILLING_YEARLY_PRICE_LABEL', '39,99 €')
    yearly_eq = getattr(settings, 'BILLING_YEARLY_MONTHLY_EQ_LABEL', '3,33 €')
    # 4,99 × 12 = 59,88 ≈ 59,99 € → économie ≈ 20 € ≈ ~4 mois
    savings = getattr(settings, 'BILLING_YEARLY_SAVINGS_LABEL', '~4 mois offerts')
    list_price = getattr(settings, 'BILLING_YEARLY_LIST_PRICE_LABEL', '59,99 €')

    return {
        'product_id': 'tchikook_premium',
        'name': 'Tchikook Pro',
        'tagline': 'Accès immédiat, recettes assistées par IA, planning et courses — sans attendre la file.',
        'currency': getattr(settings, 'BILLING_LICENSE_CURRENCY', 'eur'),
        'default_interval': 'yearly',
        'intervals': {
            'monthly': {
                'id': 'premium_monthly',
                'price_cents': int(getattr(settings, 'BILLING_MONTHLY_PRICE_CENTS', 499)),
                'price_label': monthly_label,
                'period_short': '/ mois',
                'billing_note': f'Facturé {monthly_label} chaque mois',
            },
            'yearly': {
                'id': 'premium_yearly',
                'price_cents': int(getattr(settings, 'BILLING_YEARLY_PRICE_CENTS', 3999)),
                'price_label': yearly_label,
                'list_price_label': list_price,
                'period_short': '/ an',
                'monthly_equivalent_label': f'{yearly_eq}/mois',
                'weekly_equivalent_label': getattr(settings, 'BILLING_YEARLY_WEEKLY_EQ_LABEL', '0,77 €'),
                'billing_note': f'Facturé {yearly_label} une fois par an',
                'savings_label': savings,
                'savings_amount_label': getattr(settings, 'BILLING_YEARLY_SAVINGS_AMOUNT_LABEL', '~20 €'),
                'recommended': True,
                'badge': '~4 mois offerts',
            },
        },
        'checkout_mode': get_checkout_mode(),
        'support_email': get_support_email(),
        'web_checkout_base_url': get_web_checkout_base_url(),
        'highlights': [
            'Passe devant la liste d’attente dès aujourd’hui',
            'Laisse l’IA inventer et adapter tes recettes',
            'Gagne du temps sur planning et courses',
            'Importe une recette depuis un lien en un geste',
        ],
        'social_proof': 'Réponse sous 24 h · instructions de paiement par email',
        'free_note': 'Sans Pro, tu restes sur la liste d’attente — on t’écrit dès que c’est ton tour.',
        'price_cents': int(getattr(settings, 'BILLING_YEARLY_PRICE_CENTS', 3999)),
        'price_label': yearly_label,
        'billing_period': 'yearly',
        'billing_period_label': '/ an',
    }


def activate_premium_license(user, *, source: str = 'purchase') -> dict:
    """
    Point unique d’activation licence :
    - validated_at (skip waitlist)
    - plan=premium (entitlements)
    """
    now = timezone.now()
    update_fields = []
    if user.validated_at is None:
        user.validated_at = now
        update_fields.append('validated_at')
    if getattr(user, 'plan', None) != user.PLAN_PREMIUM:
        user.plan = user.PLAN_PREMIUM
        update_fields.append('plan')
    if update_fields:
        user.save(update_fields=update_fields)
    return {
        'activated': True,
        'source': source,
        'validated_at': user.validated_at,
        'plan': user.plan,
    }


def _handoff_max_age_seconds() -> int:
    return int(getattr(settings, 'BILLING_HANDOFF_TTL_SECONDS', 600))


def create_billing_handoff(user) -> dict:
    """
    Crée un token one-shot court TTL pour ouvrir tchikook.fr déjà authentifié.
    Prêt pour Stancer / page licence web — non utilisé tant que checkout_mode != web_handoff.
    """
    nonce = secrets.token_urlsafe(12)
    payload = {
        'uid': user.id,
        'email': (user.email or '').strip().lower(),
        'nonce': nonce,
        'purpose': 'license_checkout',
    }
    token = signing.dumps(payload, salt=HANDOFF_SALT, compress=True)
    base = get_web_checkout_base_url()
    sep = '&' if '?' in base else '?'
    url = f'{base}{sep}handoff={token}'
    return {
        'token': token,
        'url': url,
        'expires_in': _handoff_max_age_seconds(),
    }


def parse_billing_handoff_token(token: str) -> dict:
    """Valide le token handoff (à consommer côté page web / API publique)."""
    payload = signing.loads(
        token,
        salt=HANDOFF_SALT,
        max_age=_handoff_max_age_seconds(),
    )
    if not isinstance(payload, dict) or payload.get('purpose') != 'license_checkout':
        raise signing.BadSignature('invalid purpose')
    if not payload.get('uid'):
        raise signing.BadSignature('missing uid')
    return payload
