"""Contexte dynamique injecté à chaque tour agent (date, semaine, utilisateur)."""

from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser
from django.utils import timezone

_WEEKDAY_FR = (
    'lundi',
    'mardi',
    'mercredi',
    'jeudi',
    'vendredi',
    'samedi',
    'dimanche',
)


def _week_bounds(anchor):
    """Semaine lun→dim contenant anchor."""
    start = anchor - timedelta(days=anchor.weekday())
    end = start + timedelta(days=6)
    return start, end


def build_session_context_prompt(user: AbstractBaseUser) -> str:
    """
    Bloc système recalculé à chaque message (date du jour, semaines, user).
    """
    today = timezone.localdate()
    now = timezone.localtime()
    week_start, week_end = _week_bounds(today)
    next_week_start = week_end + timedelta(days=1)
    next_week_end = next_week_start + timedelta(days=6)
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    username = getattr(user, 'username', None) or getattr(user, 'email', 'utilisateur')

    return f"""Contexte Tchikook Agent (référence interne — ne pas recopier tel quel à l'utilisateur) :
- Utilisateur : {username} (id {user.pk})
- Maintenant : {now.strftime('%Y-%m-%d %H:%M')} ({_WEEKDAY_FR[today.weekday()]}) — fuseau {timezone.get_current_timezone()}
- Aujourd'hui : {today.isoformat()}
- Hier : {yesterday.isoformat()} | Demain : {tomorrow.isoformat()}
- Cette semaine (lun→dim) : {week_start.isoformat()} → {week_end.isoformat()}
- Semaine prochaine (lun→dim) : {next_week_start.isoformat()} → {next_week_end.isoformat()}

Règles dates et planning :
- Interprète toi-même « cette semaine », « la semaine prochaine », « demain », « ce week-end », etc. avec les dates ci-dessus.
- Ne demande PAS à l'utilisateur des dates au format AAAA-MM-JJ si une expression relative ou le contexte suffit.
- Pour une question sur le planning, appelle get_meal_plans tout de suite avec start_date et end_date calculés (YYYY-MM-DD).
- Ne demande des précisions de dates que si la période est vraiment ambiguë (ex. « en été » sans autre indice)."""
