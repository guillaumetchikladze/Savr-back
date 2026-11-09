"""Historique « Tes conversations » : liste, renommage, suppression douce, reprise, titres automatiques."""

from datetime import timedelta
from unittest import mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone

from chat.models import Conversation, Message, MessageFeedback
from chat.services import conversation_title as titles
from chat.tasks import generate_conversation_title, purge_deleted_conversations_task

pytestmark = pytest.mark.django_db


def _conv(user, *, title='Nouvelle conversation', messages=(), **kw):
    conv = Conversation.objects.create(user=user, title=title, **kw)
    for role, content in messages:
        Message.objects.create(conversation=conv, role=role, content=content)
    return conv


U, A = Message.ROLE_USER, Message.ROLE_ASSISTANT


# ---------------------------------------------------------------------------
# Liste
# ---------------------------------------------------------------------------
def test_sessions_list_own_non_empty_newest_first(api, user, make_user):
    other = make_user()
    old = _conv(user, title='Vieille', messages=[(U, 'Idée de soupe ?'), (A, 'Une soupe de potiron.')])
    new = _conv(user, title='Récente', messages=[(U, 'Menu de la semaine'), (A, 'Voici 5 idées.')])
    Conversation.objects.filter(pk=old.pk).update(updated_at=timezone.now() - timedelta(days=2))
    _conv(user)  # vide → exclue
    _conv(user, messages=[(A, 'Bonjour')])  # sans message utilisateur → exclue
    _conv(other, messages=[(U, 'Pas à toi')])
    deleted = _conv(user, messages=[(U, 'Supprimée')], deleted_at=timezone.now())

    resp = api.as_user(user).get('/api/chat/sessions/')

    assert resp.status_code == 200
    assert resp.data['count'] == 2
    rows = resp.data['results']
    assert [r['id'] for r in rows] == [new.id, old.id]
    assert deleted.id not in [r['id'] for r in rows]
    first = rows[0]
    assert first['title'] == 'Récente'
    assert first['message_count'] == 2
    assert first['last_message_preview'] == 'Voici 5 idées.'
    assert first['last_message_role'] == 'assistant'
    assert first['last_message_at'] is not None
    assert set(first) >= {'id', 'title', 'title_source', 'created_at', 'updated_at'}


def test_sessions_list_is_paginated_and_query_count_is_constant(api, user):
    for i in range(3):
        _conv(user, messages=[(U, f'Question {i}'), (A, f'Réponse {i}')])
    client = api.as_user(user)
    with CaptureQueriesContext(connection) as small:
        client.get('/api/chat/sessions/?page_size=2')
    for i in range(10):
        _conv(user, messages=[(U, f'Autre {i}'), (A, '{"query": "x"}')])
    with CaptureQueriesContext(connection) as big:
        resp = client.get('/api/chat/sessions/?page_size=10')
    assert resp.data['count'] == 13
    assert len(resp.data['results']) == 10
    assert resp.data['next']
    assert len(big) == len(small)


def test_preview_hides_tool_json_and_uses_tutoiement(api, user):
    conv = _conv(user, messages=[(U, 'Mes repas ?')])
    Message.objects.create(
        conversation=conv, role=A, content='{"query": "pâtes"}', ui_payload={'tool_traces': [{'tool': 'x'}]}
    )
    row = api.as_user(user).get('/api/chat/sessions/').data['results'][0]
    assert row['last_message_preview'] == 'Tchikook a consulté tes données'


def test_legacy_conversations_list_keeps_empty_but_hides_deleted(api, user):
    empty = _conv(user)
    gone = _conv(user, messages=[(U, 'x')], deleted_at=timezone.now())
    resp = api.as_user(user).get('/api/chat/conversations/')
    ids = [c['id'] for c in resp.data['results']]
    assert empty.id in ids and gone.id not in ids
    assert set(resp.data['results'][0]) == {'id', 'title', 'created_at', 'updated_at', 'last_message_preview'}


def test_create_session_returns_session_payload(api, user):
    resp = api.as_user(user).post('/api/chat/sessions/', {}, format='json')
    assert resp.status_code == 201
    assert resp.data['message_count'] == 0
    assert resp.data['title'] == 'Nouvelle conversation'
    assert Conversation.objects.get(pk=resp.data['id']).user == user


# ---------------------------------------------------------------------------
# Renommer / supprimer / permissions
# ---------------------------------------------------------------------------
def test_rename_session_sets_user_title_without_bumping(api, user):
    conv = _conv(user, title='Auto', messages=[(U, 'Salut')])
    before = Conversation.objects.get(pk=conv.pk).updated_at

    resp = api.as_user(user).patch(
        f'/api/chat/sessions/{conv.id}/', {'title': '  Dîners   de la semaine '}, format='json'
    )

    assert resp.status_code == 200
    assert resp.data['title'] == 'Dîners de la semaine'
    assert resp.data['title_source'] == 'user'
    conv.refresh_from_db()
    assert conv.updated_at == before


@pytest.mark.parametrize('payload', [{'title': '   '}, {'title': 'x' * 121}, {}, {'title': 12}])
def test_rename_rejects_invalid_titles(api, user, payload):
    conv = _conv(user, messages=[(U, 'Salut')])
    resp = api.as_user(user).patch(f'/api/chat/sessions/{conv.id}/', payload, format='json')
    assert resp.status_code == 400
    assert resp.data['code'] == 'invalid_title'


def test_put_is_not_allowed(api, user):
    conv = _conv(user, messages=[(U, 'Salut')])
    resp = api.as_user(user).put(f'/api/chat/sessions/{conv.id}/', {'title': 'x'}, format='json')
    assert resp.status_code == 405


def test_delete_is_soft_and_hides_everywhere(api, user):
    conv = _conv(user, messages=[(U, 'Salut'), (A, 'Coucou')])
    msg = conv.messages.first()
    client = api.as_user(user)

    assert client.delete(f'/api/chat/sessions/{conv.id}/').status_code == 204

    conv.refresh_from_db()
    assert conv.deleted_at is not None
    assert client.get(f'/api/chat/sessions/{conv.id}/').status_code == 404
    assert client.get(f'/api/chat/sessions/{conv.id}/messages/').status_code == 404
    assert client.get(f'/api/chat/conversations/{conv.id}/messages/').status_code == 404
    assert client.delete(f'/api/chat/sessions/{conv.id}/').status_code == 404
    assert client.post(f'/api/chat/messages/{msg.id}/feedback/', {'rating': 'up'}).status_code == 404
    assert client.get('/api/chat/sessions/').data['count'] == 0


def test_purge_deletes_old_soft_deleted_conversations():
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username='purge', email='purge@test.com', password='x')
    old = _conv(user, messages=[(U, 'a')], deleted_at=timezone.now() - timedelta(days=31))
    recent = _conv(user, messages=[(U, 'b')], deleted_at=timezone.now() - timedelta(days=2))
    alive = _conv(user, messages=[(U, 'c')])
    MessageFeedback.objects.create(message=old.messages.first(), user=user, rating='up')

    purge_deleted_conversations_task()

    assert not Conversation.objects.filter(pk=old.pk).exists()
    assert Conversation.objects.filter(pk__in=[recent.pk, alive.pk]).count() == 2


def test_other_users_session_is_404(api, user, make_user):
    conv = _conv(make_user(), title='Secret', messages=[(U, 'Hello')])
    client = api.as_user(user)
    assert client.get(f'/api/chat/sessions/{conv.id}/').status_code == 404
    assert client.patch(f'/api/chat/sessions/{conv.id}/', {'title': 'Pris'}, format='json').status_code == 404
    assert client.delete(f'/api/chat/sessions/{conv.id}/').status_code == 404
    assert client.get(f'/api/chat/sessions/{conv.id}/messages/').status_code == 404
    conv.refresh_from_db()
    assert conv.title == 'Secret' and conv.deleted_at is None


def test_anonymous_is_rejected(api):
    assert api.get('/api/chat/sessions/').status_code in (401, 403)


# ---------------------------------------------------------------------------
# Reprise : messages
# ---------------------------------------------------------------------------
def test_messages_latest_and_before_for_resuming(api, user):
    conv = _conv(user, messages=[(U if i % 2 == 0 else A, f'm{i}') for i in range(7)])
    client = api.as_user(user)

    latest = client.get(f'/api/chat/sessions/{conv.id}/messages/?latest=1&page_size=3').data
    assert [m['content'] for m in latest['results']] == ['m4', 'm5', 'm6']
    assert latest['has_more'] is True and latest['count'] == 7

    older = client.get(f'/api/chat/sessions/{conv.id}/messages/?before={latest["results"][0]["id"]}&page_size=3').data
    assert [m['content'] for m in older['results']] == ['m1', 'm2', 'm3']
    assert older['has_more'] is True

    oldest = client.get(f'/api/chat/sessions/{conv.id}/messages/?before={older["results"][0]["id"]}&page_size=3').data
    assert [m['content'] for m in oldest['results']] == ['m0']
    assert oldest['has_more'] is False

    legacy = client.get(f'/api/chat/conversations/{conv.id}/messages/?offset=0&page_size=5').data
    assert [m['content'] for m in legacy['results']] == ['m0', 'm1', 'm2', 'm3', 'm4']
    assert legacy['has_more'] is True

    bad = client.get(f'/api/chat/sessions/{conv.id}/messages/?page_size=abc&offset=-3')
    assert bad.status_code == 200 and bad.data['page_size'] == 50 and bad.data['offset'] == 0


# ---------------------------------------------------------------------------
# Titres automatiques
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        (
            'Salut ! Tu peux me proposer des idées de dîners végé pour la semaine ?',
            'Tu peux me proposer des idées de dîners végé pour la…',
        ),
        ('bonjour, quoi faire avec des courgettes. Merci', 'Quoi faire avec des courgettes'),
        ('https://www.marmiton.org/recettes/recette_gateau.aspx', 'Recette de marmiton.org'),
        ('   ', 'Nouvelle conversation'),
        ('Gratin\nAvec des pommes de terre', 'Gratin Avec des pommes de terre'),
        ('Idées de gâteaux faciles\npour un anniversaire', 'Idées de gâteaux faciles'),
    ],
)
def test_smart_title(text, expected):
    title = titles.smart_title(text)
    assert title == expected
    assert len(title) <= titles.MAX_TITLE_CHARS + 1


def test_initial_title_is_idempotent_and_never_overrides_user_title(user):
    conv = _conv(user)
    assert titles.apply_initial_title(conv.id, 'Quoi faire avec des courgettes ?') == 'Quoi faire avec des courgettes ?'
    assert titles.apply_initial_title(conv.id, 'Autre chose complètement') is None
    renamed = _conv(user, title='Mon titre', title_source=Conversation.TITLE_USER)
    assert titles.apply_initial_title(renamed.id, 'Écrase-moi') is None
    conv.refresh_from_db()
    renamed.refresh_from_db()
    assert (conv.title, conv.title_source) == ('Quoi faire avec des courgettes ?', 'auto')
    assert renamed.title == 'Mon titre'


@override_settings(CHAT_TITLE_LLM_ENABLED=True)
def test_title_task_uses_llm_after_first_exchange(user):
    conv = _conv(user, messages=[(U, 'Salut, des idées pour ce soir avec du riz ?'), (A, 'Un riz cantonais.')])
    with (
        mock.patch.object(titles, 'llm_available', return_value=True),
        mock.patch.object(titles, '_call_llm', return_value='{"title": "Idées de riz pour ce soir."}') as llm,
        mock.patch('chat.tasks.broadcast_title') as broadcast,
    ):
        generate_conversation_title(conv.id)
    conv.refresh_from_db()
    assert (conv.title, conv.title_source) == ('Idées de riz pour ce soir', 'llm')
    assert llm.call_count == 1
    broadcast.assert_called_once_with(conv.id, 'Idées de riz pour ce soir')


@pytest.mark.parametrize(
    'raw',
    ['pas du json', '{"title": ""}', '{"title": "Menu minceur à 1200 calories"}', '{"title": "' + 'x' * 80 + '"}'],
)
def test_title_task_falls_back_to_message_when_llm_is_unusable(user, raw):
    conv = _conv(user, messages=[(U, 'Quoi faire avec des courgettes ?'), (A, 'Un gratin.')])
    with (
        mock.patch.object(titles, 'llm_available', return_value=True),
        mock.patch.object(titles, '_call_llm', return_value=raw),
        mock.patch('chat.tasks.broadcast_title'),
    ):
        generate_conversation_title(conv.id)
    conv.refresh_from_db()
    assert (conv.title, conv.title_source) == ('Quoi faire avec des courgettes ?', 'auto')


def test_title_task_handles_llm_errors_and_skips_later_turns(user):
    conv = _conv(user, messages=[(U, 'Premier message assez long'), (A, 'ok')])
    with (
        mock.patch.object(titles, 'llm_available', return_value=True),
        mock.patch.object(titles, '_call_llm', side_effect=TimeoutError('lent')),
        mock.patch('chat.tasks.broadcast_title'),
    ):
        generate_conversation_title(conv.id)
    conv.refresh_from_db()
    assert conv.title_source == 'auto'

    Message.objects.create(conversation=conv, role=U, content='Deuxième tour')
    with mock.patch.object(titles, 'llm_available', return_value=True), mock.patch.object(titles, '_call_llm') as llm:
        generate_conversation_title(conv.id)
    llm.assert_not_called()


def test_title_task_never_touches_user_or_deleted_titles(user):
    renamed = _conv(user, title='À moi', title_source=Conversation.TITLE_USER, messages=[(U, 'Bonjour toi')])
    deleted = _conv(user, messages=[(U, 'Bonjour toi')], deleted_at=timezone.now())
    with mock.patch.object(titles, 'llm_available', return_value=True), mock.patch.object(titles, '_call_llm') as llm:
        generate_conversation_title(renamed.id)
        generate_conversation_title(deleted.id)
    llm.assert_not_called()
    renamed.refresh_from_db()
    assert renamed.title == 'À moi'


def test_title_task_without_llm_uses_first_message(user):
    conv = _conv(user, messages=[(U, 'Coucou ! Un dessert sans four ?'), (A, 'Un tiramisu.')])
    with mock.patch.object(titles, '_call_llm') as llm, mock.patch('chat.tasks.broadcast_title') as broadcast:
        generate_conversation_title(conv.id)
    llm.assert_not_called()
    conv.refresh_from_db()
    assert conv.title == 'Un dessert sans four ?'
    broadcast.assert_called_once()
