# HNSW sur embedding 512d — à appliquer après backfill des vecteurs.

from django.db import migrations
from pgvector.django import HnswIndex


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0065_recipe_search_index_512d'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='recipe',
            index=HnswIndex(
                name='recipe_embedding_hnsw_idx',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
        ),
    ]
