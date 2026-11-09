"""
Exceptions globales curées : mot-clé présent seulement comme complément d'un nom composé
qui ne contient pas le produit visé. Les allergies restent prudentes : seuls les composés
réellement exempts de l'allergène sont listés (« beurre de cacahuète » reste un conflit
pour une allergie à l'arachide, il n'est exclu que du mot-clé « beurre » du lactose).
"""

from django.db import migrations

PLANT_MILKS = ['lait de coco', "lait d'amande", "lait d'avoine", 'lait de soja', 'lait de riz']
NOT_DAIRY_BUTTER = ['beurre de cacahuète', 'beurre de cacao']

SEEDS = [
    # (kind, label ou mot-clé, nom composé exclu)
    ('dislike', 'olive', "huile d'olive"),
    *[('allergy', 'lait', p) for p in PLANT_MILKS],
    ('allergy', 'crème', 'crème de coco'),
    *[('allergy', 'beurre', p) for p in NOT_DAIRY_BUTTER],
    ('allergy', 'noix', 'noix de muscade'),
    ('allergy', 'noix', 'noix de saint-jacques'),
    *[('diet', 'lait', p) for p in PLANT_MILKS],
    ('diet', 'crème', 'crème de coco'),
    *[('diet', 'beurre', p) for p in NOT_DAIRY_BUTTER],
]


def seed(apps, schema_editor):
    Exc = apps.get_model('recipes', 'PreferenceIngredientException')
    for kind, label, pattern in SEEDS:
        Exc.objects.get_or_create(kind=kind, label=label, ingredient_pattern=pattern, user=None, ingredient=None)


def unseed(apps, schema_editor):
    Exc = apps.get_model('recipes', 'PreferenceIngredientException')
    for kind, label, pattern in SEEDS:
        Exc.objects.filter(kind=kind, label=label, ingredient_pattern=pattern, user__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('recipes', '0072_preference_ingredient_exception'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
