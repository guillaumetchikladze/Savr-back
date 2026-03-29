from django.db import migrations, models


def set_onboarding_done_for_existing_users(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    User.objects.all().update(onboarding_completed=True)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0011_loyaltycard'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='allergies',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='user',
            name='food_dislikes',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='user',
            name='onboarding_completed',
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(set_onboarding_done_for_existing_users, migrations.RunPython.noop),
    ]
