from django.db import migrations, models


def backfill_slot_keys(apps, schema_editor):
    MealPlan = apps.get_model('recipes', 'MealPlan')
    for mp in MealPlan.objects.all():
        if not getattr(mp, 'slot_key', None):
            mp.slot_key = mp.meal_time
            mp.save(update_fields=['slot_key'])


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0055_alter_mealplan_meal_time_breakfast'),
    ]

    operations = [
        migrations.AddField(
            model_name='mealplan',
            name='custom_label',
            field=models.CharField(blank=True, default='', max_length=80),
        ),
        migrations.AddField(
            model_name='mealplan',
            name='scheduled_time',
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='mealplan',
            name='slot_key',
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name='mealplan',
            name='meal_time',
            field=models.CharField(
                choices=[
                    ('breakfast', 'Petit-déjeuner'),
                    ('lunch', 'Déjeuner'),
                    ('dinner', 'Dîner'),
                    ('other', 'Autre'),
                ],
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_slot_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='mealplan',
            name='slot_key',
            field=models.CharField(
                blank=True,
                default='',
                help_text='lunch/dinner/breakfast ou UUID pour un créneau personnalisé (meal_time=other).',
                max_length=64,
            ),
        ),
        migrations.AlterUniqueTogether(
            name='mealplan',
            unique_together=set(),
        ),
        migrations.AddConstraint(
            model_name='mealplan',
            constraint=models.UniqueConstraint(
                fields=('user', 'date', 'slot_key'),
                name='mealplan_user_date_slotkey_uniq',
            ),
        ),
        migrations.AlterModelOptions(
            name='mealplan',
            options={
                'ordering': ['-date', 'meal_time', 'slot_key'],
            },
        ),
    ]
