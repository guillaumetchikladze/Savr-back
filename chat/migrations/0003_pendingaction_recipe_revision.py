from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0002_message_feedback'),
    ]

    operations = [
        migrations.AlterField(
            model_name='pendingaction',
            name='action_type',
            field=models.CharField(
                choices=[
                    ('meal_deletion', 'Meal deletion'),
                    ('meal_invitation', 'Meal invitation'),
                    ('recipe_revision', 'Recipe revision'),
                ],
                max_length=40,
            ),
        ),
    ]
