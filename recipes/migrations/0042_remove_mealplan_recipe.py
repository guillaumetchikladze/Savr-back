from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("recipes", "0041_remove_recipe_batch_from_step"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="mealplan",
            name="recipe",
        ),
    ]


