from django.db import migrations
import pgvector.django


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0027_rename_recipe_image_url_to_image_path'),
    ]

    operations = [
        migrations.AddField(
            model_name='recipe',
            name='embedding',
            field=pgvector.django.VectorField(blank=True, dimensions=384, help_text='Embedding sémantique pour la recherche', null=True),
        ),
    ]



