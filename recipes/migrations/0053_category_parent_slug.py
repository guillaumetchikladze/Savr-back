from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0052_mealplanrecipebatch_portions_override'),
    ]

    operations = [
        migrations.AddField(
            model_name='category',
            name='parent',
            field=models.ForeignKey(
                blank=True,
                help_text='Section (ex. Pôle frais). Vide pour racines historiques sans hiérarchie.',
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='children',
                to='recipes.category',
            ),
        ),
        migrations.AddField(
            model_name='category',
            name='slug',
            field=models.SlugField(
                blank=True,
                help_text='Identifiant stable pour règles et migrations (feuilles uniquement de préférence).',
                max_length=80,
                null=True,
                unique=True,
            ),
        ),
    ]
