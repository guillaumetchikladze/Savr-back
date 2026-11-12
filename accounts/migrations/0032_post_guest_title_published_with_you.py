"""wrd1 : les notifs `post_guest` déjà envoyées disent « a publié un repas avec toi » (texte du registre)."""

from django.db import migrations

OLD_SUFFIXES = (' t’a mis dans son plat', " t'a mis dans son plat")
NEW_SUFFIX = ' a publié un repas avec toi'


def forwards(apps, schema_editor):
    Notification = apps.get_model('accounts', 'Notification')
    for old in OLD_SUFFIXES:
        notifs = Notification.objects.filter(notification_type='post_guest', title__endswith=old).only('id', 'title')
        for notif in notifs:
            title = notif.title[: -len(old)] + NEW_SUFFIX
            Notification.objects.filter(pk=notif.pk).update(title=title)


class Migration(migrations.Migration):
    dependencies = [('accounts', '0031_post_guest_message_no_validation')]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
