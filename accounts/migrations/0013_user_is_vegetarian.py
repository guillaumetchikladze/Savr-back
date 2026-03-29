from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0012_user_food_dislikes_allergies_onboarding'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='is_vegetarian',
            field=models.BooleanField(default=False),
        ),
    ]
