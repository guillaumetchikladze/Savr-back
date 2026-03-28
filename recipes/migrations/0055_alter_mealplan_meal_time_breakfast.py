from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0054_post_report'),
    ]

    operations = [
        migrations.AlterField(
            model_name='mealplan',
            name='meal_time',
            field=models.CharField(
                choices=[
                    ('breakfast', 'Petit-déjeuner'),
                    ('lunch', 'Déjeuner'),
                    ('dinner', 'Dîner'),
                ],
                max_length=20,
            ),
        ),
    ]
