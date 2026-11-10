"""
Aperçus publics pour les liens qui ouvrent l'app (`/api/public/`, .claude-work/specs/deep-links.md §1).

Lus sans compte par le site (pages `tchikook.fr/r/<id>`, `/p/<id>`, `/u/<username>` et leurs balises
Open Graph). Lecture seule, sans authentification (un jeton éventuel est ignoré : le lecteur est
toujours anonyme), débit limité par IP comme l'aperçu d'invitation `GET /api/invites/<code>/` ; le site
relaie l'IP du visiteur avec sa clé `X-Tchikook-Preview-Key` (`core/client_ip.public_rate_limited`).

Visibilité (lecteur anonyme, cf. `recipes/services/post_visibility.py`) :
- recette : seulement `is_public` d'un auteur actif, sinon 404 ; au plus 5 ingrédients, pas d'étapes ;
- post : publié, auteur actif, sinon 404 ; `is_public` → aperçu, sinon teaser `{id, teaser: true}` sans
  contenu (« Ouvre Tchikook pour voir ce plat ») ;
- profil : compte actif et validé, sinon 404 ; pseudo, nom affiché, avatar, nombre de plats cuisinés.
Jamais d'email, de commentaires, de préférences ni de données d'un contenu privé.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.http import Http404
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from accounts.serializers import UserSerializer
from core.client_ip import public_rate_limited
from core.public_links import (
    app_url,
    post_share_url,
    profile_share_url,
    public_display_name,
    recipe_share_url,
)

from .models import Post, Recipe
from .services.cooked_service import cooked_meal_plans, post_display_title

HOUR = 3600
PREVIEW_LIMIT_PER_IP = 60  # par heure, les trois aperçus confondus
MAX_INGREDIENTS = 5
EXCERPT_MAX = 200
# Les crawlers (WhatsApp, iMessage…) gardent l'image : URL pré-signée longue (max S3 SigV4 : 7 jours).
OG_IMAGE_EXPIRES_IN = 6 * 24 * 3600
TEASER_MESSAGE = 'Ouvre Tchikook pour voir ce plat.'


def _too_many():
    return Response(
        {'error': 'Trop de tentatives, réessaie plus tard.', 'code': 'rate_limited'},
        status=status.HTTP_429_TOO_MANY_REQUESTS,
    )


def _limited(request) -> bool:
    return public_rate_limited(request, 'public', limit_per_ip=PREVIEW_LIMIT_PER_IP, window_seconds=HOUR)


def _excerpt(text, limit: int = EXCERPT_MAX) -> str:
    value = ' '.join(str(text or '').split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + '…'


def _image_url(image_path, fallback=None):
    """URL d'image durable pour les aperçus (repli : URL standard du modèle)."""
    if not image_path:
        return fallback
    if str(image_path).startswith('http'):
        return image_path
    try:
        from savr_back.settings import build_presigned_get_url

        return build_presigned_get_url(image_path, expires_in=OG_IMAGE_EXPIRES_IN) or fallback
    except Exception:
        return fallback


def _author(user) -> dict:
    return {
        'id': user.id,
        'username': user.username,
        'display_name': public_display_name(user),
        'avatar_url': UserSerializer(context={}).get_avatar_url(user),
    }


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def public_recipe_view(request, recipe_id):
    if _limited(request):
        return _too_many()
    recipe = (
        Recipe.objects.select_related('created_by')
        .filter(pk=recipe_id, is_public=True, created_by__is_active=True)
        .first()
    )
    if recipe is None:
        raise Http404
    ingredients = list(
        recipe.recipe_ingredients.select_related('ingredient')
        .order_by('id')
        .values_list('ingredient__name', flat=True)[: MAX_INGREDIENTS + 1]
    )
    total = (recipe.prep_time or 0) + (recipe.cook_time or 0)
    return Response(
        {
            'id': recipe.id,
            'title': recipe.title,
            'description': _excerpt(recipe.description),
            'image_url': _image_url(recipe.image_path, fallback=recipe.image_url),
            'author': _author(recipe.created_by),
            'prep_time': recipe.prep_time,
            'cook_time': recipe.cook_time,
            'total_time': total or None,
            'servings': recipe.servings,
            'ingredients': ingredients[:MAX_INGREDIENTS],
            'has_more_ingredients': len(ingredients) > MAX_INGREDIENTS,
            'url': recipe_share_url(recipe.id),
            'app_url': app_url(f'recipe/{recipe.id}'),
        }
    )


def _post_image(post: Post):
    for photo in post.photos.all():
        if photo.image_path and not photo.is_draft:
            return _image_url(photo.image_path, fallback=photo.image_url)
    return None


def _first_public_recipe(post: Post) -> Recipe | None:
    """Recette du post (batch, sinon recettes du repas dans l'ordre) : seulement si elle est publique."""
    candidates = []
    if post.recipe_batch_id and post.recipe_batch:
        candidates.append(post.recipe_batch.recipe)
    if post.meal_plan_id and post.meal_plan:
        mprbs = post.meal_plan.meal_plan_recipe_batches.select_related('recipe_batch__recipe').order_by('order', 'id')
        candidates.extend(m.recipe_batch.recipe for m in mprbs if m.recipe_batch_id)
    return next((r for r in candidates if r is not None and r.is_public), None)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def public_post_view(request, post_id):
    if _limited(request):
        return _too_many()
    post = (
        Post.objects.select_related('user', 'recipe_batch__recipe', 'meal_plan')
        .filter(pk=post_id, is_published=True, user__is_active=True)
        .first()
    )
    if post is None:
        raise Http404
    base = {
        'id': post.id,
        'url': post_share_url(post.id),
        'app_url': app_url(f'post/{post.id}'),
    }
    if not post.is_public:
        # Amis seulement : rien sur le contenu ni l'auteur, juste de quoi proposer d'ouvrir l'app.
        return Response({**base, 'teaser': True, 'message': TEASER_MESSAGE})

    recipe = _first_public_recipe(post)
    public_recipe = None
    if recipe is not None:
        public_recipe = {'id': recipe.id, 'title': recipe.title, 'url': recipe_share_url(recipe.id)}
    return Response(
        {
            **base,
            'teaser': False,
            'title': post_display_title(post) or '',
            'comment': _excerpt(post.comment),
            'image_url': _post_image(post),
            'author': _author(post.user),
            'recipe': public_recipe,
            'created_at': post.created_at,
        }
    )


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
def public_user_view(request, username):
    if _limited(request):
        return _too_many()
    name = str(username or '').strip()
    users = get_user_model().objects.filter(is_active=True, validated_at__isnull=False)
    # Pseudo exact d'abord (unique), sinon insensible à la casse (lien retapé à la main).
    user = users.filter(username=name).first() or users.filter(username__iexact=name).order_by('id').first()
    if user is None:
        raise Http404
    return Response(
        {
            'id': user.id,
            'username': user.username,
            'display_name': public_display_name(user),
            'avatar_url': UserSerializer(context={}).get_avatar_url(user),
            'dishes_count': cooked_meal_plans(user).count(),
            'url': profile_share_url(user.username),
            'app_url': app_url(f'user/{user.id}'),
        }
    )
