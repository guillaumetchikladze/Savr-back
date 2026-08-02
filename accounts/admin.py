from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils import timezone
from django.conf import settings

from .models import User, UserBlock, AllowedEmail
from .billing import activate_premium_license


def _enqueue_access_granted_email(user):
    try:
        from emails.services import enqueue_email

        from_email = (getattr(settings, 'EMAIL_FROM_ADDRESS', '') or '').strip() or 'noreply@tchikook.fr'
        enqueue_email(
            from_email=from_email,
            to_email=user.email,
            subject='C’est enfin ton tour — bienvenue sur Tchikook',
            content={
                'template_name': 'emails/access_granted',
                'context': {
                    'username': user.username or user.email,
                },
            },
            action_name='access_granted',
            priority='HIGH',
            user_id=user.id,
        )
    except Exception:
        pass


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = [
        'email',
        'username',
        'is_validated_display',
        'validated_at',
        'plan',
        'level',
        'is_staff',
        'created_at',
    ]
    list_filter = ['plan', 'is_staff', 'is_superuser', 'level', 'validated_at']
    search_fields = ['email', 'username']
    actions = ['approve_users', 'activate_premium_licenses']
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Gamification', {'fields': ('level', 'experience_points')}),
        ('Accès & abonnement', {'fields': ('validated_at', 'plan', 'onboarding_completed')}),
    )

    @admin.display(boolean=True, description='Validé')
    def is_validated_display(self, obj):
        return obj.validated_at is not None

    @admin.action(description='Approuver (liste d’attente)')
    def approve_users(self, request, queryset):
        now = timezone.now()
        pending = queryset.filter(validated_at__isnull=True)
        count = 0
        for user in pending:
            user.validated_at = now
            user.save(update_fields=['validated_at'])
            _enqueue_access_granted_email(user)
            count += 1
        self.message_user(request, f'{count} compte(s) approuvé(s).')

    @admin.action(description='Activer licence premium (+ skip waitlist)')
    def activate_premium_licenses(self, request, queryset):
        count = 0
        for user in queryset:
            was_pending = user.validated_at is None
            activate_premium_license(user, source='admin_manual')
            if was_pending:
                _enqueue_access_granted_email(user)
            count += 1
        self.message_user(request, f'{count} licence(s) premium activée(s).')


@admin.register(AllowedEmail)
class AllowedEmailAdmin(admin.ModelAdmin):
    list_display = ['email', 'note', 'created_at']
    search_fields = ['email', 'note']
    ordering = ['-created_at']


@admin.register(UserBlock)
class UserBlockAdmin(admin.ModelAdmin):
    list_display = ['blocker', 'blocked', 'created_at']
    list_filter = ['created_at']
    search_fields = ['blocker__username', 'blocker__email', 'blocked__username', 'blocked__email']
    raw_id_fields = ['blocker', 'blocked']
    ordering = ['-created_at']
