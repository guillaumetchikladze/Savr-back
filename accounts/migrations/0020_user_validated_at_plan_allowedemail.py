# Generated manually for waitlist + entitlements

from django.db import migrations, models
from django.utils import timezone


def approve_existing_users(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    now = timezone.now()
    User.objects.filter(validated_at__isnull=True).update(validated_at=now)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0019_userblock'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='plan',
            field=models.CharField(
                choices=[('free', 'Free'), ('premium', 'Premium')],
                default='free',
                help_text="Plan d'abonnement (socle paywall).",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='user',
            name='validated_at',
            field=models.DateTimeField(
                blank=True,
                help_text="Date de validation admin. Null = compte en liste d'attente.",
                null=True,
            ),
        ),
        migrations.CreateModel(
            name='AllowedEmail',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(max_length=254, unique=True)),
                ('note', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Email autorisé',
                'verbose_name_plural': 'Emails autorisés',
                'ordering': ['-created_at'],
            },
        ),
        migrations.RunPython(approve_existing_users, noop_reverse),
    ]
