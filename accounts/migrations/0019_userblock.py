from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0018_followrequest_notification_follow_request'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserBlock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('blocked', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='blocks_received',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='Bloqué',
                )),
                ('blocker', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='blocks_initiated',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='Bloqueur',
                )),
            ],
            options={
                'verbose_name': 'Blocage utilisateur',
                'verbose_name_plural': 'Blocages utilisateurs',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='userblock',
            index=models.Index(fields=['blocker', 'created_at'], name='userblock_blocker_created_idx'),
        ),
        migrations.AddIndex(
            model_name='userblock',
            index=models.Index(fields=['blocked'], name='userblock_blocked_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='userblock',
            unique_together={('blocker', 'blocked')},
        ),
    ]
