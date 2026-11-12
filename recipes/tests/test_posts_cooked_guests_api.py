"""Posts : convives directs du plat (ckd2 lot 3) — « Avec qui » / @Léa dans « J'ai cuisiné »."""

from datetime import date

import pytest

from accounts.models import HouseholdMember, Notification, UserBlock
from recipes.models import MealInvitation, Post, PostRepost
from tables.models import Table, TableMember

pytestmark = pytest.mark.django_db

COOKED_URL = '/api/posts/cooked/'
CANDIDATES_URL = '/api/posts/guest-candidates/'
EVENING = '2026-11-08T20:15:00+01:00'


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()


@pytest.fixture
def tom(make_user):
    return make_user('tom', first_name='Tom')


@pytest.fixture
def lea(make_user, follow, tom):
    lea = make_user('lea', first_name='Léa')
    follow(tom, lea)
    follow(lea, tom)
    return lea


@pytest.fixture
def pauline(make_user, follow, tom):
    """Pauline : foyer de Tom (compte accepté)."""
    pauline = make_user('pauline', first_name='Pauline')
    follow(tom, pauline)
    follow(pauline, tom)
    HouseholdMember.objects.create(owner=tom, user=pauline, status='accepted')
    return pauline


def _cook(api, user, capture, **data):
    data.setdefault('cooked_at', EVENING)
    with capture(execute=True):
        return api.as_user(user).post(COOKED_URL, data, format='json')


def _guest_ids(post_id):
    post = Post.objects.get(id=post_id)
    return set(
        MealInvitation.objects.filter(meal_plan_id=post.meal_plan_id, status='accepted').values_list(
            'invitee_id', flat=True
        )
    )


def test_guest_added_directly_notified_and_shown_on_post(
    api, tom, lea, make_recipe, django_capture_on_commit_callbacks
):
    recipe = make_recipe(tom, 'Pâtes au canard')

    resp = _cook(
        api, tom, django_capture_on_commit_callbacks, recipe_id=recipe.id, comment='Merci @Léa', guest_ids=[lea.id]
    )

    assert resp.status_code == 201, resp.data
    inv = MealInvitation.objects.get(invitee=lea)
    assert inv.status == 'accepted' and inv.source == 'direct' and inv.inviter_id == tom.id
    notif = Notification.objects.get(user=lea, notification_type='post_guest')
    assert notif.related_post_id == resp.data['id']
    assert notif.title == 'tom a publié un repas avec toi'
    # Aucune validation demandée : la notif informe, propose seulement de se retirer.
    assert notif.message == '«\u00a0Pâtes au canard\u00a0», cuisiné ensemble. Ce n’est pas toi ? Tu peux te retirer.'
    assert 'republier' not in notif.message
    (guest,) = resp.data['guests']
    assert guest['id'] == lea.id and guest['first_name'] == 'Léa' and guest['can_view_profile'] is True

    # Léa voit le plat, peut le republier (« cuisiné avec ») et le carnet de Tom dit « avec Léa ».
    detail = api.as_user(lea).get(f'/api/posts/{resp.data["id"]}/')
    assert detail.status_code == 200 and detail.data['can_repost'] is True
    assert detail.data['guests'][0]['can_view_profile'] is True  # soi
    carnet = api.as_user(tom).get('/api/carnet/me/').json()
    entry = next(e for e in carnet['results'] if e.get('post_id') == resp.data['id'])
    assert entry['guests_count'] == 1 and entry['guests'][0]['first_name'] == 'Léa'


def test_only_friends_or_table_mates_can_be_added(
    api, tom, lea, make_user, follow, make_recipe, django_capture_on_commit_callbacks
):
    stranger = make_user('stranger')
    blocked = make_user('blocked')
    follow(tom, blocked)
    UserBlock.objects.create(blocker=blocked, blocked=tom)
    one_way = make_user('fan')
    follow(one_way, tom)  # suit Tom : ami·e dans un sens
    mate = make_user('mate', first_name='Inès')
    table = Table.objects.create(name='Les colocs')
    TableMember.objects.create(table=table, user=tom, role='owner', status='accepted')
    TableMember.objects.create(table=table, user=mate, status='accepted')
    invited_only = make_user('invited')
    TableMember.objects.create(table=table, user=invited_only, status='invited')

    resp = _cook(
        api,
        tom,
        django_capture_on_commit_callbacks,
        recipe_id=make_recipe(tom).id,
        guest_ids=[lea.id, stranger.id, blocked.id, one_way.id, mate.id, invited_only.id, tom.id, 999999],
    )

    assert resp.status_code == 201, resp.data
    assert _guest_ids(resp.data['id']) == {lea.id, one_way.id, mate.id}
    assert not Notification.objects.filter(user__in=[stranger, blocked, invited_only], notification_type='post_guest')


def test_invalid_guest_ids_payload_is_rejected(api, tom, make_recipe, django_capture_on_commit_callbacks):
    resp = _cook(api, tom, django_capture_on_commit_callbacks, recipe_id=make_recipe(tom).id, guest_ids='lea')

    assert resp.status_code == 400 and resp.data['code'] == 'invalid_guest_ids'
    assert not Post.objects.exists()


def test_household_pre_added_from_planning_is_silent(
    api, tom, lea, pauline, make_meal_plan, make_recipe, django_capture_on_commit_callbacks
):
    mp = make_meal_plan(tom, day=date(2026, 11, 8), meal_time='dinner', recipes=[make_recipe(tom, 'Risotto')])

    resp = _cook(
        api,
        tom,
        django_capture_on_commit_callbacks,
        meal_plan_id=mp.id,
        guest_ids=[lea.id],
        household_guest_ids=[pauline.id],
    )

    assert resp.status_code == 201, resp.data
    assert MealInvitation.objects.get(invitee=pauline).source == 'household'
    assert not Notification.objects.filter(user=pauline, notification_type='post_guest').exists()
    assert Notification.objects.filter(user=lea, notification_type='post_guest').exists()
    assert [g['first_name'] for g in resp.data['guests']] == ['Léa', 'Pauline']


def test_household_ids_outside_planning_are_ordinary_guests(
    api, tom, lea, pauline, make_recipe, django_capture_on_commit_callbacks
):
    # Plat tapé (pas de meal_plan_id) : jamais de préajout muet, Pauline est une convive comme une autre.
    resp = _cook(
        api,
        tom,
        django_capture_on_commit_callbacks,
        recipe_id=make_recipe(tom).id,
        household_guest_ids=[pauline.id, lea.id],
    )

    assert resp.status_code == 201, resp.data
    assert set(MealInvitation.objects.values_list('source', flat=True)) == {'direct'}
    assert Notification.objects.filter(notification_type='post_guest').count() == 2


def test_existing_planning_invitation_becomes_guest_without_second_notification(
    api, tom, lea, make_meal_plan, make_recipe, django_capture_on_commit_callbacks
):
    mp = make_meal_plan(tom, day=date(2026, 11, 8), meal_time='dinner', recipes=[make_recipe(tom)])
    accepted = MealInvitation.objects.create(inviter=tom, invitee=lea, meal_plan=mp, status='accepted')

    resp = _cook(api, tom, django_capture_on_commit_callbacks, meal_plan_id=mp.id, guest_ids=[lea.id])

    assert resp.status_code == 201, resp.data
    accepted.refresh_from_db()
    assert accepted.source == 'invite'  # donnée existante gardée telle quelle
    assert not Notification.objects.filter(notification_type='post_guest').exists()


def test_private_post_holds_guests_until_published(api, tom, lea, make_recipe, django_capture_on_commit_callbacks):
    resp = _cook(
        api,
        tom,
        django_capture_on_commit_callbacks,
        recipe_id=make_recipe(tom, 'Quiche').id,
        guest_ids=[lea.id],
        publish=False,
    )

    assert resp.status_code == 201, resp.data
    post = Post.objects.get(id=resp.data['id'])
    assert not MealInvitation.objects.exists()  # Léa ne voit rien d'un plat « rien que pour toi »
    assert not Notification.objects.filter(notification_type='post_guest').exists()
    assert post.held_guests == {'guest_ids': [lea.id], 'household_guest_ids': []}
    assert api.as_user(lea).get(f'/api/posts/{post.id}/').status_code == 404

    with django_capture_on_commit_callbacks(execute=True):
        published = api.as_user(tom).post(f'/api/posts/{post.id}/publish/', {}, format='json')

    assert published.status_code == 200, published.data
    post.refresh_from_db()
    assert post.held_guests is None
    assert _guest_ids(post.id) == {lea.id}
    assert Notification.objects.filter(user=lea, notification_type='post_guest', related_post_id=post.id).exists()


def test_guest_can_leave_the_dish(api, tom, lea, make_recipe, django_capture_on_commit_callbacks):
    resp = _cook(api, tom, django_capture_on_commit_callbacks, recipe_id=make_recipe(tom).id, guest_ids=[lea.id])
    post_id = resp.data['id']
    assert api.as_user(lea).post(f'/api/posts/{post_id}/repost/').status_code == 201

    assert api.as_user(tom).post(f'/api/posts/{post_id}/leave/').status_code == 404  # l'hôte ne se retire pas
    res = api.as_user(lea).post(f'/api/posts/{post_id}/leave/')

    assert res.status_code == 204
    assert MealInvitation.objects.get(invitee=lea).status == 'declined'
    assert not PostRepost.objects.exists()
    assert Notification.objects.get(user=lea, notification_type='post_guest').is_read
    assert api.as_user(tom).get(f'/api/posts/{post_id}/').data['guests'] == []
    assert api.as_user(lea).post(f'/api/posts/{post_id}/leave/').status_code == 404


def test_undo_removes_guests_added_by_the_log(
    api, tom, lea, make_meal_plan, make_recipe, django_capture_on_commit_callbacks
):
    mp = make_meal_plan(tom, day=date(2026, 11, 8), meal_time='dinner', recipes=[make_recipe(tom)])
    declined = MealInvitation.objects.create(inviter=tom, invitee=lea, meal_plan=mp, status='declined')
    resp = _cook(api, tom, django_capture_on_commit_callbacks, meal_plan_id=mp.id, guest_ids=[lea.id])
    declined.refresh_from_db()
    assert declined.status == 'accepted' and declined.source == 'direct'

    assert api.as_user(tom).delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true').status_code == 204

    declined.refresh_from_db()
    assert declined.status == 'declined' and declined.source == 'invite'
    assert not Notification.objects.filter(notification_type='post_guest').exists()


def test_guest_names_link_only_for_viewers_who_can_see_the_profile(
    api, tom, lea, make_user, follow, make_recipe, django_capture_on_commit_callbacks
):
    friend_of_tom = make_user('sam')
    follow(friend_of_tom, tom)  # voit le post de Tom, ne suit pas Léa
    resp = _cook(api, tom, django_capture_on_commit_callbacks, recipe_id=make_recipe(tom).id, guest_ids=[lea.id])

    feed = api.as_user(friend_of_tom).get('/api/posts/', {'is_published': 'true'})
    post = next(p for p in feed.data['results'] if p['id'] == resp.data['id'])
    assert post['guests'][0]['first_name'] == 'Léa' and post['guests'][0]['can_view_profile'] is False

    follow(friend_of_tom, lea)
    detail = api.as_user(friend_of_tom).get(f'/api/posts/{resp.data["id"]}/')
    assert detail.data['guests'][0]['can_view_profile'] is True


def test_guest_candidates_order_mutual_household_friend_table(api, tom, lea, pauline, make_user, follow):
    fan = make_user('fan', first_name='Arthur')
    follow(fan, tom)
    mate = make_user('mate', first_name='Inès')
    table = Table.objects.create(name='Les colocs')
    TableMember.objects.create(table=table, user=tom, role='owner', status='accepted')
    TableMember.objects.create(table=table, user=mate, status='accepted')
    stranger = make_user('stranger')

    res = api.as_user(tom).get(CANDIDATES_URL)

    assert res.status_code == 200
    rows = [(r['first_name'], r['kind'], r['is_household']) for r in res.data['results']]
    assert rows == [
        ('Léa', 'mutual', False),
        ('Pauline', 'mutual', True),
        ('Arthur', 'friend', False),
        ('Inès', 'table', False),
    ]
    assert stranger.id not in {r['id'] for r in res.data['results']}
