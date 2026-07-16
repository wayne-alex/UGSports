from django.urls import path

from client import views
from client.views import service_worker

urlpatterns = [
    path('', views.splash, name='splash'),

    path('dashboard/<int:pk>/', views.client_dashboard, name='client_dashboard'),
    path('dashboard/<int:pk>/standings/', views.client_tournament_standings_poll,
         name='client_tournament_standings_poll'),
    path('dashboard/<int:pk>/fixtures/', views.client_tournament_fixtures_poll, name='client_tournament_fixtures_poll'),
    path('dashboard/<int:pk>/news/', views.client_tournament_news_poll, name='client_tournament_news_poll'),
    path('fixture/<int:pk>/details/', views.fixture_detail, name='fixture_detail'),

    path('lobby/', views.client_lobby, name='client_lobby'),

    path('api/news/<int:post_id>/like/', views.like_news_post, name='api_like_post'),
    path('api/news/<int:post_id>/comment/', views.add_news_comment, name='api_comment_post'),
    path('api/news/<int:post_id>/poll/', views.poll_news_stats, name='api_poll_post'),
    path('api/team/<int:team_id>/detail/', views.team_detail_api, name='api_team_detail'),

    path('phase/<int:phase_pk>/enter/', views.client_phase_detail, name='client_phase_detail'),

    path('service-worker.js', service_worker, name='service_worker'),
    path('offline.html', views.offline_view, name='offline'),
]
