# Generated manually to migrate Step from recipe_batch to recipe

from django.db import migrations, models
import django.db.models.deletion


def populate_recipe_from_batch(apps, schema_editor):
    """Remplir le champ recipe depuis recipe_batch.recipe pour les steps existants"""
    Step = apps.get_model('recipes', 'Step')
    RecipeBatch = apps.get_model('recipes', 'RecipeBatch')
    
    # Pour chaque step qui a un recipe_batch, remplir recipe depuis recipe_batch.recipe
    for step in Step.objects.filter(recipe_batch__isnull=False):
        if step.recipe_batch and step.recipe_batch.recipe:
            step.recipe = step.recipe_batch.recipe
            step.save()


def reverse_populate_recipe_from_batch(apps, schema_editor):
    """Opération inverse : ne rien faire car on ne peut pas déduire recipe_batch depuis recipe"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0040_add_batch_is_cooked'),
    ]

    operations = [
        # 1. Ajouter le champ recipe comme nullable
        migrations.AddField(
            model_name='step',
            name='recipe',
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='steps',
                to='recipes.recipe',
                help_text='Recette associée'
            ),
        ),
        # 2. Remplir recipe depuis recipe_batch.recipe
        migrations.RunPython(populate_recipe_from_batch, reverse_populate_recipe_from_batch),
        # 3. Rendre recipe non-nullable
        migrations.AlterField(
            model_name='step',
            name='recipe',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='steps',
                to='recipes.recipe',
                help_text='Recette associée'
            ),
        ),
        # 4. Mettre à jour les Meta (ordering et unique_together)
        migrations.AlterModelOptions(
            name='step',
            options={'ordering': ['recipe', 'order']},
        ),
        migrations.AlterUniqueTogether(
            name='step',
            unique_together={('recipe', 'order')},
        ),
        # 5. Supprimer le champ recipe_batch
        migrations.RemoveField(
            model_name='step',
            name='recipe_batch',
        ),
    ]

