from django.contrib.auth.views import LogoutView
from django.urls import path

from accounts import views

urlpatterns = [
    path('', views.login_admin, name='login_admin'),
    path('superadmin_dashboard/', views.dashboard_admin, name='dashboard_admin'),

    # Staff Accounts
    path('staff/', views.StaffAccountsView, name='staff_accounts'),
    path('staff/create/', views.StaffCreateView, name='staff_create'),
    path('staff/<int:pk>/edit/', views.StaffUpdateView, name='staff_edit'),
    path('staff/<int:pk>/delete/', views.StaffDeleteView, name='staff_delete'),

    # Sports & Regions
    path('sports-regions/', views.SportsRegionsView, name='sports_regions'),
    path('sports/create/', views.SportCreateView, name='sport_create'),
    path('sports/<int:pk>/edit/', views.SportUpdateView, name='sport_edit'),
    path('subcounty/<int:pk>/edit/', views.subcounty_edit, name='subcounty_edit'),
    path('ward/<int:pk>/edit/', views.ward_edit, name='ward_edit'),

    # Tournament Desk
    path('tournaments/', views.TournamentDeskView, name='tournament_desk'),
    path('tournaments/<int:pk>/', views.TournamentDetailView, name='tournament_detail'),
    path('tournaments/<int:pk>/fixtures/generate/', views.GenerateFixturesView, name='generate_fixtures'),
    path('tournaments/<int:pk>/edit/', views.TournamentEditView, name='tournament_edit'),
    path('fixtures/<int:pk>/edit/', views.edit_fixture_view, name='edit_fixture'),
    path('tournaments/<int:pk>/delete/', views.TournamentDeleteView, name='tournament_delete'),
    path('fixtures/<int:fixture_pk>/result/', views.ResultEntryView, name='result_entry'),

    path('tournaments/<int:tournament_pk>/phases/new/', views.PhaseCreateView, name='phase_create'),
    path('phases/<int:pk>/', views.PhaseDetailView, name='phase_detail'),
    path('phases/<int:phase_pk>/players/new/', views.PlayerCreateView, name='player_create'),
    path('phase-entries/<int:entry_id>/promote/', views.PromoteTeamView, name='promote_team'),
    path('phases/<int:phase_pk>/teams/new/', views.TeamCreateView, name='team_create'),
    path('phases/<int:phase_pk>/teams/select/', views.PhaseTeamSelectView, name='phase_team_select'),
    path('phases/<int:pk>/status/', views.PhaseStatusUpdateView, name='phase_status_update'),
    path('phase/<int:pk>/edit/', views.PhaseEditView, name='phase_edit'),
    path('phase/<int:pk>/delete/', views.PhaseDeleteView, name='phase_delete'),
    path('phase-sports/<int:phase_sport_pk>/next-round/', views.NextRoundBuilderView, name='next_round_builder'),
    path('phase-sports/<int:phase_pk>/fixtures/new/', views.FixtureCreateView, name='fixture_create'),
    # Team
    path('teams/<int:pk>/manage/', views.manage_team, name='manage_team'),

    # Newsroom
    path('newsroom/', views.NewsroomView, name='newsroom'),
    path('newsroom/create/', views.NewsPostCreateView, name='news_create'),
    path('newsroom/<int:pk>/edit/', views.NewsPostUpdateView, name='news_edit'),
    path('newsroom/<int:pk>/delete/', views.NewsPostDeleteView, name='news_delete'),
    path('newsroom/comment/<int:pk>/delete/', views.NewsCommentDeleteView, name='news_comment_delete'),

    # Account
    path('account/settings/', views.account_settings, name='account_settings'),
    path('logout/', LogoutView.as_view(next_page='login_admin'), name='logout'),

    path('auditlogs/', views.AuditLogView, name='audit_log'),

    #     Ward admin
    path('ward/dashboard/', views.ward_dashboard, name='ward_dashboard'),

    path('ward/tournament/', views.ward_tournament, name='ward_tournament'),
    path('ward/teams/<int:pk>/manage/', views.ward_manage_team, name='ward_manage_team'),
    path('ward/fixtures/<int:pk>/edit/', views.ward_edit_fixture_view, name='ward_edit_fixture'),
    path('ward/fixtures/<int:fixture_pk>/result/', views.ward_ResultEntryView, name='ward_result_entry'),
    path('ward/fixtures/<int:pk>/edit/', views.ward_edit_fixture_view, name='ward_edit_fixture'),
    path('ward/phases/<int:phase_pk>/teams/new/', views.ward_TeamCreateView, name='ward_team_create'),
    path('ward/account/settings/', views.ward_account_settings, name='ward_account_settings'),
    path('ward/phases/<int:pk>/generate-fixtures/', views.WardGenerateFixturesView, name='ward_generate_fixtures'),
    path('ward/phase-sports/<int:pk>/next-round/', views.WardNextRoundBuilderView, name='ward_next_round_builder'),

    path('ward/newsroom/', views.ward_newsroom, name='ward_newsroom'),
    path('ward/news/create/', views.news_create, name='ward_news_create'),
    path('ward/news/<int:pk>/edit/', views.news_edit, name='ward_news_edit'),
    path('ward/news/<int:pk>/delete/', views.news_delete, name='ward_news_delete'),
    path('ward/comment/<int:pk>/delete/', views.news_comment_delete, name='news_comment_delete'),
    #     Sub County
    path('subcounty/dashboard/', views.subcounty_dashboard, name='subcounty_dashboard'),
    path('subcounty/tournaments/', views.subcounty_tournament, name='subcounty_tournament'),
    path('subcounty/tournaments/<int:tournament_id>/', views.subcounty_tournament_detail,
         name='subcounty_tournament_detail'),
    path('subcounty/newsroom/', views.subcounty_newsroom, name='subcounty_newsroom'),
    path('subcounty/news/create/', views.subcounty_news_create, name='subcounty_news_create'),
    path('subcounty/news/<int:pk>/edit/', views.subcountynews_edit, name='subcounty_news_edit'),
    path('subcounty/news/<int:pk>/delete/', views.subcountynews_delete, name='subcounty_news_delete'),
    path('subcounty/comment/<int:pk>/delete/', views.subcounty_news_comment_delete,
         name='subcounty_news_comment_delete'),
    path('subcounty/settings/', views.subcounty_account_settings, name='subcounty_account_settings'),
    path('subcounty/phases/<int:pk>/', views.subcounty_phase_detail, name='subcounty_phase_detail'),
    path('subcounty/phases/<int:phase_id>/add-fixture/', views.subcounty_add_fixture, name='subcounty_add_fixture'),
    path('subcounty/phases/<int:pk>/generate-fixtures/', views.SubcountyGenerateFixturesView,
         name='subcounty_generate_fixtures'),
    path('subcounty/phase-sports/<int:phase_sport_pk>/next-round/', views.SubcountyNextRoundBuilderView,
         name='subcounty_next_round_builder'),
    path('subcounty/phases/<int:pk>/status/', views.subcounty_phase_status_update,
         name='subcounty_phase_status_update'),
    path('subcounty/phases/<int:phase_pk>/teams/create/', views.subcounty_team_create, name='subcounty_team_create'),
    path('subcounty/teams/<int:pk>/manage/', views.subcounty_manage_team, name='subcounty_manage_team'),
    path('subcounty/phases/<int:phase_pk>/teams/select/', views.SubcountyPhaseTeamSelectView,
         name='subcounty_phase_team_select'),
    path('subconty/phase-entries/<int:entry_id>/promote/', views.SubcountyPromoteTeamView,
         name='subconty_promote_team'),
    path('subcounty/fixtures/<int:fixture_pk>/result/', views.SubcountyResultEntryView, name='subcounty_result_entry'),
    path('subcounty/fixtures/<int:pk>/edit/', views.Subcountyedit_fixture_view, name='subcounty_edit_fixture'),

]
