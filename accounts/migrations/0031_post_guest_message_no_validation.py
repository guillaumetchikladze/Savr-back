"""ckd2 lot 3 suite : les notifs `post_guest` déjà envoyées n'invitent plus à republier (texte du registre)."""

from django.db import migrations

OLD = 'cuisiné ensemble. Tu peux le republier, ou te retirer.'
NEW = 'cuisiné ensemble. Ce n’est pas toi ? Tu peux te retirer.'


def forwards(apps, schema_editor):
    Notification = apps.get_model('accounts', 'Notification')
    for notif in Notification.objects.filter(notification_type='post_guest', message__contains=OLD).only('id', 'message'):
        message = notif.message.replace(OLD, NEW)
        if message.startswith('ton plat, '):
            message = 'Cuisiné ensemble.' + message[len('ton plat, cuisiné ensemble.') :]
        Notification.objects.filter(pk=notif.pk).update(message=message)


class Migration(migrations.Migration):
    dependencies = [('accounts', '0030_alter_notification_notification_type')]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
