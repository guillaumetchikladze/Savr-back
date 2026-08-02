from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission, IsAuthenticated


class IsValidated(IsAuthenticated):
    """
    Authenticated user whose account has been admin-validated (validated_at set).
    Pending waitlist users get 403 with a stable error code for the mobile client.
    """

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False
        if getattr(request.user, 'validated_at', None) is None:
            raise PermissionDenied(
                detail={
                    'error': 'Compte en attente de validation.',
                    'code': 'not_validated',
                }
            )
        return True


class HasAIFeature(BasePermission):
    """Requires validated user with AI entitlement (respects AI_PAYWALL_ENABLED)."""

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return False
        if getattr(user, 'validated_at', None) is None:
            raise PermissionDenied(
                detail={
                    'error': 'Compte en attente de validation.',
                    'code': 'not_validated',
                }
            )
        from accounts.entitlements import user_has_feature, feature_locked_payload

        if not user_has_feature(user, 'ai'):
            raise PermissionDenied(detail=feature_locked_payload('ai'))
        return True
