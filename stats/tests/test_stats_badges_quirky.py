"""
Badges loufoques (spec badges-quirky) : un évaluateur représentatif par famille, secrets, rareté, carte à
partager, notifications (rare / secret, pas de spam au rattrapage) et nombre de requêtes borné.
"""

import re
from datetime import UTC, date, datetime, timedelta

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import HouseholdMember, Notification
from notifications.models import NotificationPreference
from recipes.models import MealInvitation, Post, PostPhoto
from stats.badge_colors import colour_of
from stats.models import BadgeRarity, UserBadge
from stats.services import badge_rarity
from stats.services.badges import BADGES, BADGES_BY_CODE, award_badges_safely, evaluate_badges
from stats.services.badges_quirky import QUIRKY_BADGES, easter

pytestmark = pytest.mark.django_db


def _codes(badges):
    return {b['code'] for b in badges}


@pytest.fixture
def cook(user, make_recipe, make_meal_plan):
    """`cook(day, title=…, ingredients=…, **meal)` : un repas cuisiné (une recette) le jour `day`."""
    recipes = {}

    def _cook(day, title='Gratin', ingredients=('pommes de terre', 'creme'), **extra):
        key = (title, tuple(ingredients), extra.get('cuisine'))
        if key not in recipes:
            recipes[key] = make_recipe(user, title, ingredients=ingredients, cuisine=extra.pop('cuisine', None))
        extra.pop('cuisine', None)
        return make_meal_plan(user, day=day, recipes=[recipes[key]], confirmed=True, **extra)

    return _cook


# ---------------------------------------------------------------------------
# Registre et copie
# ---------------------------------------------------------------------------
BANNED = re.compile(
    r'\bbande\b|strava|!|\bvous\b|perdre|longtemps|manqu|oublié|nutrition|calorie|santé|healthy', re.IGNORECASE
)


def test_registry_is_big_unique_and_well_written():
    codes = [b.code for b in BADGES]
    assert len(codes) == len(set(codes)) and len(QUIRKY_BADGES) >= 50
    secrets = [b for b in QUIRKY_BADGES if b.secret]
    assert len(secrets) >= 15
    enigmes = [b for b in secrets if b.mystery == 'enigme']
    assert all(b.hint for b in enigmes)
    introuvables = [b for b in secrets if b.mystery == 'introuvable']
    assert len(introuvables) == 6
    for b in QUIRKY_BADGES:
        assert b.family and b.phrase and b.rarity_guess in badge_rarity.TIERS
        for text in (b.title, b.description, b.phrase, b.hint):
            assert not BANNED.search(text), (b.code, text)


def test_colour_map_covers_rainbow_and_unknowns():
    assert [colour_of(n) for n in ('Tomates cerises', 'carotte', 'maïs', 'courgette', 'aubergine')] == [
        'rouge',
        'orange',
        'jaune',
        'vert',
        'violet',
    ]
    assert colour_of('poivron jaune') == 'jaune' and colour_of('poivron') == 'rouge'
    assert colour_of('patate douce') == 'orange' and colour_of('pomme de terre') == 'jaune'
    assert colour_of('oignon') == 'blanc' and colour_of('tofu fumé') is None


# ---------------------------------------------------------------------------
# Une famille, un évaluateur représentatif
# ---------------------------------------------------------------------------
def test_same_dish_repeated_and_leftovers(user, cook):
    start = date(2026, 3, 2)
    for i in range(10):
        cook(start + timedelta(days=i * 3))
    cook(date(2026, 6, 1), 'Dahl', ['lentilles corail'])
    cook(date(2026, 6, 2), 'Dahl', ['lentilles corail'])

    new = {b['code']: b for b in evaluate_badges(user, notify=False)}

    assert {'repeat_5', 'repeat_10'} <= set(new) and 'repeat_25' not in new
    assert new['repeat_5']['context'] == {'achieved_on': '2026-03-14', 'value': 10, 'dish': 'Gratin'}
    assert 'repeat_3_days' not in new  # 2 jours d'affilée seulement

    cook(date(2026, 6, 3), 'Dahl', ['lentilles corail'])
    assert _codes(evaluate_badges(user, notify=False)) >= {'repeat_3_days'}


def test_dish_anniversary_is_a_secret_within_one_day(user, cook):
    cook(date(2025, 4, 10), 'Blanquette', ['veau'])
    cook(date(2026, 4, 9), 'Blanquette', ['veau'])  # un an moins un jour
    cook(date(2026, 4, 9), 'Pâtes', ['pates'], meal_time='dinner')

    new = {b['code']: b for b in evaluate_badges(user, notify=False)}

    assert new['dish_anniversary']['secret'] is True
    assert new['dish_anniversary']['celebration'] == 'secret'
    assert new['dish_anniversary']['context']['dish'] == 'Blanquette'
    # Titre court : la légende sur 2 lignes ne finit plus en « Joyeux anniversaire, . ».
    assert new['dish_anniversary']['title'] == 'L’anniversaire du plat'


def test_calendar_chandeleur_and_summer_raclette(user, cook):
    assert easter(2026) == date(2026, 4, 5) and easter(2027) == date(2027, 3, 28)
    cook(date(2026, 2, 2), 'Crêpes au sucre', ['farine', 'oeufs', 'lait'])
    cook(date(2026, 2, 3), 'Raclette', ['raclette', 'pommes de terre'])  # hiver : normal
    cook(date(2026, 3, 1), 'Fondue de poireaux', ['poireaux'])  # pas une fondue au fromage

    first = _codes(evaluate_badges(user, notify=False))
    assert 'chandeleur' in first and 'raclette_ete' not in first

    cook(date(2026, 7, 14), 'Tartiflette', ['reblochon', 'pommes de terre', 'lardons'])
    assert 'raclette_ete' in _codes(evaluate_badges(user, notify=False))


def test_midnight_uses_the_user_timezone(user, make_user, cook):
    def log_at(owner_meal, utc):
        post = Post.objects.create(user=owner_meal.user, meal_plan=owner_meal, label='Pâtes')
        Post.objects.filter(pk=post.pk).update(created_at=utc)

    # 04:30 UTC = 00:30 à New York (heure d’été depuis le 8 mars), 05:30 à Paris.
    meal = cook(date(2026, 3, 9), 'Pâtes', ['pates'])
    log_at(meal, datetime(2026, 3, 10, 4, 30, tzinfo=UTC))
    assert 'minuit' not in _codes(evaluate_badges(user, notify=False))  # Paris : 5 h 30

    NotificationPreference.objects.create(user=user, timezone='America/New_York')
    assert 'minuit' in _codes(evaluate_badges(user, notify=False))


def test_rainbow_week_and_ingredient_count_extremes(user, cook):
    monday = date(2026, 5, 4)
    cook(monday, 'Salade', ['tomate', 'courgette'])
    cook(monday + timedelta(days=2), 'Purée', ['carotte', 'maïs', 'aubergine', 'sel'])
    cook(monday + timedelta(days=7), 'Ratatouille', [f'ingrédient {i}' for i in range(20)])

    new = _codes(evaluate_badges(user, notify=False))

    assert {'arc_en_ciel', 'minimaliste', 'marathon'} <= new
    assert 'tout_orange' not in new


def test_world_tour_in_seven_days(user, cook):
    start = date(2026, 9, 1)
    for i, cuisine in enumerate(('italienne', 'japonaise', 'mexicaine', 'indienne')):
        cook(start + timedelta(days=i), f'Plat {cuisine}', ['riz'], cuisine=cuisine)
    cook(start + timedelta(days=8), 'Tajine', ['semoule'], cuisine='marocaine')  # trop tard
    assert 'tour_monde_semaine' not in _codes(evaluate_badges(user, notify=False))

    cook(start + timedelta(days=5), 'Pho', ['nouilles de riz'], cuisine='vietnamienne')
    assert 'tour_monde_semaine' in _codes(evaluate_badges(user, notify=False))


def test_cantine_never_counts_the_household(user, make_user, cook):
    meal = cook(date(2026, 10, 3), 'Paella', ['riz'], guest_count=8)
    family = [make_user(f'fam{i}') for i in range(3)]
    for member in family:
        HouseholdMember.objects.create(owner=user, user=member, status=HouseholdMember.STATUS_ACCEPTED)
        MealInvitation.objects.create(inviter=user, invitee=member, meal_plan=meal, status='accepted')
    assert 'cantine' not in _codes(evaluate_badges(user, notify=False))  # 1 + 8 = 9 (foyer exclu)

    for i in range(3):
        MealInvitation.objects.create(inviter=user, invitee=make_user(f'ami{i}'), meal_plan=meal, status='accepted')
    assert 'cantine' in _codes(evaluate_badges(user, notify=False))  # 1 + 3 + 8 = 12


def test_influencer_counts_my_turn_from_others(user, make_user, cook, make_meal_plan):
    mine = Post.objects.create(user=user, meal_plan=cook(date(2026, 8, 1)), label='Gratin', is_published=True)
    friends = [make_user(f'fan{i}') for i in range(5)]
    for i, friend in enumerate(friends[:4]):
        meal = make_meal_plan(friend, day=date(2026, 8, 2 + i), confirmed=True)
        Post.objects.create(user=friend, meal_plan=meal, label='Gratin', inspired_by_post=mine)
    Post.objects.create(user=user, meal_plan=cook(date(2026, 8, 9)), inspired_by_post=mine)  # soi : non
    assert 'influenceur' not in _codes(evaluate_badges(user, notify=False))

    meal = make_meal_plan(friends[4], day=date(2026, 8, 10), confirmed=True)
    Post.objects.create(user=friends[4], meal_plan=meal, label='Gratin', inspired_by_post=mine)
    new = {b['code']: b for b in evaluate_badges(user, notify=False)}
    assert new['influenceur']['context']['value'] == 5


# ---------------------------------------------------------------------------
# Secrets, rareté, carte à partager
# ---------------------------------------------------------------------------
def test_secret_badges_are_hidden_until_earned(api, user, cook):
    cook(date(2026, 1, 15))
    client = api.as_user(user)

    overview = client.get('/api/badges/me/?all=1').data
    codes = [b['code'] for b in overview['next']]
    assert 'raclette_ete' not in codes and 'repeat_5' in codes
    secret = next(b for b in overview['next'] if b['secret'])
    assert secret['title'] == '???' and secret['hint'] and secret['code'].startswith('secret_')
    assert secret['phrase'] == '' and secret['progress']['ratio'] == 0
    assert 'Fromage' not in str(secret)  # rien qui révèle la condition
    assert not any(b['secret'] for b in client.get('/api/badges/me/').data['next'])  # aperçu : jamais
    assert {f['code'] for f in overview['families']} >= {'meme_recette', 'calendrier', 'defis', 'a_plusieurs'}

    cook(date(2026, 7, 2), 'Raclette', ['raclette'])
    earned = {b['code']: b for b in client.get('/api/badges/me/?all=1').data['earned']}
    assert earned['raclette_ete']['secret'] is True and earned['raclette_ete']['title'] == 'Raclette en juillet'


def test_rarity_is_computed_over_active_cooks(settings, make_user, make_meal_plan):
    settings.BADGE_RARITY_MIN_ACTIVE_USERS = 10
    today = date.today()
    cooks = [make_user(f'c{i}') for i in range(12)]
    for i, u in enumerate(cooks):
        make_meal_plan(u, day=today - timedelta(days=200 if i == 11 else 3), confirmed=True)  # le 12e : inactif
    for u in cooks[:6]:
        UserBadge.objects.create(user=u, code='first_cooked')
    UserBadge.objects.create(user=cooks[0], code='repeat_5')
    UserBadge.objects.create(user=cooks[11], code='repeat_5')  # inactif : ne compte pas

    rows = badge_rarity.compute_badge_rarity()

    assert (rows['first_cooked'].holders, rows['first_cooked'].active_users) == (6, 11)
    assert rows['first_cooked'].tier == 'commun' and rows['repeat_5'].tier == 'rare'  # 54,5 % et 9,1 %
    assert rows['alphabet_26'].percent == 0 and rows['alphabet_26'].tier == 'legendaire'
    assert [badge_rarity.tier_for(p) for p in (0.5, 3, 12, 40)] == ['legendaire', 'tres_rare', 'rare', 'commun']
    fields = badge_rarity.rarity_fields(BADGES_BY_CODE['repeat_5'], badge_rarity.rarity_map())
    assert fields['rarity_percent'] == 9.1 and fields['rarity_text'] == 'Seulement 9 % des cuisiniers Tchikook l’ont'

    settings.BADGE_RARITY_MIN_ACTIVE_USERS = 50  # trop peu de cuisiniers : estimation du registre
    assert badge_rarity.compute_badge_rarity()['first_cooked'].tier == BADGES_BY_CODE['first_cooked'].rarity_guess
    assert BadgeRarity.objects.count() == len(BADGES)


def test_share_payload_of_an_earned_badge(api, user, cook):
    user.first_name = 'Alice'
    user.save(update_fields=['first_name'])
    for i in range(5):
        cook(date(2026, 3, 1) + timedelta(days=i * 2))
    client = api.as_user(user)
    client.get('/api/badges/me/')

    resp = client.get('/api/badges/repeat_5/share/')

    assert resp.status_code == 200, resp.data
    data = resp.data
    assert data['badge']['title'] == 'La fétiche' and data['badge']['phrase'] == 'Tu sais ce que tu aimes.'
    assert data['achieved_on'] == '2026-03-09' and data['user'] == {'id': user.id, 'display_name': 'Alice'}
    assert data['invite']['url'].endswith('/i/' + data['invite']['code'])
    assert data['invite']['url'] in data['share_text'] and '« La fétiche »' in data['share_text']
    assert {'rarity_percent', 'rarity_tier', 'rarity_text'} <= set(data['badge'])
    assert client.get('/api/badges/repeat_10/share/').status_code == 404  # pas obtenu
    assert client.get('/api/badges/nope/share/').status_code == 404


# ---------------------------------------------------------------------------
# Notifications et rattrapage
# ---------------------------------------------------------------------------
def test_backfill_awards_history_silently(user, cook, celery_delay):
    for i in range(6):
        cook(date(2025, 2, 2) + timedelta(days=i), 'Crêpes', ['farine'])

    call_command('backfill_badges')

    codes = set(UserBadge.objects.filter(user=user).values_list('code', flat=True))
    assert {'repeat_5', 'repeat_3_days', 'chandeleur', 'first_cooked'} <= codes
    assert not Notification.objects.filter(user=user).exists()
    assert not [c for c in celery_delay.calls if c[0].endswith('send_new_badge_push')]

    # Un vieux badge attribué au fil de l'eau (avant le rattrapage) : ni célébration, ni notification.
    UserBadge.objects.filter(user=user, code='chandeleur').delete()
    cook(date.today(), 'Salade', ['roquette'])
    assert 'chandeleur' not in _codes(award_badges_safely(user))
    assert not [c for c in celery_delay.calls if 'chandeleur' in str(c[1])]


def test_rare_or_secret_badge_sends_badge_rare(user):
    from stats.tasks import send_new_badge_push

    send_new_badge_push.run(user.id, ['first_cooked', 'raclette_ete'], False)
    send_new_badge_push.run(user.id, ['first_cooked'], False)

    titles = list(Notification.objects.filter(user=user).order_by('id').values_list('title', flat=True))
    assert titles == ['Badge secret débloqué : Raclette en juillet 🫕', 'Nouveau badge : Premier plat 👩‍🍳']


# ---------------------------------------------------------------------------
# Coût de l'évaluation : nombre de requêtes constant
# ---------------------------------------------------------------------------
def _history(owner, n, make_recipe, make_meal_plan):
    recipes = [
        make_recipe(owner, f'Plat {i}', ingredients=['tomate', 'ail', 'beurre', f'épice {i}'], cuisine='italienne')
        for i in range(8)
    ]
    start = date(2025, 1, 1)
    for i in range(n):
        meal = make_meal_plan(owner, day=start + timedelta(days=i), recipes=[recipes[i % 8]], confirmed=True)
        if i % 3 == 0:
            post = Post.objects.create(user=owner, meal_plan=meal, label='', is_published=True)
            PostPhoto.objects.create(post=post, photo_type='after_cooking', image_path=f'p/{i}.jpg')


def test_evaluation_query_count_is_constant(make_user, make_recipe, make_meal_plan):
    small, big = make_user('small'), make_user('big')
    _history(small, 20, make_recipe, make_meal_plan)
    _history(big, 200, make_recipe, make_meal_plan)

    badge_rarity.rarity_map()  # rareté en cache (1 requête par heure, partagée par tous)
    counts = []
    for owner in (small, big):
        with CaptureQueriesContext(connection) as ctx:
            evaluate_badges(owner, notify=False)
        counts.append(len(ctx.captured_queries))

    assert counts[0] == counts[1], counts
    assert counts[1] <= 25, counts


def test_v3_progressive_hints_stages(user):
    from stats.services.badges import BADGES_BY_CODE, secret_placeholder

    badge = BADGES_BY_CODE['rituel']
    assert len(badge.hints) == 3

    # Stade 1 (0 à 9 plats)
    s1 = secret_placeholder(badge, cooked_dishes=3)
    assert s1['hint_stage'] == 1
    assert s1['hint'] == badge.hints[0]
    assert s1['next_hint_in'] == 7

    # Stade 2 (10 à 24 plats)
    s2 = secret_placeholder(badge, cooked_dishes=15)
    assert s2['hint_stage'] == 2
    assert s2['hint'] == badge.hints[1]
    assert s2['next_hint_in'] == 10

    # Stade 3 (25 plats ou plus)
    s3 = secret_placeholder(badge, cooked_dishes=28)
    assert s3['hint_stage'] == 3
    assert s3['hint'] == badge.hints[2]
    assert s3['next_hint_in'] is None


def test_v3_new_badges_evaluation(user, cook, make_recipe):
    # 1. Grand retour (écart >= 180 j sur la même recette)
    cook(date(2025, 1, 1), 'Tarte aux pommes', ['pomme'])
    cook(date(2025, 7, 10), 'Tarte aux pommes', ['pomme'])

    # 2. Team patate (patates)
    cook(date(2025, 7, 11), 'Frites maison', ['pomme de terre'])

    # 3. Herboriste (5 herbes fraîches hors ingrédients de base)
    cook(date(2025, 7, 12), 'Tajine parfumé', ['basilic', 'persil', 'coriandre', 'menthe', 'ciboulette'])

    # 4. Pyramide (6 groupes différents)
    # viandes, legumes, fruits, herbes_epices, cereales_feculents, laitiers
    cook(date(2025, 7, 13), 'Grand bol équilibré', ['poulet', 'carotte', 'pomme', 'curry', 'riz', 'parmesan'])

    new = {b['code']: b for b in evaluate_badges(user, notify=False)}
    assert 'grand_retour' in new
    assert 'herboriste' in new
    assert 'pyramide' in new


def test_v3_lot2_badges_and_introuvables_mechanics(user, make_user, cook, make_recipe, make_meal_plan):
    from django.utils import timezone

    from recipes.models import MealInvitation, Post, PostComment, PostPhoto, Timer
    from stats.services.badges import BADGES_BY_CODE, badges_overview, share_text
    from tables.models import Table, TableMember

    # 1. Pique-assiette (5 invitations acceptées)
    host = make_user('host')
    for i in range(5):
        m = make_meal_plan(host, day=date(2025, 8, i + 1), confirmed=True)
        MealInvitation.objects.create(inviter=host, invitee=user, meal_plan=m, status='accepted')

    # 2. Débat culinaire (10 commentaires)
    post_meal = cook(date(2025, 9, 1), 'Chili con carne', ['haricots rouges'])
    post = Post.objects.create(user=user, meal_plan=post_meal, label='Mon super chili')
    commenters = [make_user(f'commenter{i}') for i in range(3)]  # un débat : au moins 3 personnes
    for i in range(10):
        PostComment.objects.create(post=post, user=commenters[i % 3], text=f'Super {i}')

    # 3. Making-of (photo pendant la cuisine + au repas)
    PostPhoto.objects.create(post=post, photo_type='during_cooking')
    PostPhoto.objects.create(post=post, photo_type='at_meal_time')

    # 4. Chef d'orchestre (3 minuteurs simultanés)
    now = timezone.now()
    timer_recipe = make_recipe(user, 'Chili rapide', ingredients=['haricots'])
    step = timer_recipe.steps.create(order=1, instruction='Cuire')
    for _i in range(3):
        Timer.objects.create(
            user=user,
            step=step,
            duration_minutes=15,
            remaining_seconds=900,
            expires_at=now + timezone.timedelta(minutes=15),
        )

    # 5. Télépathie (même recette le même jour qu'un membre de tablée)
    member = make_user('tablemate')
    table = Table.objects.create(name='Les gourmets', created_by=user)
    TableMember.objects.create(table=table, user=user, status=TableMember.STATUS_ACCEPTED)
    TableMember.objects.create(table=table, user=member, status=TableMember.STATUS_ACCEPTED)

    alice_meal = cook(date(2025, 9, 10), 'Risotto', ['riz'])
    alice_recipe = alice_meal.meal_plan_recipe_batches.first().recipe_batch.recipe
    make_meal_plan(member, day=date(2025, 9, 10), recipes=[alice_recipe], confirmed=True)

    new = {b['code']: b for b in evaluate_badges(user, notify=False)}
    assert 'pique_assiette' in new
    assert 'debat_culinaire' in new
    assert 'making_of' in new
    assert 'chef_orchestre' in new
    assert 'telepathie' in new
    assert new['telepathie']['celebration'] == 'introuvable'

    # Vérification partage introuvable : ne divulgue pas la condition
    tele_share = share_text(BADGES_BY_CODE['telepathie'], 'https://tchikook.fr/i/abc')
    assert 'J’ai trouvé un badge introuvable sur Tchikook' in tele_share
    assert 'Sauras-tu comment' in tele_share
    assert 'même plat cuisiné' not in tele_share

    # Vérification masquage avant déblocage dans badges_overview
    other_user = make_user('other_user')
    overview = badges_overview(other_user, next_limit=None)
    next_codes = [b['code'] for b in overview['next']]
    # Aucun introuvable dans next avant déblocage (0 mention)
    for code in ('telepathie', 'bouche_a_oreille', 'poisson_avril', 'bissextile', 'tchikiversaire', 'midi_pile'):
        assert code not in next_codes


# ---------------------------------------------------------------------------
# fx02 : règles corrigées (relecture du 2026-11-10)
# ---------------------------------------------------------------------------
def _value(user, code):
    from stats.services.badges import _measure, lifetime_stats

    return _measure(BADGES_BY_CODE[code], lifetime_stats(user))[:2]


def test_pique_assiette_counts_past_cooked_meals_outside_the_household(user, make_user, make_meal_plan):
    today = date.today()
    host, partner = make_user('host'), make_user('partner')
    HouseholdMember.objects.create(owner=user, user=partner, status=HouseholdMember.STATUS_ACCEPTED)

    def invite(owner, day, confirmed=True):
        meal = make_meal_plan(owner, day=day, confirmed=confirmed)
        MealInvitation.objects.create(inviter=owner, invitee=user, meal_plan=meal, status='accepted')

    for i in range(3):
        invite(host, today - timedelta(days=10 + i))
    invite(host, today + timedelta(days=3))  # repas à venir
    invite(host, today - timedelta(days=2), confirmed=False)  # jamais cuisiné
    invite(partner, today - timedelta(days=4))  # le foyer n'invite pas
    invite(partner, today - timedelta(days=5))

    assert _value(user, 'pique_assiette') == (3, None)

    invite(host, today - timedelta(days=1))
    invite(host, today)
    assert _value(user, 'pique_assiette') == (5, today)


def test_chasseur_de_recettes_counts_imported_recipes(user, cook):
    from recipes.models import Recipe

    for i in range(10):
        meal = cook(date(2026, 2, 1) + timedelta(days=i), f'Import {i}', [f'ingrédient {i}'])
        recipe = meal.meal_plan_recipe_batches.first().recipe_batch.recipe
        if i < 8:
            Recipe.objects.filter(id=recipe.id).update(source_type='imported')
        elif i == 8:
            Recipe.objects.filter(id=recipe.id).update(import_source_url='https://example.com/r')
    assert _value(user, 'chasseur_recettes') == (9, None)  # la 10e est créée à la main

    meal = cook(date(2026, 2, 20), 'Import 10', ['ingrédient 10'])
    Recipe.objects.filter(id=meal.meal_plan_recipe_batches.first().recipe_batch.recipe_id).update(
        source_type='imported'
    )
    assert _value(user, 'chasseur_recettes') == (10, date(2026, 2, 20))


def test_pilote_auto_is_dated_on_the_20th_completed_recipe(user, make_recipe, make_meal_plan):
    from recipes.models import CookingProgress

    meal = make_meal_plan(user, day=date(2025, 3, 1), recipes=[make_recipe(user, 'Soupe')], confirmed=True)
    batch = meal.meal_plan_recipe_batches.first().recipe_batch
    start = datetime(2025, 3, 1, 12, tzinfo=UTC)
    for i in range(20):
        CookingProgress.objects.create(
            user=user,
            recipe_batch=batch,
            status='completed',
            completed_at=start + timedelta(days=i),
            total_time_minutes=None if i % 2 else 30,  # mesurée ou pas : suivie jusqu'au bout quand même
        )
    CookingProgress.objects.create(user=user, recipe_batch=batch, status='abandoned')

    new = {b['code']: b for b in evaluate_badges(user, notify=False)}
    # Daté de la 20e (pas d'aujourd'hui) : rattrapage silencieux, aucune célébration de masse au déploiement.
    assert new['pilote_auto']['context']['achieved_on'] == '2025-03-20'
    assert 'pilote_auto' not in _codes(award_badges_safely(user))


def test_alliteration_ignores_articles_and_is_never_in_the_future(user, cook):
    monday = date(2026, 3, 2)
    for i, title in enumerate(['La paella', 'Le poulet rôti', 'Pâtes au pesto', 'Une pizza', 'Purée maison']):
        cook(monday + timedelta(days=i // 2), title, [f'ingrédient {i}'], meal_time=('lunch', 'dinner')[i % 2])

    new = {b['code']: b for b in evaluate_badges(user, notify=False)}
    assert new['alliteration']['context']['achieved_on'] == '2026-03-04'  # jour du 5e plat, pas le dimanche


def test_rien_ne_se_perd_compares_every_dish_of_the_day_before(user, cook):
    leftovers = ['carotte', 'poireau', 'navet', 'céleri']
    cook(date(2026, 4, 1), 'Pot-au-feu', [*leftovers, 'boeuf'])
    cook(date(2026, 4, 1), 'Tarte au citron', ['citron', 'farine', 'oeuf'], meal_time='dinner')
    assert 'rien_ne_se_perd' not in _codes(evaluate_badges(user, notify=False))

    cook(date(2026, 4, 2), 'Velouté de légumes', [*leftovers, 'crème'])
    new = {b['code']: b for b in evaluate_badges(user, notify=False)}
    assert new['rien_ne_se_perd']['context']['achieved_on'] == '2026-04-02'


def test_debat_culinaire_needs_several_people(user, make_user, cook):
    from recipes.models import PostComment

    post = Post.objects.create(user=user, meal_plan=cook(date(2026, 5, 1)), label='Chili', is_published=True)
    chatty, other = make_user('chatty'), make_user('other')
    for i in range(12):
        PostComment.objects.create(post=post, user=chatty, text=f'Miam {i}')
    PostComment.objects.create(post=post, user=other, text='Oh')
    PostComment.objects.create(post=post, user=user, text='Merci')  # ses propres mots ne comptent pas
    assert 'debat_culinaire' not in _codes(evaluate_badges(user, notify=False))  # 2 personnes

    PostComment.objects.create(post=post, user=make_user('third'), text='Recette ?')
    assert 'debat_culinaire' in _codes(evaluate_badges(user, notify=False))


def test_enigma_hints_count_dishes_since_the_badge_arrived(user, cook, monkeypatch):
    from stats.services import badge_types
    from stats.services.badges import badges_overview, hint_stage_of

    since = date(2026, 6, 1)
    monkeypatch.setattr(badge_types, 'ENIGMES_V3_SINCE', since)
    for i in range(30):  # gros historique AVANT l'arrivée des énigmes
        cook(since - timedelta(days=40 - i), f'Plat {i}', [f'ingrédient {i}'])
    for i in range(4):
        cook(since + timedelta(days=i), f'Nouveau {i}', [f'nouvel ingrédient {i}'])

    secrets = [b for b in badges_overview(user, next_limit=None)['next'] if b['secret']]
    rituel_hint = BADGES_BY_CODE['rituel'].hints[0]
    rituel = next(b for b in secrets if b['hint'] == rituel_hint)
    assert (rituel['hint_stage'], rituel['next_hint_in']) == (1, 6)
    assert all(b['icon'] == 'mystery' for b in secrets)  # jamais l'icône réelle d'une énigme
    assert BADGES_BY_CODE['rituel'].icon not in str(secrets)
    assert BADGES_BY_CODE['rituel'].hints[2] not in str(secrets)  # aucun indice futur dans le payload

    assert hint_stage_of(3, 10) == (2, 15) and hint_stage_of(3, 24) == (2, 1) and hint_stage_of(3, 25) == (3, None)
    assert hint_stage_of(2, 30) == (2, None)  # plus d'indice : jamais « dans 0 plat »
    assert hint_stage_of(1, 0) == (1, None)


def test_friend_profile_wording_and_share_card_of_a_mystery(api, user, make_user):
    from stats.services.badges import badge_share_payload, earned_badges_public

    friend = make_user('friend')
    for code in ('tablee_complete', 'host_5', 'telepathie'):
        UserBadge.objects.create(user=user, code=code, context={'achieved_on': '2026-10-01'})

    seen = {b['code']: b['description'] for b in earned_badges_public(user, viewer=friend)}
    assert seen['tablee_complete'] == 'Tout le monde a cuisiné dans sa tablée la même semaine.'
    assert seen['host_5'] == 'A cuisiné pour 5 personnes.'
    assert seen['telepathie'] == 'Demande-lui comment !'

    share = badge_share_payload(user, 'telepathie')
    assert share['badge']['description'] == '' and share['badge']['phrase']
    assert 'tablée' not in share['share_text']
