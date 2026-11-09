from django.urls import path

from . import views

urlpatterns = [
    path('', views.tables_view, name='tables'),
    path('<int:table_id>/', views.table_detail_view, name='table_detail'),
    path('<int:table_id>/invite/', views.table_invite_view, name='table_invite'),
    path('<int:table_id>/accept/', views.table_accept_view, name='table_accept'),
    path('<int:table_id>/decline/', views.table_decline_view, name='table_decline'),
    path('<int:table_id>/leave/', views.table_leave_view, name='table_leave'),
    path('<int:table_id>/members/<int:user_id>/', views.table_member_view, name='table_member'),
    path('<int:table_id>/me/', views.table_me_view, name='table_me'),
]
