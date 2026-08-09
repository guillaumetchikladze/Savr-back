from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0068_postrepost'),
    ]

    operations = [
        migrations.AddField(
            model_name='mealplan',
            name='dining_alone',
            field=models.BooleanField(
                default=False,
                help_text="L'hôte a confirmé qu'il mange seul (offramp step Convives)",
            ),
        ),
    ]
