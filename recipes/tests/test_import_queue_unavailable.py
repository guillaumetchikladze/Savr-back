"""Les endpoints d'import ne bloquent ni ne plantent quand le broker est indisponible."""

import time
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from core.celery_dispatch import publish, publish_tracked
from recipes.models import RecipeImportRequest


@pytest.fixture
def api_client(db):
    user = get_user_model().objects.create_user(
        validated_at=timezone.now(), username='queue_user', email='queue_user@example.com', password='password123'
    )
    client = APIClient()
    client.force_authenticate(user)
    return client


URL = '/api/recipes/import_from_url/'


def test_import_from_url_returns_503_fast_when_broker_down(api_client):
    with mock.patch('celery.app.task.Task.apply_async', side_effect=ConnectionError('redis down')):
        started = time.monotonic()
        response = api_client.post(URL, {'url': 'https://example.com/recette-1'}, format='json')
        elapsed = time.monotonic() - started

    assert response.status_code == 503
    assert response.json()['error'] == 'queue_unavailable'
    assert elapsed < 2
    request = RecipeImportRequest.objects.get()
    assert request.status == RecipeImportRequest.STATUS_ERROR
    assert request.error_message == 'queue_unavailable'


def test_import_from_url_success_keeps_request_id_and_task_id(api_client, celery_delay):
    response = api_client.post(URL, {'url': 'https://example.com/recette-2'}, format='json')

    assert response.status_code == 202
    request = RecipeImportRequest.objects.get(pk=response.json()['request_id'])
    assert request.task_id
    assert celery_delay.calls[0][0] == 'recipes.tasks.process_recipe_import_from_url'
    assert celery_delay.calls[0][1] == (str(request.id),)
    status_response = api_client.get(f'/api/recipes/formalize/status/{request.id}/')
    assert status_response.status_code == 200


def test_generate_from_idea_returns_503_when_broker_down(api_client):
    with mock.patch('celery.app.task.Task.apply_async', side_effect=ConnectionError('redis down')):
        response = api_client.post(
            '/api/recipes/generate_from_idea/', {'idea_text': 'un gratin de courgettes'}, format='json'
        )

    assert response.status_code == 503
    assert response.json()['error'] == 'queue_unavailable'


def test_publish_tracked_returns_task_id_or_none():
    task = mock.MagicMock()
    assert publish_tracked(task, args=[1], task_id='abc') == 'abc'
    assert task.apply_async.call_args.kwargs['task_id'] == 'abc'
    task.apply_async.side_effect = ConnectionError('down')
    assert publish_tracked(task, args=[1]) is None
    assert publish(task, args=[1]) is False


def test_converted_call_sites_never_raise_when_broker_down(db):
    from recipes.services.recipe_search_index import schedule_recipe_search_reindex

    with mock.patch('celery.app.task.Task.apply_async', side_effect=ConnectionError('redis down')):
        schedule_recipe_search_reindex(0)  # no-op
        from stats.tasks import schedule_badge_evaluation

        schedule_badge_evaluation(999999)
