from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0050_postcommentlike'),
        ('accounts', '0011_loyaltycard'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ShoppingListLoyaltyCard',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('card', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='shopping_list_links', to='accounts.loyaltycard')),
                ('shopping_list', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='loyalty_cards_links', to='recipes.shoppinglist')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='shoppinglistloyaltycard',
            index=models.Index(fields=['shopping_list'], name='shoplist_loyalty_list_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='shoppinglistloyaltycard',
            unique_together={('shopping_list', 'card')},
        ),
    ]

