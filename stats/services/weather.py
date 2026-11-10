"""
Météo des badges (phase 2, spec badges-quirky « La météo ») : Open-Meteo, gratuit et sans clé.

- Opt-in : l'utilisateur déclare **sa ville** (`User.city`, `PUT /api/weather/city/`), jamais de GPS. Géocodée
  une seule fois (API de géocodage Open-Meteo) dans une tâche Celery, coordonnées gardées sur l'utilisateur.
- Température max et cumul de pluie du jour par (lieu, date), mis en cache en base (`WeatherDay`, partagé entre
  cuisiniers d'une même ville, coordonnées arrondies à 0,01°). Jours de plus de `ARCHIVE_DELAY_DAYS` jours :
  API d'archive ; plus récents : API de prévision (`start_date` / `end_date`). Une requête par plage d'au plus un an.
- Tout appel réseau a lieu dans une tâche Celery (`stats.tasks.evaluate_user_badges` /
  `geocode_user_city_task`), jamais dans une requête HTTP. Délais explicites (connexion, lecture).
- Interrupteur : `WEATHER_BADGES_ENABLED` (défaut False) : rien n'est appelé ni évalué sans lui.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date, timedelta

import requests
from django.conf import settings
from django.utils import timezone

from ..models import WeatherDay

logger = logging.getLogger(__name__)

GEOCODING_URL = 'https://geocoding-api.open-meteo.com/v1/search'
ARCHIVE_URL = 'https://archive-api.open-meteo.com/v1/archive'
FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
TIMEOUT = (5, 15)
ARCHIVE_DELAY_DAYS = 5  # l'archive a quelques jours de retard
FORECAST_MAX_PAST_DAYS = 90
MAX_RANGE_DAYS = 366
CITY_MAX_LENGTH = 120


class WeatherError(Exception):
    pass


def enabled() -> bool:
    return bool(getattr(settings, 'WEATHER_BADGES_ENABLED', False))


def _get_json(url: str, params: dict) -> dict:
    """Seul point d'accès réseau du module (mocké dans les tests)."""
    try:
        resp = requests.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise WeatherError(str(exc)) from exc


def location_key(lat: float, lon: float) -> str:
    return f'{lat:.2f},{lon:.2f}'


def user_location(user) -> str | None:
    lat, lon = getattr(user, 'city_latitude', None), getattr(user, 'city_longitude', None)
    return location_key(lat, lon) if lat is not None and lon is not None else None


# ---------------------------------------------------------------------------
# Ville (opt-in)
# ---------------------------------------------------------------------------
def city_payload(user) -> dict:
    return {
        'enabled': enabled(),
        'city': user.city or None,
        'label': user.city_label or None,
        'geocoded': user_location(user) is not None,
    }


def set_city(user, city: str | None) -> dict:
    """Enregistre (ou efface) la ville ; le géocodage part en tâche au commit. Les badges obtenus restent."""
    city = ' '.join((city or '').split())[:CITY_MAX_LENGTH]
    if city and city == user.city and user_location(user):
        return city_payload(user)  # même ville, déjà géocodée : rien à refaire
    user.city = city
    user.city_label = ''
    user.city_latitude = user.city_longitude = user.city_geocoded_at = None
    user.save(update_fields=['city', 'city_label', 'city_latitude', 'city_longitude', 'city_geocoded_at'])
    if city and enabled():
        from core.celery_dispatch import enqueue_on_commit

        from ..tasks import geocode_user_city_task

        enqueue_on_commit(geocode_user_city_task, args=[user.id])
    return city_payload(user)


def geocode(city: str) -> dict | None:
    data = _get_json(GEOCODING_URL, {'name': city, 'count': 1, 'language': 'fr', 'format': 'json'})
    results = data.get('results') or []
    if not results:
        return None
    top = results[0]
    label = ', '.join(p for p in (top.get('name'), top.get('country')) if p)
    return {'label': label, 'latitude': float(top['latitude']), 'longitude': float(top['longitude'])}


def geocode_user_city(user) -> bool:
    """Géocode la ville déclarée (une fois). True si des coordonnées sont disponibles. Réseau : tâche seulement."""
    if not user.city:
        return False
    if user_location(user):
        return True
    found = geocode(user.city)
    if not found:
        logger.info('[weather] ville introuvable pour user=%s', user.id)
        return False
    user.city_label = found['label'][:160]
    user.city_latitude, user.city_longitude = found['latitude'], found['longitude']
    user.city_geocoded_at = timezone.now()
    user.save(update_fields=['city_label', 'city_latitude', 'city_longitude', 'city_geocoded_at'])
    return True


# ---------------------------------------------------------------------------
# Météo du jour (cache en base)
# ---------------------------------------------------------------------------
def _chunks(days: list[date]) -> Iterable[tuple[date, date]]:
    """Plages [début, fin] d'au plus MAX_RANGE_DAYS jours couvrant les dates triées."""
    start = prev = None
    for day in days:
        if start is None:
            start = prev = day
        elif (day - start).days >= MAX_RANGE_DAYS:
            yield start, prev
            start = day
        prev = day
    if start is not None:
        yield start, prev


def _fetch(lat: float, lon: float, start: date, end: date, *, recent: bool) -> list[WeatherDay]:
    params = {
        'latitude': round(lat, 4),
        'longitude': round(lon, 4),
        'start_date': start.isoformat(),
        'end_date': end.isoformat(),
        'daily': 'temperature_2m_max,precipitation_sum',
        'timezone': 'auto',
    }
    data = _get_json(FORECAST_URL if recent else ARCHIVE_URL, params)
    daily = data.get('daily') or {}
    loc = location_key(lat, lon)
    rows = []
    for day, tmax, rain in zip(
        daily.get('time') or (), daily.get('temperature_2m_max') or (), daily.get('precipitation_sum') or ()
    ):
        rows.append(WeatherDay(location=loc, date=date.fromisoformat(day), temperature_max=tmax, precipitation_mm=rain))
    return rows


def ensure_weather(lat: float, lon: float, days: Iterable[date], *, today: date | None = None) -> int:
    """Récupère la météo des jours absents du cache (une requête par plage). Renvoie le nombre de jours ajoutés."""
    today = today or timezone.localdate()
    loc = location_key(lat, lon)
    wanted = {d for d in days if d <= today}
    if not wanted:
        return 0
    known = set(WeatherDay.objects.filter(location=loc, date__in=wanted).values_list('date', flat=True))
    missing = sorted(wanted - known)
    if not missing:
        return 0
    archive_end = today - timedelta(days=ARCHIVE_DELAY_DAYS)
    old = [d for d in missing if d <= archive_end]
    recent = [d for d in missing if d > archive_end and d >= today - timedelta(days=FORECAST_MAX_PAST_DAYS)]
    rows: list[WeatherDay] = []
    for group, is_recent in ((old, False), (recent, True)):
        for start, end in _chunks(group):
            rows.extend(_fetch(lat, lon, start, end, recent=is_recent))
    rows = [r for r in rows if r.date <= today]  # jamais de prévision du futur
    WeatherDay.objects.bulk_create(rows, ignore_conflicts=True)
    return len(rows)


def weather_by_date(location: str | None, days: Iterable[date]) -> dict[date, WeatherDay]:
    """Lecture seule du cache (1 requête, jamais de réseau)."""
    days = set(days)
    if not location or not days:
        return {}
    return {w.date: w for w in WeatherDay.objects.filter(location=location, date__in=days)}


def refresh_user_weather(user, days: Iterable[date]) -> int:
    """Tâche Celery : géocode si besoin puis complète le cache météo des jours cuisinés. Ne lève pas."""
    if not enabled():
        return 0
    try:
        if not geocode_user_city(user):
            return 0
        return ensure_weather(user.city_latitude, user.city_longitude, days)
    except WeatherError:
        logger.warning('[weather] Open-Meteo indisponible pour user=%s', user.id, exc_info=True)
        return 0
