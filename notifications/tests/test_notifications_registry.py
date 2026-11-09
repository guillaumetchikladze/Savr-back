"""Registre : rendu des modèles (ton, pluriels, agrégation) et forme commune du payload `data`."""

import re
from datetime import date

import pytest

from notifications.registry import CATEGORIES, REGISTRY, get_type

EMOJI = re.compile('[\U0001f300-\U0001faff☀-➿]')
FORBIDDEN = ('perdre', 'série', 'longtemps', 'manqu', 'oublié')

SAMPLE_CONTEXT = {
    'actor': 'Léa',
    'actors': ['Léa', 'Tom'],
    'dish': '« Quiche »',
    'dish_title': 'Quiche',
    'comment': 'Trop beau',
    'meal_time': 'dinner',
    'date': date(2026, 11, 14),
    'list_name': '« Courses »',
    'step_order': 3,
    'recipe': 'Quiche',
    'badge': 'Aubergine',
    'emoji': '🍆',
    'description': 'Tu as cuisiné l’aubergine.',
    'post_count': 2,
    'author_count': 2,
    'cooked': 4,
    'new_dishes': 2,
}


@pytest.mark.parametrize('key', sorted(REGISTRY))
def test_every_type_renders_in_house_style(key):
    spec = REGISTRY[key]
    assert spec.category in CATEGORIES
    title, body = spec.render(SAMPLE_CONTEXT)
    assert title and body
    text = f'{title} {body}'
    assert '!' not in text and '{' not in text
    assert len(EMOJI.findall(text)) <= 1
    assert not any(word in text.lower() for word in FORBIDDEN)
    assert 'vous' not in text.lower().split()  # tutoiement


def test_payload_shape_is_common_to_all_types():
    spec = get_type('post_miam')
    data = spec.build_data({'post_id': 12, 'user_id': 3}, notification_id=99)
    assert data['type'] == 'post_miam' and data['category'] == 'social'
    assert data['notification_id'] == 99 and data['url'] == 'tchikook://post/12'
    assert data['post_id'] == 12
    # Compat apps déjà publiées
    assert (data['source'], data['kind']) == ('social', 'post_miam')

    timer = get_type('timer_almost_finished').build_data({'timer_id': 7, 'recipe_id': 5}, notification_id=1)
    assert timer['timerId'] == 7 and timer['recipeId'] == 5 and timer['step_order'] is None
    assert timer['url'] == 'tchikook://cooking/5'


def test_aggregated_likes_and_plurals():
    miam = get_type('post_miam')
    assert miam.render({'actors': ['Léa', 'Tom', 'Zoé', 'Max'], 'dish': '« Quiche »'}) == (
        'Nouveaux miams',
        'Léa et 3 autres ont aimé « Quiche ».',
    )
    assert miam.render({'actor': 'Léa', 'dish': 'ton plat'})[1] == 'Léa a aimé ton plat.'

    recap = get_type('weekly_recap')
    assert recap.render({'cooked': 4, 'new_dishes': 2})[1] == 'Cette semaine tu as cuisiné 4 plats, dont 2 nouveaux.'
    assert recap.render({'cooked': 1, 'new_dishes': 1})[1] == 'Cette semaine tu as cuisiné 1 plat, dont 1 nouveau.'
    assert recap.render({'cooked': 0, 'friends_posts': 3})[1].startswith('Tes amis ont partagé 3 plats')

    invitation = get_type('meal_invitation')
    ctx = {'actor': 'Léa', 'meal_time': 'dinner', 'date': date(2026, 11, 14)}
    assert invitation.render(ctx)[1] == 'Léa t’invite au dîner du samedi 14 novembre.'
    assert invitation.render({**ctx, 'count': 3})[1] == 'Léa t’invite à 3 repas.'


def test_a_mon_tour_renders_single_and_aggregated():
    my_turn = get_type('a_mon_tour')
    assert my_turn.enabled and my_turn.category == 'social' and not my_turn.transactional
    assert my_turn.render({'actor': 'Léa', 'dish': '« Quiche »'}) == (
        'Ton plat donne envie',
        'Léa veut refaire « Quiche ».',
    )
    assert my_turn.render({'actors': ['Léa', 'Tom', 'Zoé'], 'dish': '« Quiche »'})[1] == (
        'Léa et 2 autres veulent refaire « Quiche ».'
    )
    cooked = get_type('a_mon_tour_cooked')
    assert cooked.render({'actor': 'Léa', 'dish': '« Quiche »'})[1] == 'Léa a refait « Quiche ».'
    assert cooked.render({'actors': ['Léa', 'Tom'], 'dish': '« Quiche »'})[1] == 'Léa et Tom ont refait « Quiche ».'
    data = cooked.build_data({'post_id': 4, 'cooked_post_id': 9}, notification_id=1)
    assert data['url'] == 'tchikook://post/4' and data['cooked_post_id'] == 9
