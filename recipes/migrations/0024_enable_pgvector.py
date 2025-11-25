# Generated manually - Enable pgvector extension

from pgvector.django import VectorExtension
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0023_add_ingredient_embedding'),
    ]

    operations = [
        VectorExtension(),
    ]

