"""Badges météo (phase 2, opt-in « Ta ville », Open-Meteo) : derrière `WEATHER_BADGES_ENABLED`."""

from __future__ import annotations

from .badge_types import BadgeDef

WEATHER_BADGES: tuple[BadgeDef, ...] = ()
