from django.urls import path

from . import views_invites

urlpatterns = [
    path('me/', views_invites.my_invite_view, name='invite_me'),
    path('me/rotate/', views_invites.rotate_invite_view, name='invite_rotate'),
    path('redeem/', views_invites.redeem_invite_view, name='invite_redeem'),
    path('<str:code>/', views_invites.invite_preview_view, name='invite_preview'),
]
