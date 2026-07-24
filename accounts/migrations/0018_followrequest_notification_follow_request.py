# Generated manually for follow request flow

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0017_notification_post_repost'),
    ]

    operations = [
        migrations.CreateModel(
            name='FollowRequest',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(
                    choices=[('pending', 'En attente'), ('accepted', 'Acceptée'), ('declined', 'Refusée')],
                    default='pending',
                    max_length=20,
                    verbose_name='Statut',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('requester', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sent_follow_requests',
                    to='accounts.user',
                    verbose_name='Demandeur',
                )),
                ('target', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='received_follow_requests',
                    to='accounts.user',
                    verbose_name='Cible',
                )),
            ],
            options={
                'verbose_name': 'Demande de suivi',
                'verbose_name_plural': 'Demandes de suivi',
                'ordering': ['-created_at'],
                'unique_together': {('requester', 'target')},
            },
        ),
        migrations.AddIndex(
            model_name='followrequest',
            index=models.Index(fields=['target', 'status'], name='followreq_target_status_idx'),
        ),
        migrations.AddIndex(
            model_name='followrequest',
            index=models.Index(fields=['requester', 'status'], name='followreq_requester_status_idx'),
        ),
        migrations.AlterField(
            model_name='notification',
            name='notification_type',
            field=models.CharField(
                choices=[
                    ('follow', 'Nouvel ami'),
                    ('follow_request', "Demande d'ami"),
                    ('recipe_reminder', 'Rappel de recette'),
                    ('recipe_shared', 'Recette partagée'),
                    ('achievement', 'Nouveau succès'),
                    ('meal_invitation', 'Invitation à un repas'),
                    ('photo_during_cooking', 'Photo pendant la cuisine'),
                    ('photo_after_cooking', 'Photo après la cuisine'),
                    ('photo_at_meal_time', "Photo à l'heure du repas"),
                    ('post_miam', 'Miam sur votre post'),
                    ('post_comment', 'Commentaire sur votre post'),
                    ('post_comment_mention', 'Vous êtes mentionné dans un commentaire'),
                    ('post_repost', 'Repost de votre post'),
                ],
                max_length=50,
                verbose_name='Type de notification',
            ),
        ),
    ]
