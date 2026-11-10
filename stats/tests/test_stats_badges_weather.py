"""Badges météo (phase 2) : ville opt-in, Open-Meteo mocké, cache par (lieu, date), évaluation en tâche seulement."""

from datetime import date, timedelta
from unittest import mock

import pytest

from stats.models import UserBadge, WeatherDay
from stats.services import weather
from stats.services.badges import BADGES_BY_CODE, badges_overview, evaluate_badges
from stats.tasks import geocode_user_city_task

pytestmark = pytest.mark.django_db

HOT_DAY = date(2026, 7, 20)


def _fake_open_meteo(calls):
    def fake(url, params):
        calls.append((url, dict(params)))
        if url == weather.GEOCODING_URL:
            return {'results': [{'name': 'Lyon', 'country': 'France', 'latitude': 45.7578, 'longitude': 4.832}]}
        start, end = date.fromisoformat(params['start_date']), date.fromisoformat(params['end_date'])
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return {
            'daily': {
                'time': [d.isoformat() for d in days],
                'temperature_2m_max': [36.5 if d == HOT_DAY else 21.0 for d in days],
                'precipitation_sum': [0.0 for _ in days],
            }
        }

    return fake


@pytest.fixture
def weather_on(settings):
    settings.WEATHER_BADGES_ENABLED = True


def test_weather_badges_are_hidden_and_never_evaluated_when_disabled(user, make_recipe, make_meal_plan):
    make_meal_plan(
        user, day=HOT_DAY, recipes=[make_recipe(user, 'Gratin', required_equipment=['four'])], confirmed=True
    )
    with mock.patch.object(weather, '_get_json') as net:
        overview = badges_overview(user, next_limit=None)
        geocode_user_city_task.run(user.id)
    assert not net.called
    assert not any(b['family'] == 'meteo' for b in overview['next'] + overview['earned'])


def test_city_opt_in_geocodes_in_a_task_and_awards_from_cache(
    api, user, make_recipe, make_meal_plan, weather_on, celery_delay, django_capture_on_commit_callbacks
):
    gratin = make_recipe(user, 'Gratin dauphinois', required_equipment=['four'])
    make_meal_plan(user, day=HOT_DAY, recipes=[gratin], confirmed=True)
    make_meal_plan(user, day=HOT_DAY + timedelta(days=30), recipes=[gratin], confirmed=True)
    client = api.as_user(user)

    calls = []
    with mock.patch.object(weather, '_get_json', side_effect=_fake_open_meteo(calls)):
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.put('/api/weather/city/', {'city': '  Lyon '}, format='json')
        assert resp.status_code == 200 and resp.data == {
            'enabled': True,
            'city': 'Lyon',
            'label': None,
            'geocoded': False,
        }
        assert not calls  # jamais de réseau dans la requête
        assert [c[0] for c in celery_delay.calls] == ['stats.tasks.geocode_user_city_task']
        # Le chemin requête n'évalue jamais la météo.
        assert 'four_canicule' not in {b['code'] for b in evaluate_badges(user, notify=False)}

        geocode_user_city_task.run(user.id)
        user.refresh_from_db()
        assert (user.city_label, round(user.city_latitude, 2)) == ('Lyon, France', 45.76)
        assert [c[0] for c in calls] == [weather.GEOCODING_URL, weather.ARCHIVE_URL]
        assert WeatherDay.objects.filter(location='45.76,4.83', date=HOT_DAY, temperature_max=36.5).exists()

        badge = UserBadge.objects.get(user=user, code='four_canicule')
        assert badge.context['temperature_max'] == 36.5 and badge.context['achieved_on'] == HOT_DAY.isoformat()

        calls.clear()
        geocode_user_city_task.run(user.id)  # tout est en cache : aucun appel
        assert calls == []

    assert client.get('/api/weather/city/').data['geocoded'] is True
    assert client.delete('/api/weather/city/').data == {'enabled': True, 'city': None, 'label': None, 'geocoded': False}
    assert UserBadge.objects.filter(user=user, code='four_canicule').exists()  # un badge obtenu reste
    assert BADGES_BY_CODE['raclette_chaleur'].rarity_guess == 'legendaire'


def test_open_meteo_down_never_breaks_badges(user, make_meal_plan, weather_on):
    user.city = 'Lyon'
    user.save(update_fields=['city'])
    make_meal_plan(user, day=HOT_DAY, confirmed=True)
    with mock.patch.object(weather, '_get_json', side_effect=weather.WeatherError('timeout')):
        assert geocode_user_city_task.run(user.id) >= 1  # first_cooked quand même


def test_city_updates_are_rate_limited_per_user_and_same_city_is_a_noop(
    api, user, make_user, weather_on, settings, celery_delay, django_capture_on_commit_callbacks
):
    """PUT /weather/city/ ne peut pas spammer le géocodage : plafond quotidien, et même ville géocodée = rien."""
    settings.WEATHER_CITY_UPDATES_PER_USER_DAY = 3
    client = api.as_user(user)
    with django_capture_on_commit_callbacks(execute=True):
        statuses = [
            client.put('/api/weather/city/', {'city': name}, format='json').status_code
            for name in ('Lyon', 'Paris', 'Nantes', 'Lille')
        ]
    assert statuses == [200, 200, 200, 429]
    assert len(celery_delay.calls) == 3
    user.refresh_from_db()
    assert user.city == 'Nantes'
    # Les autres utilisateurs ne sont pas touchés ; DELETE (aucun appel externe) n'est pas limité.
    assert api.as_user(make_user('autre')).put('/api/weather/city/', {'city': 'Lyon'}, format='json').status_code == 200
    assert api.as_user(user).delete('/api/weather/city/').status_code == 200

    # Même ville déjà géocodée : coordonnées gardées, aucune tâche enfilée.
    settings.WEATHER_CITY_UPDATES_PER_USER_DAY = 100
    geo = make_user('geo', city='Lyon', city_label='Lyon, France', city_latitude=45.76, city_longitude=4.83)
    celery_delay.calls.clear()
    with django_capture_on_commit_callbacks(execute=True):
        resp = api.as_user(geo).put('/api/weather/city/', {'city': ' Lyon '}, format='json')
    assert resp.status_code == 200 and resp.data['geocoded'] is True
    assert celery_delay.calls == []
    geo.refresh_from_db()
    assert geo.city_latitude == 45.76
