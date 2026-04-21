# Generated manually: temp meal-plan photo uploads

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0063_mealplan_reminder_task_and_post_mealplan'),
    ]

    operations = [
        migrations.AddField(
            model_name='postphoto',
            name='meal_plan',
            field=models.ForeignKey(
                blank=True,
                help_text='Repas associé (upload temporaire avant attribution à une recette)',
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='draft_photos',
                to='recipes.mealplan',
            ),
        ),
    ]

