"""Mots de l'auteur → recettes (hl1) : passages de la légende surlignés et liés à une recette du post."""

from __future__ import annotations

import uuid
from datetime import date
from unittest import mock

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from recipes.models import Post
from recipes.serializers import CaptionLinksFieldMixin
from recipes.services.caption_links import caption_links

FEED_URL = '/api/posts/?is_published=true'


def spans(caption: str, *titles: str) -> list[tuple[str, int]]:
    """(texte surligné, n° de recette 1-based) — découpe en UTF-16 comme l'app."""
    units = caption.encode('utf-16-le')
    links = caption_links(caption, [(i + 1, t) for i, t in enumerate(titles)])
    return [(units[2 * link['start'] : 2 * link['end']].decode('utf-16-le'), link['recipe_id']) for link in links]


# ---------------------------------------------------------------------------
# Correspondance (pur, sans base)
# ---------------------------------------------------------------------------
def test_owner_example_cake_sale_in_tom_s_brunch():
    caption = "Salade d'été, cake salé et omelette : le brunch parfait"
    assert spans(caption, 'Cake salé tomates séchées et chèvre') == [('cake salé', 1)]
    assert caption_links(caption, [(7, 'Cake salé tomates séchées et chèvre')]) == [
        {'start': 14, 'end': 23, 'recipe_id': 7}
    ]


def test_multi_dish_meal_links_each_recipe_once_without_overlap():
    caption = "Salade d'été, cake salé et omelette : le brunch parfait"
    got = spans(caption, 'Cake salé tomates séchées et chèvre', 'Salade de lentilles', 'Omelette aux herbes')
    # « salade » seule ne dit pas laquelle (catégorie) ; « omelette » = 1er mot du titre.
    assert got == [('cake salé', 1), ('omelette', 3)]
    assert spans(caption, "Salade d'été pastèque feta")[0] == ("Salade d'été", 1)


@pytest.mark.parametrize(
    ('caption', 'title', 'expected'),
    [
        ('Les GÂTEAUX au chocolat de mamie', 'Gâteau au chocolat', 'GÂTEAUX au chocolat'),
        ('gateau au chocolat', 'Gâteaux au Chocolat', 'gateau au chocolat'),
        ('Mes crêpes au sarrasin', 'Crêpe au sarrasin', 'crêpes au sarrasin'),
        ('des choux à la crème', 'Chou à la crème', 'choux à la crème'),
        ("Poireaux vinaigrette, l'été", 'Poireau vinaigrette', 'Poireaux vinaigrette'),
        ('Œufs cocotte du dimanche', 'Oeufs cocotte', 'Œufs cocotte'),
        ('pommes-de-terre sautées', 'Pommes de terre sautées', 'pommes-de-terre sautées'),
        ('risotto aux champignons, trop bon', 'Risotto crémeux aux champignons', 'risotto aux champignons'),
        ('UN TIRAMISU !!!', 'Tiramisu', 'TIRAMISU'),
    ],
)
def test_accents_case_plurals_apostrophes_and_hyphens(caption, title, expected):
    assert spans(caption, title) == [(expected, 1)]


def test_decomposed_accents_keep_original_offsets():
    caption = 'Cake salé du soir'  # « é » décomposé (e + accent combinant)
    assert spans(caption, 'Cake salé') == [('Cake salé', 1)]


def test_risotto_alone_is_enough_when_it_names_the_dish():
    assert spans('Mon risotto du dimanche', 'Risotto aux champignons') == [('risotto', 1)]
    assert spans('Le pain était parfait', 'Pain perdu') == [('pain', 1)]
    # mais pas un mot secondaire du titre, ni une catégorie seule
    assert spans('Plein de champignons', 'Risotto aux champignons') == []
    assert spans("Une salade d'été", 'Salade de lentilles') == []
    assert spans('Une bonne tarte', 'Tarte aux pommes') == []


def test_no_false_positive_on_sel_vs_sale_or_on_prefixes():
    assert spans('Un peu de sel', 'Caramel au beurre salé') == []
    assert spans('un goût salé', 'Caramel au beurre salé') == []  # adjectif seul : pas le nom du plat
    assert spans('Cake salé', 'Fleur de sel') == []
    assert spans('Une salade', 'Salé sucré') == []
    assert spans('Les pâtisseries', 'Pâtes au pesto') == []


def test_generic_words_never_anchor_a_passage():
    assert spans('Recette maison', 'Recette maison') == []
    assert spans('Le brunch parfait', 'Brunch parfait') == []
    assert spans('Un dessert facile', 'Dessert facile au chocolat') == []
    assert spans('Un dessert facile au chocolat', 'Dessert facile au chocolat') == [('dessert facile au chocolat', 1)]


def test_punctuation_breaks_a_passage_but_stopwords_do_not():
    assert spans('Cake, salé', 'Cake salé') == []
    assert spans('Tarte : pommes', 'Tarte aux pommes') == []
    assert spans('tarte avec des pommes', 'Tarte aux pommes') == [('tarte avec des pommes', 1)]


def test_prefers_short_title_on_exact_match_and_longest_run_elsewhere():
    # « cake salé » nomme exactement « Cake salé » plutôt que la variante plus longue
    assert spans('cake salé', 'Cake salé', 'Cake salé tomates séchées') == [('cake salé', 1)]
    caption = 'Cake salé tomates séchées et chèvre, et un cake salé nature'
    assert spans(caption, 'Cake salé', 'Cake salé tomates séchées et chèvre') == [
        ('Cake salé tomates séchées et chèvre', 2),
        ('cake salé', 1),
    ]


def test_ambiguous_tie_highlights_nothing():
    assert spans('cake salé', 'Cake salé aux olives', 'Cake salé au jambon') == []
    assert spans('Le poulet', 'Poulet basquaise', 'Poulet rôti') == []
    # deux recettes de même titre (deux auteurs) : doute
    assert spans('Tiramisu', 'Tiramisu', 'Tiramisu') == []


def test_one_span_per_recipe_first_occurrence():
    assert spans('Tiramisu, encore du tiramisu', 'Tiramisu') == [('Tiramisu', 1)]


def test_emoji_offsets_are_utf16_like_javascript():
    caption = '🍰🎉 Cake salé du soir 🧀'
    links = caption_links(caption, [(1, 'Cake salé')])
    assert links == [{'start': 5, 'end': 14, 'recipe_id': 1}]  # chaque emoji = 2 unités UTF-16
    assert spans(caption, 'Cake salé') == [('Cake salé', 1)]
    assert spans('🍝pâtes au pesto🌿', 'Pâtes au pesto') == [('pâtes au pesto', 1)]


def test_empty_inputs_and_bad_recipes():
    assert caption_links('', [(1, 'Tiramisu')]) == []
    assert caption_links('   ', [(1, 'Tiramisu')]) == []
    assert caption_links(None, [(1, 'Tiramisu')]) == []
    assert caption_links('Tiramisu', []) == []
    assert caption_links('Tiramisu', [(None, 'Tiramisu'), (2, ''), (3, None)]) == []
    assert caption_links('2 4 6', [(1, '2 4 6')]) == []


# ---------------------------------------------------------------------------
# API : feed, détail, modification, requêtes
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def cook(make_meal_plan):
    def _cook(owner, *, recipes=(), comment='', label=''):
        mp = make_meal_plan(
            owner,
            day=date.today(),
            meal_time='other',
            slot_key=uuid.uuid4().hex,
            recipes=list(recipes),
            confirmed=True,
        )
        batch = mp.meal_plan_recipe_batches.first()
        return Post.objects.create(
            user=owner,
            meal_plan=mp,
            recipe_batch=batch.recipe_batch if batch else None,
            comment=comment,
            label=label,
            is_published=True,
        )

    return _cook


@pytest.mark.django_db
def test_feed_and_detail_expose_caption_links_for_every_recipe_of_the_meal(
    user, make_user, make_recipe, cook, follow, api
):
    tom = make_user()
    follow(user, tom)
    cake = make_recipe(tom, 'Cake salé tomates séchées et chèvre')
    omelette = make_recipe(tom, 'Omelette aux herbes')
    salad = make_recipe(tom, 'Salade de lentilles')
    caption = "Salade d'été, cake salé et omelette : le brunch parfait"
    post = cook(tom, recipes=[salad, cake, omelette], comment=caption)
    cook(tom, label='Pâtes', comment='Rien à voir')

    client = api.as_user(user)
    feed = {p['id']: p for p in client.get(FEED_URL).json()['results']}
    expected = [
        {'start': 14, 'end': 23, 'recipe_id': cake.id},
        {'start': 27, 'end': 35, 'recipe_id': omelette.id},
    ]
    assert feed[post.id]['caption_links'] == expected
    assert all(p['caption_links'] == [] for pid, p in feed.items() if pid != post.id)

    detail = client.get(f'/api/posts/{post.id}/').json()
    assert detail['caption_links'] == expected


@pytest.mark.django_db
def test_edit_recomputes_links(user, make_recipe, cook, api):
    risotto = make_recipe(user, 'Risotto aux champignons')
    post = cook(user, recipes=[risotto], comment='Trop bon')
    client = api.as_user(user)
    assert client.get(f'/api/posts/{post.id}/').json()['caption_links'] == []

    resp = client.patch(
        f'/api/posts/{post.id}/update-content/', {'comment': '  Mon risotto du dimanche  '}, format='json'
    )
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body['comment'] == 'Mon risotto du dimanche'
    assert body['caption_links'] == [{'start': 4, 'end': 11, 'recipe_id': risotto.id}]


@pytest.mark.django_db
def test_feed_query_count_unchanged_by_caption_links(user, make_user, make_recipe, cook, follow, api):
    client = api.as_user(user)
    for i in range(4):
        friend = make_user()
        follow(user, friend)
        cook(
            friend,
            recipes=[make_recipe(friend, f'Tiramisu {i}'), make_recipe(friend, 'Cake salé')],
            comment='cake salé',
        )
        cook(friend, label='Pâtes au pesto', comment='Des pâtes au pesto')

    def feed_queries():
        client.get(FEED_URL)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(FEED_URL)
        return resp.json()['results'], len(ctx.captured_queries)

    with mock.patch.object(CaptionLinksFieldMixin, 'get_caption_links', return_value=[]):
        _, without_links = feed_queries()
    results, with_links = feed_queries()
    assert with_links == without_links
    assert sum(bool(p['caption_links']) for p in results) == 4
