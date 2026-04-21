# Generated manually: mealplan-level reminder + mealplan posts

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0062_recipebatch_meal_time_photo_reminder_task_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='mealplan',
            name='meal_time_photo_reminder_task_id',
            field=models.CharField(
                blank=True,
                default='',
                help_text='ID tâche Celery (rappel push photo à table) pour révocation si replanification',
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='post',
            name='meal_plan',
            field=models.ForeignKey(
                blank=True,
                help_text='Repas associé (nouveau workflow : post du repas).',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='posts',
                to='recipes.mealplan',
            ),
        ),
    ]

