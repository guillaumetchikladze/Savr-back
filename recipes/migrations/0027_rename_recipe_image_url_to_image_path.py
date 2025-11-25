from django.db import migrations, models
from urllib.parse import urlparse


def strip_endpoint(apps, schema_editor):
    Recipe = apps.get_model('recipes', 'Recipe')
    for recipe in Recipe.objects.exclude(image_path__isnull=True).exclude(image_path=''):
        value = recipe.image_path
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc:
            new_path = parsed.path.lstrip('/')
            if new_path:
                recipe.image_path = new_path
                recipe.save(update_fields=['image_path'])


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0026_recipeimportrequest'),
    ]

    operations = [
        migrations.RenameField(
            model_name='recipe',
            old_name='image_url',
            new_name='image_path',
        ),
        migrations.AlterField(
            model_name='recipe',
            name='image_path',
            field=models.CharField(blank=True, help_text="Chemin relatif de l'image (ex: recipes/user/uuid.jpg)", max_length=500, null=True),
        ),
        migrations.RunPython(strip_endpoint, reverse_code=migrations.RunPython.noop),
    ]



