"""Rétention : digest des posts des amis (≤ 1/jour, seulement si nouveaux posts) et récap hebdo."""

from datetime import date, timedelta

from notifications.retention import friends_digest_for_user, send_friends_digests, weekly_recap_for_user


def test_friends_digest_only_with_new_posts_and_once_per_day(user, make_user, follow, device, expo, paris, make_post):
    device(user)
    lea, stranger = make_user('lea'), make_user('stranger')
    follow(user, lea)
    evening = paris(18)

    # Rien de neuf (post d'un inconnu, post privé d'un ami) → pas de digest.
    make_post(stranger, at=evening - timedelta(hours=2))
    make_post(lea, 'Privé', at=evening - timedelta(hours=2), published=False)
    assert friends_digest_for_user(user, now=evening).status == 'no_posts'

    make_post(lea, 'Quiche', at=evening - timedelta(hours=1))
    assert friends_digest_for_user(user, now=evening).status == 'sent'
    (message,) = expo.messages
    assert message['title'] == 'Du nouveau chez tes amis'
    assert message['body'] == 'lea a partagé «\u00a0Quiche\u00a0».'
    assert message['data']['type'] == 'friends_digest' and message['data']['url'] == 'tchikook://feed/friends'

    # Même jour, nouveau post : toujours au plus un digest par jour.
    make_post(lea, 'Tarte', at=evening + timedelta(minutes=30))
    assert friends_digest_for_user(user, now=evening + timedelta(hours=1)).status == 'duplicate'
    # Lendemain : seuls les posts publiés depuis le dernier digest comptent.
    assert friends_digest_for_user(user, now=evening + timedelta(days=1)).status == 'sent'
    assert expo.messages[-1]['body'] == 'lea a partagé «\u00a0Tarte\u00a0».'
    assert friends_digest_for_user(user, now=evening + timedelta(days=2)).status == 'no_posts'
    assert len(expo.messages) == 2


def test_friends_digest_runs_at_local_evening_only(user, make_user, follow, device, expo, paris, make_post):
    device(user)
    lea = make_user('lea')
    follow(user, lea)
    make_post(lea, at=paris(15))

    assert send_friends_digests(now=paris(16)) == {}
    assert send_friends_digests(now=paris(18)) == {'sent': 1}
    # Beat relancé dans la même heure avec un post de plus : toujours un seul digest ce jour-là.
    make_post(lea, 'Tarte', at=paris(18) + timedelta(seconds=30))
    assert send_friends_digests(now=paris(18, 1)) == {'duplicate': 1}
    assert len(expo.messages) == 1


def test_weekly_recap(user, make_user, follow, device, expo, paris, make_recipe, make_meal_plan, make_post):
    device(user)
    sunday = paris(19, day=date(2026, 11, 8))  # dimanche
    monday = date(2026, 11, 2)
    quiche, dal = make_recipe(user, 'Quiche'), make_recipe(user, 'Dal')
    make_meal_plan(user, day=date(2026, 10, 1), recipes=[dal], confirmed=True)  # déjà cuisiné avant
    make_meal_plan(user, day=monday, recipes=[quiche], confirmed=True)
    make_meal_plan(user, day=monday + timedelta(days=2), recipes=[dal], confirmed=True)
    make_meal_plan(user, day=monday + timedelta(days=3), meal_time='dinner', recipes=[quiche])  # planifié seulement

    result = weekly_recap_for_user(user, now=sunday)
    assert result.status == 'sent'
    (message,) = expo.messages
    assert message['title'] == 'Ta semaine à table'
    assert message['body'] == 'Cette semaine tu as cuisiné 2 plats, dont 1 nouveau.'
    assert message['data']['week_start'] == '2026-11-02' and message['data']['url'] == 'tchikook://stats/week'
    assert weekly_recap_for_user(user, now=sunday + timedelta(minutes=5)).status == 'duplicate'


def test_weekly_recap_never_sent_when_nothing_happened(user, make_user, follow, device, expo, paris, make_post):
    device(user)
    sunday = paris(19, day=date(2026, 11, 8))
    assert weekly_recap_for_user(user, now=sunday).status == 'nothing_to_say'

    lea = make_user('lea')
    follow(user, lea)
    make_post(lea, at=sunday - timedelta(days=1))
    assert weekly_recap_for_user(user, now=sunday).status == 'sent'
    assert expo.messages[0]['body'].startswith('Tes amis ont partagé 1 plat cette semaine.')
