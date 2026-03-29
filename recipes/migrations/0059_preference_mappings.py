from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('recipes', '0058_agrumes_dietary_match'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PreferenceMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('allergy', 'Allergie'), ('dislike', 'Goût (n’aime pas)')], db_index=True, max_length=16)),
                ('label', models.CharField(db_index=True, max_length=120)),
                ('keywords', models.JSONField(blank=True, default=list)),
                ('status', models.CharField(choices=[('pending', 'À valider'), ('validated', 'Validé')], db_index=True, default='pending', max_length=16)),
                ('usage_count', models.IntegerField(default=0)),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_preference_mappings', to=settings.AUTH_USER_MODEL)),
                ('forked_from', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='forks', to='recipes.preferencemapping')),
            ],
            options={
                'ordering': ['kind', 'label'],
                'unique_together': {('kind', 'label')},
            },
        ),
        migrations.CreateModel(
            name='UserPreferenceMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('allergy', 'Allergie'), ('dislike', 'Goût (n’aime pas)')], db_index=True, max_length=16)),
                ('label', models.CharField(db_index=True, max_length=120)),
                ('keywords', models.JSONField(blank=True, default=list)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('base_mapping', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='user_overrides', to='recipes.preferencemapping')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='preference_mapping_overrides', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['kind', 'label'],
                'unique_together': {('user', 'kind', 'label')},
            },
        ),
        migrations.AddIndex(
            model_name='preferencemapping',
            index=models.Index(fields=['kind', 'status', 'label'], name='prefmap_kind_status_label_idx'),
        ),
        migrations.AddIndex(
            model_name='preferencemapping',
            index=models.Index(fields=['kind', 'label'], name='prefmap_kind_label_idx'),
        ),
        migrations.AddIndex(
            model_name='userpreferencemapping',
            index=models.Index(fields=['user', 'kind', 'label'], name='upm_user_kind_label_idx'),
        ),
    ]

