"""Push du dimanche des tablées (positive, jamais à 0, plafonnée)."""

from datetime import date, timedelta

from notifications.models import NotificationDelivery
from notifications.retention import send_tables_weekly, tables_weekly_for_user
from tables.models import Table, TableMember

SUNDAY = date(2026, 11, 8)
MONDAY = date(2026, 11, 2)


def _table(owner, *members, name='Les colocs'):
    table = Table.objects.create(name=name, created_by=owner)
    for i, u in enumerate((owner, *members)):
        TableMember.objects.create(table=table, user=u, role='owner' if i == 0 else 'member', status='accepted')
    return table


def test_tables_weekly_is_positive_only_and_capped(user, make_user, device, expo, paris, make_meal_plan):
    device(user)
    lea, tom = make_user('lea'), make_user('tom')
    table = _table(user, lea, tom)
    sunday = paris(19, day=SUNDAY)

    # Personne n'a cuisiné (planifié ne compte pas) : aucune push, jamais de « 0 sur 3 ».
    make_meal_plan(lea, day=MONDAY, custom_label='Dahl', meal_type='recipe')
    assert tables_weekly_for_user(user, now=sunday).status == 'nothing_to_say'

    make_meal_plan(lea, day=MONDAY, meal_time='dinner', custom_label='Dahl', meal_type='recipe', confirmed=True)
    assert send_tables_weekly(now=paris(18, day=SUNDAY)) == {}  # pas encore l'heure
    assert send_tables_weekly(now=sunday) == {'sent': 1}
    (message,) = expo.messages
    assert message['title'] == 'Ta tablée'
    assert message['body'] == 'Cette semaine, 1 sur 3 a cuisiné dans « Les colocs ».'
    assert message['data']['url'] == f'tchikook://tables/{table.id}' and message['data']['cooked_members'] == 1
    assert tables_weekly_for_user(user, now=sunday + timedelta(minutes=5)).status == 'duplicate'

    # Plafond quotidien (2 non transactionnelles déjà envoyées ce jour-là) : rien ne part.
    device(lea)
    for kind in ('friends_digest', 'weekly_recap'):
        NotificationDelivery.objects.create(user=lea, type=kind, category='x', created_at=sunday - timedelta(hours=1))
    assert tables_weekly_for_user(lea, now=sunday).status == 'capped'
    assert len(expo.messages) == 1

    # Catégorie « tables » coupée dans les préférences.
    device(tom)
    from notifications.models import NotificationPreference

    NotificationPreference.objects.create(user=tom, categories={'tables': False})
    assert tables_weekly_for_user(tom, now=sunday).status == 'muted'
