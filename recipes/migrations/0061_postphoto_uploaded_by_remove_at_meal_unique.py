# Generated manually for meal-plan posts: multiple at_meal_time per batch + uploader tracking

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('recipes', '0060_alter_preferencemapping_id_and_more'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='postphoto',
            name='unique_photo_type_per_post',
        ),
        migrations.RemoveConstraint(
            model_name='postphoto',
            name='unique_photo_type_per_batch',
        ),
        migrations.AddField(
            model_name='postphoto',
            name='uploaded_by',
            field=models.ForeignKey(
                blank=True,
                help_text='Utilisateur qui a uploadé la photo (suppression réservée à cet utilisateur si défini)',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='uploaded_post_photos',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
