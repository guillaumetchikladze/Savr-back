from django.conf import settings
from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0025_alter_ingredient_embedding'),
    ]

    operations = [
        migrations.CreateModel(
            name='RecipeImportRequest',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('payload', models.JSONField(help_text="Données brutes envoyées par l'utilisateur")),
                ('status', models.CharField(choices=[('pending', 'En attente'), ('processing', 'En cours'), ('success', 'Terminé'), ('error', 'Erreur')], default='pending', max_length=20)),
                ('error_message', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('recipe', models.ForeignKey(blank=True, null=True, on_delete=models.deletion.CASCADE, related_name='import_requests', to='recipes.recipe')),
                ('user', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='recipe_import_requests', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]



