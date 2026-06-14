from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0066_recipe_embedding_hnsw'),
    ]

    operations = [
        migrations.AddField(
            model_name='post',
            name='cooking_time_minutes',
            field=models.IntegerField(
                blank=True,
                help_text='Temps de cuisine saisi ou dérivé de la recette (minutes)',
                null=True,
            ),
        ),
    ]
