from django.db import migrations


def seed_agrumes(apps, schema_editor):
    DietaryIngredientMatch = apps.get_model('recipes', 'DietaryIngredientMatch')
    rows = [
        ('Agrumes', 'orange'),
        ('Agrumes', 'citron'),
        ('Agrumes', 'lime'),
        ('Agrumes', 'citron vert'),
        ('Agrumes', 'pamplemousse'),
        ('Agrumes', 'clémentine'),
        ('Agrumes', 'mandarine'),
        ('Agrumes', 'bergamote'),
        ('Agrumes', 'yuzu'),
        ('Agrumes', 'kumquat'),
        ('Agrumes', 'zeste d\'orange'),
        ('Agrumes', 'jus d\'orange'),
        ('Agrumes', 'cédrat'),
    ]
    for label, kw in rows:
        DietaryIngredientMatch.objects.get_or_create(
            preference_label=label,
            match_keyword=kw,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0057_dietary_ingredient_match'),
    ]

    operations = [
        migrations.RunPython(seed_agrumes, noop_reverse),
    ]
