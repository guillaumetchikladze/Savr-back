"""Aperçus publics des liens partagés (`/api/public/`), cf. `recipes/views_public.py`."""

from django.urls import path

from . import views_public

urlpatterns = [
    path('recipes/<int:recipe_id>/', views_public.public_recipe_view, name='public_recipe'),
    path('posts/<int:post_id>/', views_public.public_post_view, name='public_post'),
    path('users/<str:username>/', views_public.public_user_view, name='public_user'),
]
