"""API endpoints for licence / billing (waitlist skip + entitlements)."""

from django.contrib.auth import get_user_model
from django.core import signing
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .billing import (
    CHECKOUT_MODE_CONTACT_EMAIL,
    CHECKOUT_MODE_IAP,
    CHECKOUT_MODE_WEB_HANDOFF,
    create_billing_handoff,
    get_checkout_mode,
    get_license_offer,
    parse_billing_handoff_token,
)
from .serializers import UserSerializer

User = get_user_model()


@api_view(['GET'])
@permission_classes([AllowAny])
def billing_plans_view(request):
    """Offre + mode de checkout courant (piloté par settings)."""
    offer = get_license_offer()
    return Response(offer, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def billing_handoff_view(request):
    """
    Prépare le passage app → web (token signé).

    - contact_email : 501 — le client doit ouvrir un mailto
    - web_handoff   : renvoie url + token
    - iap           : 501 — pas encore branché (rebuild + StoreKit/Play Billing)
    """
    user = request.user
    if getattr(user, 'plan', None) == user.PLAN_PREMIUM and user.validated_at:
        return Response(
            {
                'status': 'already_active',
                'user': UserSerializer(user, context={'request': request}).data,
            },
            status=status.HTTP_200_OK,
        )

    mode = get_checkout_mode()
    if mode == CHECKOUT_MODE_CONTACT_EMAIL:
        offer = get_license_offer()
        return Response(
            {
                'status': 'contact_email',
                'checkout_mode': mode,
                'support_email': offer['support_email'],
                'message': 'Paiement en ligne bientôt disponible — contacte le support pour la licence.',
            },
            status=status.HTTP_200_OK,
        )

    if mode == CHECKOUT_MODE_IAP:
        return Response(
            {
                'status': 'iap_required',
                'checkout_mode': mode,
                'code': 'iap_not_configured',
                'message': 'Le paiement in-app (Apple / Google) n’est pas encore branché.',
            },
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )

    # web_handoff
    handoff = create_billing_handoff(user)
    return Response(
        {
            'status': 'web_handoff',
            'checkout_mode': CHECKOUT_MODE_WEB_HANDOFF,
            'handoff_url': handoff['url'],
            'expires_in': handoff['expires_in'],
        },
        status=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def billing_handoff_resolve_view(request):
    """
    Résout un token handoff pour la page web tchikook.fr.
    Ne révèle que le minimum nécessaire au checkout (pas de JWT app).
    """
    token = (request.data.get('token') or request.data.get('handoff') or '').strip()
    if not token:
        return Response({'error': 'token requis'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        payload = parse_billing_handoff_token(token)
    except signing.SignatureExpired:
        return Response(
            {'error': 'Lien expiré', 'code': 'handoff_expired'},
            status=status.HTTP_410_GONE,
        )
    except signing.BadSignature:
        return Response(
            {'error': 'Lien invalide', 'code': 'handoff_invalid'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = User.objects.filter(pk=payload['uid'], is_active=True).first()
    if not user:
        return Response({'error': 'Compte introuvable'}, status=status.HTTP_404_NOT_FOUND)

    offer = get_license_offer()
    return Response(
        {
            'user_id': user.id,
            'email': user.email,
            'username': user.username,
            'plan': user.plan,
            'is_validated': user.validated_at is not None,
            'already_premium': user.plan == user.PLAN_PREMIUM and user.validated_at is not None,
            'offer': {
                'product_id': offer['product_id'],
                'name': offer['name'],
                'currency': offer['currency'],
                'intervals': offer.get('intervals'),
                'default_interval': offer.get('default_interval'),
                'price_label': offer.get('price_label'),
                'price_cents': offer.get('price_cents'),
            },
        },
        status=status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def billing_status_view(request):
    """Statut licence après retour web / activation manuelle — pour poll côté app."""
    user = request.user
    return Response(
        {
            'plan': user.plan,
            'is_validated': user.validated_at is not None,
            'validated_at': user.validated_at,
            'is_premium': user.plan == user.PLAN_PREMIUM,
            'checkout_mode': get_checkout_mode(),
        },
        status=status.HTTP_200_OK,
    )
