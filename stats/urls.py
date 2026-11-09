from django.urls import path

from .views import badges_me_view, recap_status_view, recap_view, stats_me_view, stats_user_view

urlpatterns = [
    path('stats/me/', stats_me_view, name='stats_me'),
    path('stats/users/<int:user_id>/', stats_user_view, name='stats_user'),
    path('badges/me/', badges_me_view, name='badges_me'),
    path('recap/<int:year>/', recap_view, name='recap'),
    path('recap/<int:year>/status/', recap_status_view, name='recap_status'),
]
