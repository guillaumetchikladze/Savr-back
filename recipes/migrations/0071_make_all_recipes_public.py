from django.db import migrations


def make_all_recipes_public(apps, schema_editor):
    Recipe = apps.get_model('recipes', 'Recipe')
    Recipe.objects.filter(is_public=False).update(is_public=True)


def noop_reverse(apps, schema_editor):
    # Intentionally irreversible: private state was discarded.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0070_postphoto_mealplan_temp_idx'),
    ]

    operations = [
        migrations.RunPython(make_all_recipes_public, noop_reverse),
    ]
