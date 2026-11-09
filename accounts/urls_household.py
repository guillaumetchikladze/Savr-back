from django.urls import path

from . import views_household as views

urlpatterns = [
    path('', views.household_view, name='household'),
    path('members/', views.household_members_view, name='household_members'),
    path('members/<int:entry_id>/', views.household_member_detail_view, name='household_member_detail'),
    path('requests/<int:entry_id>/accept/', views.household_request_accept_view, name='household_request_accept'),
    path('requests/<int:entry_id>/decline/', views.household_request_decline_view, name='household_request_decline'),
]
