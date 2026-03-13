from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0010_pushdevice'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LoyaltyCard',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='Nom de la carte')),
                ('emoji', models.CharField(blank=True, help_text='Petit emoji pour identifier la carte dans l’UI', max_length=16, verbose_name='Emoji')),
                ('barcode_type', models.CharField(choices=[('ean13', 'EAN-13'), ('code128', 'Code 128'), ('qr', 'QR Code')], default='code128', max_length=32, verbose_name='Type de code barre')),
                ('encrypted_number', models.TextField(help_text='Numéro de carte chiffré avec une clé serveur (non lisible en base).', verbose_name='Numéro chiffré')),
                ('number_last4', models.CharField(blank=True, help_text='4 derniers chiffres pour affichage (non sensible).', max_length=8, verbose_name='4 derniers chiffres')),
                ('is_active', models.BooleanField(default=True, verbose_name='Active')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='loyalty_cards', to=settings.AUTH_USER_MODEL, verbose_name='Propriétaire')),
            ],
            options={
                'verbose_name': 'Carte de fidélité',
                'verbose_name_plural': 'Cartes de fidélité',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='loyaltycard',
            index=models.Index(fields=['owner', 'is_active'], name='loyaltycard_owner_active_idx'),
        ),
    ]

