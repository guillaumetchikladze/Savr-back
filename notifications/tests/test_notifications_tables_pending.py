"""Push du dimanche des tablées (positive, jamais à 0, plafonnée) et rappel du soir « Je l'ai déjà »."""

from datetime import date, timedelta

from notifications.models import NotificationDelivery
from notifications.retention import cooked_pending_for_user, send_tables_weekly, tables_weekly_for_user
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


def test_cooked_pending_reminder_once_per_evening_and_respects_dismissal(
    user, device, expo, paris, make_recipe, make_meal_plan
):
    from recipes.models import CookedConfirmationDismissal

    device(user)
    today = paris(12).date()
    lunch = make_meal_plan(user, day=today, meal_time='lunch', recipes=[make_recipe(user, 'Quiche')])
    dinner = make_meal_plan(user, day=today, meal_time='dinner', custom_label='Pâtes au pesto', meal_type='recipe')
    make_meal_plan(user, day=today - timedelta(days=1), meal_time='dinner', custom_label='Hier', meal_type='recipe')

    CookedConfirmationDismissal.objects.create(user=user, meal_plan=dinner)  # « Pas fait »
    assert cooked_pending_for_user(user, now=paris(21, 15)).status == 'sent'
    (message,) = expo.messages
    assert message['body'] == 'Tu as cuisiné Quiche ? Un tap suffit.'
    data = message['data']
    assert (data['type'], data['action'], data['meal_plan_id']) == ('cooked_pending', 'cooked_prompt', lunch.id)
    assert (data['source'], data['kind']) == ('cooking', 'meal_time_photo_reminder')  # app 1.0.9
    assert data['date'] == today.isoformat()

    # Un seul rappel par soir, même si un autre repas redevient éligible.
    CookedConfirmationDismissal.objects.all().delete()
    assert cooked_pending_for_user(user, now=paris(21, 45)).status == 'duplicate'
    assert len(expo.messages) == 1


def test_cooked_pending_never_asks_twice_for_the_same_meal(user, device, expo, paris, make_recipe, make_meal_plan):
    device(user)
    today = paris(12).date()
    meal = make_meal_plan(user, day=today, meal_time='lunch', recipes=[make_recipe(user, 'Quiche')])
    # Déjà relancé (push de fin de cuisson ou rappel d'hier) : même clé `cooked_prompt:{id}`.
    NotificationDelivery.objects.create(
        user=user, type='cooked_prompt', category='cooking_reminders', dedupe_key=f'cooked_prompt:{meal.id}'
    )
    assert cooked_pending_for_user(user, now=paris(21)).status == 'duplicate'
    assert expo.messages == []


def test_cooking_mode_prompt_skips_dismissed_meal(user, device, expo, make_recipe, make_meal_plan):
    from accounts.tasks import send_meal_time_photo_reminder_push
    from recipes.models import CookedConfirmationDismissal

    device(user)
    meal = make_meal_plan(user, meal_time='dinner', recipes=[make_recipe(user, 'Quiche')], confirmed=True)
    CookedConfirmationDismissal.objects.create(user=user, meal_plan=meal)
    send_meal_time_photo_reminder_push(user.id, meal.id)
    assert expo.messages == []
