from django.db import migrations

# Version en production au moment de l'ajout du contrôle (1.0.9 sera la première à l'embarquer).
CURRENT_VERSION = '1.0.8'

SEED = {
    # trackId App Store de com.tchikook.app (itunes.apple.com/lookup?bundleId=com.tchikook.app).
    'ios': 'https://apps.apple.com/app/id6794335935',
    'android': 'https://play.google.com/store/apps/details?id=com.tchikook.app',
}


def seed(apps, schema_editor):
    AppVersionPolicy = apps.get_model('appversion', 'AppVersionPolicy')
    for platform, store_url in SEED.items():
        AppVersionPolicy.objects.get_or_create(
            platform=platform,
            defaults={
                'latest_version': CURRENT_VERSION,
                'min_supported_version': CURRENT_VERSION,
                'store_url': store_url,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ('appversion', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
