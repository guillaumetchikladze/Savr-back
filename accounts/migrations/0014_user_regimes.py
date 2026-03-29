from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0013_user_is_vegetarian'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='regimes',
            field=models.JSONField(blank=True, default=list),
        ),
    ]

