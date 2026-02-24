# Generated for manual shopping list items (no recipe batch)

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0045_shopping_list_v2_reset'),
    ]

    operations = [
        migrations.AlterField(
            model_name='shoppinglistitemquantity',
            name='recipe_batch',
            field=models.ForeignKey(
                blank=True,
                help_text='Null = quantité ajoutée manuellement (pas liée à une recette)',
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='shopping_list_item_quantities',
                to='recipes.recipebatch',
            ),
        ),
    ]
