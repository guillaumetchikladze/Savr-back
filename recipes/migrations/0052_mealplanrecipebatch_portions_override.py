from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recipes", "0051_shoppinglistloyaltycard"),
    ]

    operations = [
        migrations.AddField(
            model_name="mealplanrecipebatch",
            name="portions",
            field=models.IntegerField(
                null=True,
                blank=True,
                help_text="Nombre de portions pour ce batch dans ce repas (None = suit le nombre de personnes)",
            ),
        ),
        migrations.AddField(
            model_name="mealplanrecipebatch",
            name="is_portions_overridden",
            field=models.BooleanField(
                default=False,
                help_text="True si l'utilisateur a détaché les portions du nombre de personnes",
            ),
        ),
        migrations.RemoveField(
            model_name="mealplanrecipebatch",
            name="ratio",
        ),
    ]

