"""
Plafond mensuel de mise en forme IA d'une recette (décision ckd2 lot 2, 12/11/2026).

`POST /recipes/formalize/` et la mise en forme / l'import lancés par `POST /posts/cooked/`
(`recipe_text`, `import_url`) sont ouverts à tous (plus de droit `ai`), avec au plus
`settings.AI_RECIPE_MONTHLY_CAP` demandes par utilisateur et par mois calendaire (fuseau du projet).

Une demande compte dès qu'elle est créée (`RecipeImportRequest` marquée `payload.quota`), qu'elle
réussisse ou non : c'est l'appel au modèle qui coûte (sauf file de tâches injoignable). Au-delà : `formalize` répond 429 ; « J'ai cuisiné »
crée quand même la recette (texte brut / nom par défaut), simplement sans passage par l'IA.
"""

from __future__ import annotations

from datetime import date

from django.conf import settings
from django.utils import timezone

QUOTA_KEY = 'ai_recipe'
QUOTA_EXCEEDED_CODE = 'ai_quota_exceeded'


def monthly_cap() -> int:
    return max(0, int(getattr(settings, 'AI_RECIPE_MONTHLY_CAP', 30) or 0))


def _month_bounds(now=None):
    local = timezone.localtime(now or timezone.now())
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return start, next_month


def used_this_month(user, now=None) -> int:
    from recipes.models import RecipeImportRequest

    start, _ = _month_bounds(now)
    return (
        RecipeImportRequest.objects.filter(user=user, payload__quota=QUOTA_KEY, created_at__gte=start)
        .exclude(error_message='queue_unavailable')  # jamais parti vers le modèle
        .count()
    )


def has_quota(user, now=None) -> bool:
    cap = monthly_cap()
    return cap == 0 or used_this_month(user, now) < cap


def quota_exceeded_payload(now=None) -> dict:
    _, resets_on = _month_bounds(now)
    cap = monthly_cap()
    return {
        'error': f'Tu as utilisé tes {cap} mises en forme de recette ce mois-ci. Ça repart le 1er du mois prochain.',
        'code': QUOTA_EXCEEDED_CODE,
        'limit': cap,
        'resets_on': resets_on.isoformat(),
    }
