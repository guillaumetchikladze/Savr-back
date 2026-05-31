# Generated manually for semantic search v2

import pgvector.django.vector
from django.db import migrations, models


def nullify_embeddings(apps, schema_editor):
    """Les vecteurs 384d ne sont pas compatibles avec la colonne 512d."""
    schema_editor.execute("UPDATE recipes_recipe SET embedding = NULL WHERE embedding IS NOT NULL;")
    schema_editor.execute("UPDATE recipes_ingredient SET embedding = NULL WHERE embedding IS NOT NULL;")


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0064_postphoto_meal_plan_temp_upload'),
    ]

    operations = [
        migrations.AddField(
            model_name='recipe',
            name='search_context_tags',
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='recipe',
            name='search_index_text',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='recipe',
            name='search_index_hash',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='recipe',
            name='search_indexed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='recipe',
            name='search_index_status',
            field=models.CharField(
                choices=[('pending', 'En cours'), ('ready', 'Prêt'), ('failed', 'Échec')],
                default='pending',
                max_length=16,
            ),
        ),
        migrations.RunPython(nullify_embeddings, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='ingredient',
            name='embedding',
            field=pgvector.django.vector.VectorField(
                blank=True,
                dimensions=512,
                help_text='Vecteur d\'embedding pour la recherche sémantique (nomic 512d)',
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name='recipe',
            name='embedding',
            field=pgvector.django.vector.VectorField(
                blank=True,
                dimensions=512,
                help_text='Embedding sémantique pour la recherche (nomic 512d)',
                null=True,
            ),
        ),
        migrations.RunSQL(
            sql="""
                CREATE INDEX IF NOT EXISTS recipe_search_index_text_trgm_gin
                ON recipes_recipe
                USING gin(search_index_text gin_trgm_ops);
            """,
            reverse_sql="DROP INDEX IF EXISTS recipe_search_index_text_trgm_gin;",
        ),
    ]
