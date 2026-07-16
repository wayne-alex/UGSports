import csv
import io
from datetime import timedelta
from io import StringIO

from django.contrib import messages
from django.contrib.auth import get_user_model, authenticate, login, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, Count
from django.forms.models import model_to_dict
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.decorators import superadmin_admin_required, ward_admin_required, subcounty_admin_required
from accounts.fixture_generator import FixtureGenerator
from accounts.forms import TournamentForm, PlayerForm, TeamForm, PhaseForm, FixtureGenerationForm, FixtureEditForm, \
    ProfileForm, WardAdminProfileForm, FixtureForm, WardTournamentForm, SubcountyTeamForm, SubcountyTournamentForm
from accounts.models import AuditLog, Sport, SubCounty, Ward, NewsPost, NewsComment, Tournament, Team, Player, Result, \
    Phase, PhaseEntry, Fixture, PhaseSport, Goal, User, Card
from accounts.services import resolve_next_round, create_next_round_fixtures


# HELPER FUNCTIONS
def get_client_ip(request):
    """Utility to get the real IP address from the request."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


def log_audit_action(user, action, obj=None, changes=None, request=None):
    """
    Helper method to create an AuditLog entry.
    """
    content_type = None
    object_id = None
    object_repr = ""

    if obj:
        content_type = ContentType.objects.get_for_model(obj)
        object_id = obj.pk
        object_repr = str(obj)[:255]  # Ensure it fits in max_length

    ip_address = get_client_ip(request) if request else None

    # Guarantee user fallback for public actions or anonymous updates
    acting_user = user if (user and user.is_authenticated) else None

    AuditLog.objects.create(
        user=acting_user,
        action=action,
        content_type=content_type,
        object_id=object_id,
        object_repr=object_repr,
        changes=changes,
        ip_address=ip_address
    )


def calculate_model_changes(old_instance, new_instance, exclude_fields=None):
    """Utility to track field shifts during edits."""
    if not exclude_fields:
        exclude_fields = []

    changes = {}
    old_data = model_to_dict(old_instance)
    new_data = model_to_dict(new_instance)

    for field, new_val in new_data.items():
        if field in exclude_fields:
            continue
        old_val = old_data.get(field)
        if old_val != new_val:
            changes[field] = [str(old_val), str(new_val)]

    return changes if changes else None


def _role_landing_url(user):
    if user.role in (User.Role.SYSTEM_ADMIN, User.Role.COUNTY_ICT_OFFICER):
        return reverse('dashboard_admin')
    if user.role == User.Role.WARD_ADMIN:
        return reverse('ward_dashboard')
    if user.role == User.Role.SUB_COUNTY_ADMIN:
        return reverse('subcounty_dashboard')
    return reverse('dashboard_admin')


User = get_user_model()


def login_admin(request):
    if request.user.is_authenticated:
        return redirect(_role_landing_url(request.user))

    if request.method == 'POST':
        email = request.POST.get('email')
        password = request.POST.get('password')
        remember = request.POST.get('remember') == 'true'

        try:
            user_obj = User.objects.get(email=email)
            username = user_obj.username
        except User.DoesNotExist:
            username = email

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            request.session.set_expiry(1209600 if remember else 0)
            log_audit_action(user, AuditLog.Action.LOGIN, request=request)

            next_url = request.GET.get('next')
            if next_url and url_has_allowed_host_and_scheme(
                    next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                return redirect(next_url)
            return redirect(_role_landing_url(user))
        else:
            context = {'form': {'errors': True}}
            return render(request, 'admin_login.html', context)

    return render(request, 'admin_login.html')


@superadmin_admin_required
@login_required(login_url='login_admin')
def dashboard_admin(request):
    total_tournaments_count = Tournament.objects.count()
    active_tournaments_count = Tournament.objects.filter(status=Tournament.Status.ONGOING).count()
    team_count = Team.objects.count()
    sport_count = Sport.objects.filter(is_active=True).count()
    player_count = Player.objects.count()
    pending_results_count = Result.objects.filter(verified=False).count()
    sub_county_count = SubCounty.objects.count()
    ward_count = Ward.objects.count()
    staff_count = User.objects.exclude(role=User.Role.SYSTEM_ADMIN).count()

    stage_breakdown = [
        {
            'code': stage_code,
            'label': stage_label,
            'phase_count': Phase.objects.filter(stage=stage_code).count(),
            'ongoing_count': Phase.objects.filter(
                stage=stage_code, status=Phase.Status.ONGOING
            ).count(),
        }
        for stage_code, stage_label in Phase.Stage.choices
    ]

    recent_audit_logs = AuditLog.objects.select_related('user').order_by('-timestamp')[:8]

    context = {
        'active_nav': 'dashboard',
        'total_tournaments_count': total_tournaments_count,
        'active_tournaments_count': active_tournaments_count,
        'team_count': team_count,
        'sport_count': sport_count,
        'player_count': player_count,
        'pending_results_count': pending_results_count,
        'sub_county_count': sub_county_count,
        'ward_count': ward_count,
        'staff_count': staff_count,
        'stage_breakdown': stage_breakdown,
        'recent_audit_logs': recent_audit_logs,
    }
    return render(request, 'superadmin_dashboard.html', context)


@superadmin_admin_required
@login_required
def StaffAccountsView(request):
    staff_list = User.objects.all().order_by('-date_joined')

    paginator = Paginator(staff_list, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    role_choices = User._meta.get_field('role').choices if hasattr(User, 'role') else []

    context = {
        'staff_members': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'role_choices': role_choices,
        'sub_counties': SubCounty.objects.all(),
        'wards': Ward.objects.select_related('sub_county').all(),
    }
    return render(request, 'superadmin_staff_accounts.html', context)


@superadmin_admin_required
@login_required
def StaffCreateView(request):
    if request.method == 'POST':
        email = request.POST.get('email')
        try:
            user = User.objects.create(
                first_name=request.POST.get('first_name'),
                last_name=request.POST.get('last_name'),
                email=email,
                username=email,
                phone_number=request.POST.get('phone_number'),
                role=request.POST.get('role'),
                sub_county_id=request.POST.get('sub_county') or None,
                ward_id=request.POST.get('ward') or None,
                is_staff=False
            )
            password = request.POST.get('password'),
            if password:
                user.set_password(password)
            else:
                user.set_password('UGSports2026!')
            user.save()

            log_audit_action(request.user, AuditLog.Action.CREATE, user, request=request)
            messages.success(request, f"Staff account for {user.get_full_name()} created successfully.")

        except IntegrityError:
            messages.error(request, "A staff member with this email already exists.")

    return redirect('staff_accounts')


@superadmin_admin_required
@login_required
def StaffUpdateView(request, pk):
    user = get_object_or_404(User, pk=pk)

    if request.method == 'POST':
        import copy
        old_user = copy.deepcopy(user)

        user.first_name = request.POST.get('first_name')
        user.last_name = request.POST.get('last_name')

        new_email = request.POST.get('email')
        if new_email != user.email and User.objects.filter(email=new_email).exists():
            messages.error(request, "That email is already in use by another account.")
            return redirect('staff_accounts')

        user.email = new_email
        user.username = new_email
        user.phone_number = request.POST.get('phone_number')

        role = request.POST.get('role')
        user.role = role

        if role == 'sub_county_admin':
            user.sub_county_id = request.POST.get('sub_county') or None
            user.ward = None
        elif role == 'ward_admin':
            user.sub_county = None
            user.ward_id = request.POST.get('ward') or None
        else:
            user.sub_county = None
            user.ward = None

        user.save()

        changes = calculate_model_changes(old_user, user, exclude_fields=['password'])
        log_audit_action(request.user, AuditLog.Action.UPDATE, user, changes=changes, request=request)
        messages.success(request, f"Staff account for {user.get_full_name()} updated successfully.")

    return redirect('staff_accounts')


@superadmin_admin_required
@login_required
def StaffDeleteView(request, pk):
    if request.method == 'POST':
        target_user = get_object_or_404(User, pk=pk)

        if target_user.pk == request.user.pk:
            messages.error(request, "Action denied: You cannot delete your own account.")
            return redirect('staff_accounts')

        user_name = target_user.get_full_name()

        log_audit_action(request.user, AuditLog.Action.DELETE, target_user, request=request)
        target_user.delete()
        messages.success(request, f"Staff account for {user_name} has been removed.")

    return redirect('staff_accounts')


@superadmin_admin_required
@login_required
def SportsRegionsView(request):
    if request.method == 'POST':
        try:
            if 'rules_summary' in request.POST:
                sport = Sport.objects.create(
                    name=request.POST.get('name'),
                    rules_summary=request.POST.get('rules_summary'),
                    players_per_side=request.POST.get('players_per_side'),
                    is_active=request.POST.get('is_active') == 'true'
                )
                log_audit_action(request.user, AuditLog.Action.CREATE, sport, request=request)
                messages.success(request, f"Sport '{sport.name}' added successfully.")

            elif 'sub_county' in request.POST:
                sub_county = get_object_or_404(SubCounty, pk=request.POST.get('sub_county'))
                ward = Ward.objects.create(
                    name=request.POST.get('name'),
                    sub_county=sub_county
                )
                log_audit_action(request.user, AuditLog.Action.CREATE, ward, request=request)
                messages.success(request, f"Ward '{ward.name}' added successfully.")

            elif 'name' in request.POST:
                sub_county = SubCounty.objects.create(
                    name=request.POST.get('name')
                )
                log_audit_action(request.user, AuditLog.Action.CREATE, sub_county, request=request)
                messages.success(request, f"Sub-County '{sub_county.name}' added successfully.")

        except IntegrityError:
            messages.error(request, "Error: A record with that exact name already exists.")

        return redirect('sports_regions')

    context = {
        'sports': Sport.objects.all(),
        'sub_counties': SubCounty.objects.prefetch_related('wards', 'admins').all(),
        'wards': Ward.objects.select_related('sub_county').prefetch_related('teams').all(),
    }
    return render(request, 'superadmin_sports_regions.html', context)


@superadmin_admin_required
@login_required
def sport_edit(request, pk):
    sport = get_object_or_404(Sport, pk=pk)
    if request.method == 'POST':
        import copy
        old_sport = copy.deepcopy(sport)

        sport.name = request.POST.get('name')
        sport.rules_summary = request.POST.get('rules_summary')
        sport.players_per_side = request.POST.get('players_per_side')
        sport.is_active = request.POST.get('is_active') == 'true'
        sport.save()

        changes = calculate_model_changes(old_sport, sport)
        log_audit_action(request.user, AuditLog.Action.UPDATE, sport, changes=changes, request=request)
        messages.success(request, f"Sport '{sport.name}' updated successfully.")
    return redirect('sports_regions')


@superadmin_admin_required
@login_required(login_url='login_admin')
def subcounty_edit(request, pk):
    sc = get_object_or_404(SubCounty, pk=pk)
    if request.method == 'POST':
        import copy
        old_sc = copy.deepcopy(sc)

        sc.name = request.POST.get('name')
        sc.save()

        changes = calculate_model_changes(old_sc, sc)
        log_audit_action(request.user, AuditLog.Action.UPDATE, sc, changes=changes, request=request)
        messages.success(request, f"Sub-County updated to '{sc.name}'.")
    return redirect('sports_regions')


@superadmin_admin_required
@login_required
def ward_edit(request, pk):
    ward = get_object_or_404(Ward, pk=pk)
    if request.method == 'POST':
        import copy
        old_ward = copy.deepcopy(ward)

        sub_county = get_object_or_404(SubCounty, pk=request.POST.get('sub_county'))
        ward.name = request.POST.get('name')
        ward.sub_county = sub_county
        ward.save()

        changes = calculate_model_changes(old_ward, ward)
        log_audit_action(request.user, AuditLog.Action.UPDATE, ward, changes=changes, request=request)
        messages.success(request, f"Ward '{ward.name}' updated successfully.")
    return redirect('sports_regions')


@superadmin_admin_required
@login_required
def SportCreateView(request):
    if request.method == 'POST':
        sport = Sport.objects.create(
            name=request.POST.get('name'),
            rules_summary=request.POST.get('rules_summary'),
            players_per_side=request.POST.get('players_per_side'),
            is_active=request.POST.get('is_active') == 'true'
        )
        log_audit_action(request.user, AuditLog.Action.CREATE, sport, request=request)
        messages.success(request, f"Sport '{sport.name}' created.")
    return redirect('sports_regions')


@superadmin_admin_required
@login_required(login_url='login_admin')
def SportUpdateView(request, pk):
    sport = get_object_or_404(Sport, pk=pk)
    if request.method == 'POST':
        import copy
        old_sport = copy.deepcopy(sport)

        sport.name = request.POST.get('name')
        sport.rules_summary = request.POST.get('rules_summary')
        sport.players_per_side = request.POST.get('players_per_side')
        sport.is_active = request.POST.get('is_active') == 'true'
        sport.save()

        changes = calculate_model_changes(old_sport, sport)
        log_audit_action(request.user, AuditLog.Action.UPDATE, sport, changes=changes, request=request)
        messages.success(request, f"Sport '{sport.name}' updated successfully.")
    return redirect('sports_regions')


@superadmin_admin_required
@login_required(login_url='login_admin')
def TournamentDeskView(request):
    if request.method == 'POST':
        form = TournamentForm(request.POST)
        if form.is_valid():
            tournament = form.save(commit=False)
            tournament.created_by = request.user
            tournament.save()
            form.save_m2m()
            log_audit_action(request.user, AuditLog.Action.CREATE, tournament, request=request)
            messages.success(request, f'"{tournament.name}" was created.')
            return redirect('tournament_detail', pk=tournament.pk)
        else:
            print('Form errors:', form.errors.as_json())
    else:
        form = TournamentForm()

    tournaments = (
        Tournament.objects
        .prefetch_related('sports', 'phases')
        .annotate(phase_count=Count('phases', distinct=True))
        .order_by('-season', 'name')
    )

    wards = list(Ward.objects.values('id', 'name', 'sub_county_id'))

    context = {
        'active_nav': 'tournaments',
        'tournaments': tournaments,
        'form': form,
        'open_form_panel': request.method == 'POST',
        'wards_json': wards,
    }
    return render(request, 'superadmin_tournament_desk.html', context)


@superadmin_admin_required
@login_required(login_url='login_admin')
def TournamentEditView(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk)
    if request.method == 'POST':
        import copy
        old_tournament = copy.deepcopy(tournament)

        form = TournamentForm(request.POST, instance=tournament)
        if form.is_valid():
            form.save()

            changes = calculate_model_changes(old_tournament, tournament)
            log_audit_action(request.user, AuditLog.Action.UPDATE, tournament, changes=changes, request=request)
            messages.success(request, f'"{tournament.name}" was updated.')
            return redirect('tournament_detail', pk=tournament.pk)
    else:
        form = TournamentForm(instance=tournament)

    return render(request, 'superadmin_tournament_edit.html', {
        'form': form,
        'tournament': tournament,
        'active_nav': 'tournaments',
    })


@superadmin_admin_required
@login_required(login_url='login_admin')
@require_POST
def TournamentDeleteView(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk)
    name = tournament.name
    try:
        log_audit_action(request.user, AuditLog.Action.DELETE, tournament, request=request)
        tournament.delete()
        messages.success(request, f'"{name}" was deleted, including all its phases.')
    except ProtectedError:
        messages.error(
            request,
            f'"{name}" can\'t be deleted — protected records dependencies exist.'
        )
    return redirect('tournament_desk')


@superadmin_admin_required
@login_required(login_url='login_admin')
def TournamentDetailView(request, pk):
    tournament = get_object_or_404(
        Tournament.objects.prefetch_related('sports'),
        pk=pk
    )

    phases = (
        Phase.objects
        .filter(tournament=tournament)
        .select_related('ward', 'sub_county')
        .annotate(
            team_count=Count('entries', distinct=True),
            fixture_count=Count('phase_sports__fixtures', distinct=True),
        )
        .order_by('order', 'ward__name', 'sub_county__name')
    )

    context = {
        'active_nav': 'tournaments',
        'tournament': tournament,
        'ward_phases': phases.filter(stage=Phase.Stage.WARD),
        'sub_county_phases': phases.filter(stage=Phase.Stage.SUB_COUNTY),
        'county_phases': phases.filter(stage=Phase.Stage.COUNTY),
        'final_phases': phases.filter(stage=Phase.Stage.FINAL),
        'phase_count': phases.count(),
    }
    return render(request, 'superadmin_tournament_detail.html', context)


@superadmin_admin_required
@login_required(login_url='login_admin')
def PhaseCreateView(request, tournament_pk):
    tournament = get_object_or_404(Tournament, pk=tournament_pk)
    initial = {'stage': request.GET.get('stage', Phase.Stage.WARD)}

    if request.method == 'POST':
        form = PhaseForm(request.POST, tournament=tournament)
        if form.is_valid():
            phase = form.save(commit=False)
            phase.tournament = tournament
            phase.created_by = request.user
            phase.save()
            for sport in tournament.sports.all():
                PhaseSport.objects.get_or_create(phase=phase, sport=sport)

            log_audit_action(request.user, AuditLog.Action.CREATE, phase, request=request)
            messages.success(request, f'{phase} was created.')
            return redirect('phase_detail', pk=phase.pk)
    else:
        form = PhaseForm(initial=initial, tournament=tournament)

    return render(request, 'superadmin_phase_form.html', {
        'form': form,
        'tournament': tournament,
        'active_nav': 'tournaments',
    })


from collections import defaultdict


@superadmin_admin_required
@login_required(login_url='login_admin')
def PhaseDetailView(request, pk):
    phase = get_object_or_404(
        Phase.objects.select_related('tournament', 'ward', 'sub_county'),
        pk=pk
    )

    tournament_sports = phase.tournament.sports.filter(is_active=True).order_by('name')
    existing_phase_sports = {
        ps.sport_id: ps
        for ps in PhaseSport.objects.filter(phase=phase).select_related('sport')
    }
    sport_rows = [
        {'sport': sport, 'phase_sport': existing_phase_sports.get(sport.id)}
        for sport in tournament_sports
    ]

    entries = (
        PhaseEntry.objects.filter(phase=phase)
        .select_related('team', 'team__ward', 'team__sport', 'promoted_from')
        .order_by('team__name')
    )
    teams = Team.objects.filter(phase_entries__phase=phase).distinct().order_by('name')
    players = (
        Player.objects.filter(team__phase_entries__phase=phase, tournament=phase.tournament)
        .select_related('team')
        .order_by('team__name', 'jersey_number')
    )

    fixtures = (
        Fixture.objects.filter(phase_sport__phase=phase)
        .select_related('home_team', 'away_team', 'phase_sport__sport', 'group', 'result')
        .order_by('round_number', 'kickoff_at')
    )
    postponed_fixtures = fixtures.filter(status=Fixture.Status.POSTPONED)
    active_fixtures = fixtures.exclude(status=Fixture.Status.POSTPONED)

    # Key by sport_id, not phase_sport_id — sport is always present, phase_sport may not be
    fixtures_by_sport = defaultdict(list)
    for f in active_fixtures:
        fixtures_by_sport[f.phase_sport.sport_id].append(f)

    fixture_dates = sorted({f.kickoff_at.date() for f in active_fixtures if f.kickoff_at})
    has_unscheduled = active_fixtures.filter(kickoff_at__isnull=True).exists()

    results = (
        Result.objects.filter(fixture__phase_sport__phase=phase)
        .select_related('fixture__home_team', 'fixture__away_team', 'verified_by')
        .order_by('-entered_at')
    )

    context = {
        'active_nav': 'tournaments',
        'phase': phase,
        'tournament': phase.tournament,
        'sport_rows': sport_rows,
        'fixtures_by_sport': dict(fixtures_by_sport),
        'entries': entries,
        'teams': teams,
        'players': players,
        'fixtures': active_fixtures,
        'postponed_fixtures': postponed_fixtures,
        'fixture_dates': fixture_dates,
        'has_unscheduled': has_unscheduled,
        'results': results,
        'can_promote': phase.next_phase_lookup is not None,
    }
    return render(request, 'superadmin_phase_detail.html', context)


@superadmin_admin_required
@login_required(login_url='login_admin')
@require_POST
def PromoteTeamView(request, entry_id):
    entry = get_object_or_404(PhaseEntry.objects.select_related('phase__tournament', 'team'), pk=entry_id)
    lookup = entry.phase.next_phase_lookup

    if lookup is None:
        messages.error(request, f'{entry.team.name} is already at the final stage.')
        return redirect('phase_detail', pk=entry.phase.pk)

    try:
        next_phase = Phase.objects.get(tournament=entry.phase.tournament, **lookup)
    except Phase.DoesNotExist:
        messages.error(request, f'No matching phase exists yet for this tournament.')
        return redirect('phase_detail', pk=entry.phase.pk)

    new_entry, created = PhaseEntry.objects.get_or_create(
        phase=next_phase, team=entry.team,
        defaults={'promoted_from': entry, 'registered_by': request.user},
    )
    if created:
        log_audit_action(
            request.user,
            AuditLog.Action.PROMOTE,
            obj=entry.team,
            changes={"from_phase": str(entry.phase), "to_phase": str(next_phase)},
            request=request
        )
        messages.success(request, f'{entry.team.name} promoted to {next_phase}.')
    else:
        messages.info(request, f'{entry.team.name} is already in {next_phase}.')

    return redirect('phase_detail', pk=next_phase.pk)


@superadmin_admin_required
@login_required(login_url='login_admin')
def TeamCreateView(request, phase_pk):
    phase = get_object_or_404(Phase, pk=phase_pk)

    if phase.stage != Phase.Stage.WARD:
        messages.error(
            request,
            'New teams can only be created at Ward level. '
            'For this phase, use "Select Teams" to bring up qualified teams instead.'
        )
        return redirect('phase_detail', pk=phase.pk)

    if request.method == 'POST':
        # 1. Handle CSV Import
        if 'csv_file' in request.FILES:
            csv_file = request.FILES['csv_file']
            data = csv_file.read().decode('utf-8')
            io_string = io.StringIO(data)
            reader = csv.DictReader(io_string)

            for row in reader:
                # Expecting columns: name, sport_id, home_ground, coach_name, coach_phone
                team = Team.objects.create(
                    name=row['name'],
                    sport_id=row['sport_id'],
                    ward=phase.ward,
                    home_ground=row.get('home_ground', ''),
                    coach_name=row.get('coach_name', ''),
                    coach_phone=row.get('coach_phone', ''),
                    created_by=request.user
                )
                log_audit_action(request.user, AuditLog.Action.CREATE, team, request=request)
                phase_entry = PhaseEntry.objects.create(
                    phase=phase,
                    team=team,
                    registered_by=request.user
                )
                log_audit_action(request.user, AuditLog.Action.CREATE, phase_entry, request=request)
            messages.success(request, "Bulk teams imported successfully.")
            return redirect('phase_detail', pk=phase.pk)

        # 2. Handle Single Form
        form = TeamForm(request.POST)
        # Manually inject the ward since it's disabled in the form
        team = form.save(commit=False)
        team.ward = phase.ward
        team.created_by = request.user
        team.save()
        log_audit_action(request.user, AuditLog.Action.CREATE, team, request=request)
        phase_entry = PhaseEntry.objects.create(phase=phase, team=team, registered_by=request.user)
        log_audit_action(request.user, AuditLog.Action.CREATE, phase_entry, request=request)
        return redirect('phase_detail',pk=phase.pk)

    else:
        form = TeamForm(initial={'ward': phase.ward})

    return render(request, 'superadmin_team_form.html', {'form': form, 'phase': phase})


@superadmin_admin_required
@login_required(login_url='login_admin')
def PhaseTeamSelectView(request, phase_pk):
    """For Sub-County / County / Final phases: pick from teams already qualified
    at the level below, instead of creating a new team."""
    phase = get_object_or_404(Phase, pk=phase_pk)

    if phase.stage == Phase.Stage.WARD:
        messages.error(request, 'Ward phases create teams directly — use "Add Team" instead.')
        return redirect('phase_detail', pk=phase.pk)

    feeder_entries = (
        PhaseEntry.objects
        .filter(phase__in=phase.feeder_phases)
        .exclude(team__phase_entries__phase=phase)  # already entered here
        .select_related('team', 'team__ward', 'phase')
        .order_by('team__name')
    )

    if request.method == 'POST':
        team_ids = request.POST.getlist('team_ids')
        if not team_ids:
            messages.error(request, 'Select at least one team to advance.')
            return redirect('phase_team_select', phase_pk=phase.pk)

        created = 0
        for team_id in team_ids:
            source_entry = feeder_entries.filter(team_id=team_id).first()
            if not source_entry:
                continue
            phase_entry, was_created = PhaseEntry.objects.get_or_create(
                phase=phase, team_id=team_id,
                defaults={'promoted_from': source_entry, 'registered_by': request.user},
            )
            if was_created:
                log_audit_action(request.user, AuditLog.Action.CREATE, phase_entry, request=request)
                created += 1

        messages.success(request, f'{created} team{"s" if created != 1 else ""} advanced into {phase}.')
        return redirect('phase_detail', pk=phase.pk)

    return render(request, 'superadmin_team_select.html', {
        'phase': phase,
        'feeder_entries': feeder_entries,
        'active_nav': 'tournaments',
    })


@superadmin_admin_required
@login_required(login_url='login_admin')
@require_POST
def PhaseStatusUpdateView(request, pk):
    phase = get_object_or_404(Phase, pk=pk)
    new_status = request.POST.get('status')
    if new_status in Phase.Status.values:
        phase.status = new_status
        phase.save(update_fields=['status'])
        log_audit_action(request.user, AuditLog.Action.UPDATE, phase, request=request)
        messages.success(request, f'{phase} marked as {phase.get_status_display()}.')
    return redirect('phase_detail', pk=phase.pk)


@superadmin_admin_required
@login_required(login_url='login_admin')
def PlayerCreateView(request, phase_pk):
    phase = get_object_or_404(Phase, pk=phase_pk)
    if request.method == 'POST':
        form = PlayerForm(request.POST, tournament=phase.tournament)
        if form.is_valid():
            player = form.save(commit=False)
            player.tournament = phase.tournament
            player.created_by = request.user
            player.save()

            log_audit_action(request.user, AuditLog.Action.CREATE, player, request=request)
            messages.success(request, f'{player.name} was registered to {player.team.name}.')
            return redirect('phase_detail', pk=phase.pk)
    else:
        form = PlayerForm(tournament=phase.tournament)

    return render(request, 'superadmin_player_form.html', {
        'form': form, 'phase': phase, 'active_nav': 'tournaments',
    })


@superadmin_admin_required
@login_required(login_url='login_admin')
def PhaseDeleteView(request, pk):
    phase = get_object_or_404(Phase, pk=pk)
    tournament_pk = phase.tournament.pk

    if request.method == 'POST':  # Best practice: use a POST request for deletion
        log_audit_action(request.user, AuditLog.Action.DELETE, phase, request=request)
        phase.delete()
        messages.success(request, "Phase deleted successfully.")
        return redirect('tournament_detail', pk=tournament_pk)

    # If you want to use a simple link, you can allow GET deletion (but POST is safer)
    log_audit_action(request.user, AuditLog.Action.DELETE, phase, request=request)
    phase.delete()
    messages.success(request, "Phase deleted successfully.")
    return redirect('tournament_detail', pk=tournament_pk)


@superadmin_admin_required
@login_required(login_url='login_admin')
def PhaseEditView(request, pk):
    phase = get_object_or_404(Phase, pk=pk)

    if request.method == 'POST':
        form = PhaseForm(request.POST, instance=phase)
        if form.is_valid():
            form.save()
            log_audit_action(request.user, AuditLog.Action.UPDATE, phase, request=request)
        messages.success(request, f"Phase updated successfully.")
        log_audit_action(request.user, AuditLog.Action.UPDATE, phase, request=request)
        return redirect('tournament_detail', pk=phase.tournament.pk)

    else:
        form = PhaseForm(instance=phase)

        return render(request, 'superadmin_phase_form.html', {
            'form': form,
            'phase': phase,
            'tournament': phase.tournament,
            'active_nav': 'tournaments'
        })


@superadmin_admin_required
@login_required(login_url='login_admin')
def GenerateFixturesView(request, pk):
    phase = get_object_or_404(Phase, pk=pk)

    if request.method == 'POST':
        form = FixtureGenerationForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            sport = data['sport']

            phase_sport, _ = PhaseSport.objects.get_or_create(
                phase=phase,
                sport=sport,
                defaults={
                    'fixture_format': data['format'],
                    'legs': data.get('legs', 1),
                }
            )
            # Keep format/legs in sync if this phase_sport already existed
            # from creation but is being (re)generated with new settings
            phase_sport.fixture_format = data['format']
            phase_sport.legs = data.get('legs', 1)
            phase_sport.save(update_fields=['fixture_format', 'legs'])
            for key, value in data.items():
                print(f"{key}: {value}")

            try:
                generator = FixtureGenerator(phase_sport, config={
                    'start_date': data.get('start_date') and data['start_date'].isoformat(),
                    'duration': data.get('duration'),
                    'schedule_type': data.get('schedule_type', 'daily'),
                    'groups': data.get('groups', 2),
                    'max_matches_per_day': data.get('max_matches_per_day') or None,
                })
                generator.generate()
                log_audit_action(request.user, AuditLog.Action.GENERATE_FIXTURES, phase_sport, request=request)
                messages.success(request, f'Fixtures for {phase_sport.sport.name} generated successfully.')
                phase_sport.fixtures_generated = True
                phase_sport.save(update_fields=['fixture_format', 'legs', 'fixtures_generated'])
            except ValidationError as e:
                messages.error(request, str(e))

        return redirect('phase_detail', pk=phase.pk)

    else:
        form = FixtureGenerationForm()

    return render(request, 'superadmin_generate_fixtures.html', {'form': form, 'phase': phase})


@superadmin_admin_required
@login_required(login_url='login_admin')
def edit_fixture_view(request, pk):
    fixture = get_object_or_404(Fixture, pk=pk)

    if request.method == 'POST':
        form = FixtureEditForm(request.POST, instance=fixture)
        if form.is_valid():
            form.save()
            log_audit_action(request.user, AuditLog.Action.UPDATE, fixture, request=request)
            return redirect('phase_detail', fixture.phase_sport.pk)
    else:
        form = FixtureEditForm(instance=fixture)

    return render(request, 'superadmin_edit_fixture.html', {'form': form, 'fixture': fixture})


@superadmin_admin_required
@login_required
def NewsroomView(request):
    # Fetch phases instead of sub-counties
    # Prefetch related ward/sub_county so the scope label renders efficiently
    phases = Phase.objects.select_related('ward', 'sub_county', 'tournament').order_by('-created_at')

    news_list = NewsPost.objects.select_related('author', 'phase', 'sub_county') \
        .prefetch_related('comments', 'comments__author') \
        .order_by('-published_at')

    paginator = Paginator(news_list, 12)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    liked_posts = request.session.get('liked_posts', [])

    context = {
        'posts': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'phases': phases,  # <-- Pass phases to the template
        'liked_posts': liked_posts,
    }
    return render(request, 'superadmin_newsroom.html', context)


@superadmin_admin_required
@login_required
def NewsPostCreateView(request):
    if request.method == 'POST':
        title = request.POST.get('title')
        tag = request.POST.get('tag')
        body = request.POST.get('body')
        phase_id = request.POST.get('phase') or None  # <-- Get Phase

        post = NewsPost.objects.create(
            title=title,
            tag=tag,
            body=body,
            phase_id=phase_id,
            author=request.user
        )

        log_audit_action(request.user, AuditLog.Action.CREATE, post, request=request)
        messages.success(request, "News post published successfully.")

    return redirect('newsroom')


@superadmin_admin_required
@login_required
def NewsPostUpdateView(request, pk):
    post = get_object_or_404(NewsPost, pk=pk)

    if request.method == 'POST':
        import copy
        old_post = copy.deepcopy(post)

        post.title = request.POST.get('title')
        post.tag = request.POST.get('tag')
        post.body = request.POST.get('body')
        post.phase_id = request.POST.get('phase') or None  # <-- Get Phase
        post.save()

        changes = calculate_model_changes(old_post, post)
        log_audit_action(request.user, AuditLog.Action.UPDATE, post, changes=changes, request=request)
        messages.success(request, "News post updated successfully.")

    return redirect('newsroom')


@superadmin_admin_required
@login_required
def NewsPostDeleteView(request, pk):
    if request.method == 'POST':
        post = get_object_or_404(NewsPost, pk=pk)
        title = post.title

        log_audit_action(request.user, AuditLog.Action.DELETE, post, request=request)
        post.delete()
        messages.success(request, f"Post '{title}' has been deleted.")

    return redirect('newsroom')


@superadmin_admin_required
def NewsPostLikeView(request, pk):
    if request.method == 'POST':
        post = get_object_or_404(NewsPost, pk=pk)
        liked_posts = request.session.get('liked_posts', [])

        if pk in liked_posts:
            post.like_count = max(0, post.like_count - 1)
            liked_posts.remove(pk)
            liked = False
            log_audit_action(request.user, AuditLog.Action.UPDATE, post, changes={"like": ["liked", "unliked"]},
                             request=request)
        else:
            post.like_count += 1
            liked_posts.append(pk)
            liked = True
            log_audit_action(request.user, AuditLog.Action.UPDATE, post, changes={"like": ["unliked", "liked"]},
                             request=request)

        post.save()
        request.session['liked_posts'] = liked_posts
        request.session.modified = True

        return JsonResponse({'liked': liked, 'like_count': post.like_count})
    return JsonResponse({'error': 'Invalid request'}, status=400)


@superadmin_admin_required
def NewsPostCommentView(request, pk):
    if request.method == 'POST':
        post = get_object_or_404(NewsPost, pk=pk)
        content = request.POST.get('content')
        guest_name = request.POST.get('guest_name', 'Anonymous Viewer')

        if content and content.strip():
            comment = NewsComment(post=post, content=content.strip())

            if request.user.is_authenticated:
                comment.author = request.user
            else:
                comment.guest_name = guest_name.strip()

            comment.save()
            log_audit_action(request.user, AuditLog.Action.CREATE, comment, request=request)
            messages.success(request, "Comment posted successfully.")

    return redirect('newsroom')


@superadmin_admin_required
@login_required
def NewsCommentDeleteView(request, pk):
    if request.method == 'POST':
        comment = get_object_or_404(NewsComment, pk=pk)

        log_audit_action(request.user, AuditLog.Action.DELETE, comment, request=request)
        comment.delete()
        messages.success(request, "Comment deleted successfully.")

    return redirect('newsroom')


@superadmin_admin_required
@login_required
def RegionCreateView(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        if 'sub_county_id' in request.POST:
            sub_county = get_object_or_404(SubCounty, pk=request.POST.get('sub_county_id'))
            ward = Ward.objects.create(name=name, sub_county=sub_county)
            log_audit_action(request.user, AuditLog.Action.CREATE, ward, request=request)
        else:
            sub_county = SubCounty.objects.create(name=name)
            log_audit_action(request.user, AuditLog.Action.CREATE, sub_county, request=request)
    return redirect('sports_regions')


@superadmin_admin_required
@login_required
def RegionUpdateView(request):
    pass


@superadmin_admin_required
@login_required
def AccountSettingsView(request):
    pass


@superadmin_admin_required
@login_required(login_url='login_admin')
def AuditLogView(request):
    logs = AuditLog.objects.select_related('user', 'content_type').order_by('-timestamp')

    action = request.GET.get('action')
    if action:
        logs = logs.filter(action=action)

    role = request.GET.get('role')
    if role:
        logs = logs.filter(user__role=role)

    date_from = request.GET.get('date_from')
    if date_from:
        logs = logs.filter(timestamp__date__gte=date_from)

    date_to = request.GET.get('date_to')
    if date_to:
        logs = logs.filter(timestamp__date__lte=date_to)

    query = request.GET.get('q')
    if query:
        logs = logs.filter(
            Q(object_repr__icontains=query) |
            Q(user__first_name__icontains=query) |
            Q(user__last_name__icontains=query) |
            Q(user__email__icontains=query)
        )

    paginator = Paginator(logs, 25)
    page_obj = paginator.get_page(request.GET.get('page'))

    now = timezone.now()
    today = now.date()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    verify_count_today = AuditLog.objects.filter(action='verify', timestamp__date=today).count()
    fixture_gen_count_week = AuditLog.objects.filter(action='generate_fixtures', timestamp__gte=week_ago).count()
    staff_created_count_month = AuditLog.objects.filter(
        action='create', content_type__model='user', timestamp__gte=month_ago
    ).count()
    deletions_today = AuditLog.objects.filter(action='delete', timestamp__date=today).count()

    context = {
        'audit_entries': page_obj,
        'page_obj': page_obj,
        'is_paginated': page_obj.has_other_pages(),
        'action_choices': AuditLog.Action.choices,
        'role_choices': User.Role.choices,
        'active_nav': 'audit',

        'total_entries': paginator.count,
        'verify_count_today': verify_count_today,
        'fixture_gen_count_week': fixture_gen_count_week,
        'staff_created_count_month': staff_created_count_month,
        'deletions_today': deletions_today,
    }
    return render(request, 'superadmin_auditLogs.html', context)


@superadmin_admin_required
@login_required(login_url='login_admin')
def ResultEntryView(request, fixture_pk):
    fixture = get_object_or_404(
        Fixture.objects.select_related('home_team', 'away_team', 'phase_sport__phase__tournament'),
        pk=fixture_pk
    )
    tournament = fixture.phase_sport.phase.tournament
    home_players = Player.objects.filter(tournament=tournament, team=fixture.home_team).order_by('jersey_number')
    away_players = Player.objects.filter(tournament=tournament, team=fixture.away_team).order_by('jersey_number')
    result = Result.objects.filter(fixture=fixture).first()
    existing_goals = Goal.objects.filter(fixture=fixture).select_related('scorer', 'assisted_by') if result else []
    existing_home_goals = [g for g in existing_goals if g.scorer.team_id == fixture.home_team_id]
    existing_away_goals = [g for g in existing_goals if g.scorer.team_id == fixture.away_team_id]

    existing_cards = Card.objects.filter(fixture=fixture).select_related('player') if result else []
    existing_home_cards = [c for c in existing_cards if c.player.team_id == fixture.home_team_id]
    existing_away_cards = [c for c in existing_cards if c.player.team_id == fixture.away_team_id]

    if request.method == 'POST':
        home_score = int(request.POST.get('home_score') or 0)
        away_score = int(request.POST.get('away_score') or 0)
        if result:
            result.home_score = home_score
            result.away_score = away_score
            result.save()
        else:
            result = Result.objects.create(
                fixture=fixture, home_score=home_score, away_score=away_score, entered_by=request.user
            )
        fixture.status = Fixture.Status.COMPLETED
        fixture.save(update_fields=['status'])
        log_audit_action(request.user, AuditLog.Action.UPDATE, fixture, request=request)

        Goal.objects.filter(fixture=fixture).delete()
        scorer_ids = request.POST.getlist('scorer')
        assist_ids = request.POST.getlist('assisted_by')
        minutes = request.POST.getlist('minute')
        for scorer_id, assist_id, minute in zip(scorer_ids, assist_ids, minutes):
            if not scorer_id:
                continue
            Goal.objects.create(
                fixture=fixture, scorer_id=scorer_id, assisted_by_id=assist_id or None, minute=minute or None,
            )

        Card.objects.filter(fixture=fixture).delete()
        card_player_ids = request.POST.getlist('card_player')
        card_types = request.POST.getlist('card_type')
        card_minutes = request.POST.getlist('card_minute')
        for player_id, card_type, minute in zip(card_player_ids, card_types, card_minutes):
            if not player_id or not card_type:
                continue
            Card.objects.create(
                fixture=fixture, player_id=player_id, card_type=card_type, minute=minute or None,
            )

        messages.success(request, f'Result saved for {fixture}.')
        return redirect('phase_detail', pk=fixture.phase_sport.phase_id)

    return render(request, 'superadmin_result_entry.html', {
        'fixture': fixture,
        'result': result,
        'home_players': home_players,
        'away_players': away_players,
        'existing_home_goals': existing_home_goals,
        'existing_away_goals': existing_away_goals,
        'existing_home_cards': existing_home_cards,
        'existing_away_cards': existing_away_cards,
        'active_nav': 'tournaments',
    })


from django.db.models import Q, Sum


@superadmin_admin_required
@login_required(login_url='login_admin')
def manage_team(request, pk):
    team = get_object_or_404(Team.objects.select_related('sport', 'ward'), pk=pk)
    players = team.players.all().order_by('jersey_number', 'name')

    from_phase_id = request.GET.get('from_phase')
    phase = get_object_or_404(Phase, pk=from_phase_id) if from_phase_id else None

    tournament = phase.tournament if phase else None
    if not tournament:
        first_entry = team.phase_entries.select_related('phase__tournament').first()
        tournament = first_entry.phase.tournament if first_entry else None

    team_fixtures = (
        Fixture.objects
        .filter(Q(home_team=team) | Q(away_team=team))
        .select_related('home_team', 'away_team', 'phase_sport__sport', 'phase_sport__phase', 'result')
        .order_by('-kickoff_at', '-round_number')
    )

    if request.method == "POST":
        action = request.POST.get('action')

        # Build the redirect target up front so every branch shares it —
        # fixes the previous bug where only 'remove_player' defined it.
        redirect_url = reverse('manage_team', kwargs={'pk': team.pk})
        if from_phase_id:
            redirect_url += f"?from_phase={from_phase_id}"

        if action == 'edit_team':
            name = request.POST.get('name', '').strip()
            if not name:
                messages.error(request, "Team name cannot be empty.")
            else:
                team.name = name
                team.home_ground = request.POST.get('home_ground', '').strip()
                team.coach_name = request.POST.get('coach_name', '').strip()
                team.coach_phone = request.POST.get('coach_phone', '').strip()
                team.save()
                log_audit_action(request.user, AuditLog.Action.UPDATE, team, request=request)
                messages.success(request, "Team details updated successfully.")

        elif action == 'add_player':
            if not tournament:
                messages.error(request, "This team isn't entered into any tournament yet — add it to a phase first.")
            else:
                name = request.POST.get('player_name', '').strip()
                jersey_raw = request.POST.get('jersey_number', '').strip()
                position = request.POST.get('position', '').strip()

                if not name or not jersey_raw:
                    messages.error(request, "Both name and jersey number are required.")
                else:
                    try:
                        jersey = int(jersey_raw)
                    except ValueError:
                        messages.error(request, "Jersey number must be a whole number.")
                    else:
                        if Player.objects.filter(team=team, tournament=tournament, jersey_number=jersey).exists():
                            messages.error(request, f"Jersey number #{jersey} is already taken on this team.")
                        else:
                            player = Player.objects.create(
                                team=team,
                                tournament=tournament,
                                name=name,
                                jersey_number=jersey,
                                position=position,
                                created_by=request.user,
                            )
                            log_audit_action(request.user, AuditLog.Action.CREATE, player, request=request)
                            messages.success(request, f"{name} added to the squad.")

        elif action == 'remove_player':
            player = get_object_or_404(Player, pk=request.POST.get('player_id'), team=team)
            player_name = player.name
            player.delete()
            log_audit_action(request.user, AuditLog.Action.DELETE, player, request=request)
            messages.success(request, f"Removed {player_name} from the roster.")

        elif action == 'bulk_add_players':
            if not tournament:
                messages.error(request, "This team isn't entered into any tournament yet — add it to a phase first.")
            else:
                csv_file = request.FILES.get('csv_file')
                if not csv_file:
                    messages.error(request, "Please choose a CSV file to upload.")
                elif not csv_file.name.lower().endswith('.csv'):
                    messages.error(request, "Please upload a file with a .csv extension.")
                else:
                    try:
                        decoded = csv_file.read().decode('utf-8-sig')
                    except UnicodeDecodeError:
                        messages.error(request, "Couldn't read that file — please save it as UTF-8 CSV and try again.")
                    else:
                        reader = csv.DictReader(StringIO(decoded))
                        headers = {h.strip().lower() for h in (reader.fieldnames or [])}

                        if not {'name', 'jersey_number'}.issubset(headers):
                            messages.error(request, "CSV must include 'name' and 'jersey_number' columns.")
                        else:
                            valid_positions = {c[0] for c in Player.Position.choices}
                            existing_jerseys = set(
                                Player.objects.filter(team=team, tournament=tournament)
                                .values_list('jersey_number', flat=True)
                            )
                            seen_in_file = set()
                            to_create = []
                            errors = []

                            for i, row in enumerate(reader, start=2):  # row 2 = first data row (row 1 is header)
                                row = {k.strip().lower(): (v or '').strip() for k, v in row.items()}
                                name = row.get('name', '')
                                jersey_raw = row.get('jersey_number', '')
                                position = row.get('position', '').upper()
                                national_id = row.get('national_id', '')

                                if not name or not jersey_raw:
                                    errors.append(f"Row {i}: missing name or jersey number.")
                                    continue
                                try:
                                    jersey = int(jersey_raw)
                                except ValueError:
                                    errors.append(f"Row {i}: jersey number '{jersey_raw}' is not a whole number.")
                                    continue
                                if jersey in existing_jerseys or jersey in seen_in_file:
                                    errors.append(f"Row {i}: jersey #{jersey} is already taken.")
                                    continue
                                if position and position not in valid_positions:
                                    errors.append(f"Row {i}: unknown position '{position}' — left blank.")
                                    position = ''

                                to_create.append(Player(
                                    team=team, tournament=tournament, name=name,
                                    jersey_number=jersey, position=position,
                                    national_id=national_id, created_by=request.user,
                                ))
                                seen_in_file.add(jersey)

                            if to_create:
                                Player.objects.bulk_create(to_create)
                                log_audit_action(
                                    request.user, AuditLog.Action.CREATE, team, request=request,
                                    changes={'bulk_players_added': len(to_create)}
                                )
                                messages.success(
                                    request,
                                    f"Added {len(to_create)} player{'s' if len(to_create) != 1 else ''} from CSV."
                                )
                            if errors:
                                preview = " | ".join(errors[:10])
                                more = f" (+{len(errors) - 10} more)" if len(errors) > 10 else ""
                                messages.warning(request, f"{len(errors)} row(s) skipped: {preview}{more}")

        return redirect(redirect_url)

    context = {
        'team': team,
        'phase': phase,
        'tournament': tournament,
        'players': players,
        'team_fixtures': team_fixtures,
        'squad_goals': players.aggregate(total=Sum('goals'))['total'] or 0,
        'positions': Player.Position.choices,
    }
    return render(request, 'superadmin_manage_team.html', context)


@superadmin_admin_required
@login_required(login_url='login_admin')
def NextRoundBuilderView(request, phase_sport_pk):
    phase_sport = get_object_or_404(PhaseSport.objects.select_related('phase', 'sport'), pk=phase_sport_pk)
    phase = phase_sport.phase
    qualify_per_group = int(request.GET.get('qualify_per_group', 2))

    qualified_teams, next_round_number, error, info = resolve_next_round(phase_sport, qualify_per_group)
    if error:
        messages.error(request, error)
        return redirect('phase_detail', pk=phase.pk)
    if info:
        messages.info(request, info)
        return redirect('phase_detail', pk=phase.pk)

    if request.method == 'POST':
        created, err = create_next_round_fixtures(phase_sport, request.POST, next_round_number)
        if err:
            messages.error(request, err)
            return redirect(request.path + f"?qualify_per_group={qualify_per_group}")
        messages.success(request, f"Round {next_round_number} created with {created} fixture(s).")
        return redirect('phase_detail', pk=phase.pk)

    return render(request, 'superadmin_next_round_builder.html', {
        'phase': phase,
        'phase_sport': phase_sport,
        'qualified_teams': qualified_teams,
        'next_round_number': next_round_number,
        'active_nav': 'tournaments',
    })


@superadmin_admin_required
@login_required(login_url='login_admin')
def account_settings(request):
    profile_form = ProfileForm(instance=request.user)
    password_form = PasswordChangeForm(user=request.user)

    if request.method == 'POST':
        if request.POST.get('form_type') == 'profile':
            profile_form = ProfileForm(request.POST, instance=request.user)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Profile updated.")
                return redirect('account_settings')

        elif request.POST.get('form_type') == 'password':
            password_form = PasswordChangeForm(user=request.user, data=request.POST)
            if password_form.is_valid():
                user = password_form.save()
                update_session_auth_hash(request, user)
                messages.success(request, "Password changed successfully.")
                return redirect('account_settings')

    return render(request, 'superadmin_account_settings.html', {
        'profile_form': profile_form,
        'password_form': password_form,
        'active_nav': 'settings',
    })


# WARD
@ward_admin_required
def ward_dashboard(request):
    ward = request.user.ward

    ward_phases = Phase.objects.filter(ward=ward).select_related('tournament')
    active_phases = ward_phases.filter(status__in=[Phase.Status.ONGOING, Phase.Status.UPCOMING])

    team_count = Team.objects.filter(phase_entries__phase__in=ward_phases).distinct().count()
    player_count = Player.objects.filter(team__ward=ward).count()

    upcoming_fixtures = (
        Fixture.objects
        .filter(phase_sport__phase__in=ward_phases, status=Fixture.Status.SCHEDULED)
        .select_related('home_team', 'away_team', 'phase_sport__sport')
        .order_by('kickoff_at')[:5]
    )
    pending_results_count = Result.objects.filter(
        fixture__phase_sport__phase__in=ward_phases, verified=False
    ).count()

    context = {
        'active_nav': 'dashboard',
        'ward': ward,
        'ward_phases': ward_phases,
        'active_phase_count': active_phases.count(),
        'team_count': team_count,
        'player_count': player_count,
        'upcoming_fixtures': upcoming_fixtures,
        'pending_results_count': pending_results_count,
    }
    return render(request, 'ward/wardadmin_dashboard.html', context)


@ward_admin_required
@login_required
def ward_tournament(request):
    if not hasattr(request.user, 'ward') or not request.user.ward:
        return redirect('login')
    ward = request.user.ward

    if request.method == 'POST':
        form = WardTournamentForm(request.POST)
        if form.is_valid():
            tournament = form.save(commit=False)
            tournament.sub_county = ward.sub_county
            tournament.ward = ward
            tournament.created_by = request.user
            tournament.save()
            form.save_m2m()

            phase = Phase.objects.create(
                tournament=tournament,
                ward=ward,
                stage='ward',
                status=tournament.status,
            )

            log_audit_action(request.user, AuditLog.Action.CREATE, tournament, request=request)
            messages.success(request, f'"{tournament.name}" was created for {ward.name} Ward.')
            return redirect(f"{request.path}?phase_id={phase.pk}")
        else:
            print('Ward tournament form errors:', form.errors.as_json())
    else:
        form = WardTournamentForm()

    ward_phases = Phase.objects.filter(ward=ward).select_related('tournament').order_by('-status', '-id')
    phase_id = request.GET.get('phase_id')
    selected_phase = None
    if phase_id:
        selected_phase = get_object_or_404(Phase, pk=phase_id, ward=ward)
    elif ward_phases.exists():
        selected_phase = ward_phases.filter(status='ongoing').first() or ward_phases.first()

    context = {
        'active_nav': 'tournament',
        'ward': ward,
        'ward_phases': ward_phases,
        'selected_phase': selected_phase,
        'form': form,
        'open_form_panel': request.method == 'POST',
        'teams': [], 'fixtures': [], 'results': [],
        'sport_rows': [], 'fixtures_by_sport': {},
        'fixture_dates': [], 'has_unscheduled': False, 'postponed_fixtures': [],
    }

    if selected_phase:
        teams = Team.objects.filter(phase_entries__phase=selected_phase, ward=ward).distinct().order_by('name')

        tournament_sports = selected_phase.tournament.sports.filter(is_active=True).order_by('name')
        existing_phase_sports = {
            ps.sport_id: ps
            for ps in PhaseSport.objects.filter(phase=selected_phase).select_related('sport')
        }
        sport_rows = [
            {'sport': sport, 'phase_sport': existing_phase_sports.get(sport.id)}
            for sport in tournament_sports
        ]

        fixtures = Fixture.objects.filter(phase_sport__phase=selected_phase).select_related(
            'home_team', 'away_team', 'phase_sport__sport', 'result'
        ).order_by('kickoff_at', 'id')

        postponed_fixtures = fixtures.filter(status='postponed')
        active_fixtures = fixtures.exclude(status='postponed')

        fixtures_by_sport = defaultdict(list)
        for f in active_fixtures:
            fixtures_by_sport[f.phase_sport.sport_id].append(f)

        fixture_dates = sorted({f.kickoff_at.date() for f in active_fixtures if f.kickoff_at})
        has_unscheduled = active_fixtures.filter(kickoff_at__isnull=True).exists()

        results = Result.objects.filter(fixture__phase_sport__phase=selected_phase).select_related(
            'fixture__home_team', 'fixture__away_team', 'fixture__phase_sport__sport'
        ).order_by('-entered_at')

        context.update({
            'teams': teams,
            'sport_rows': sport_rows,
            'fixtures_by_sport': dict(fixtures_by_sport),
            'fixtures': active_fixtures,
            'results': results,
            'fixture_dates': fixture_dates,
            'has_unscheduled': has_unscheduled,
            'postponed_fixtures': postponed_fixtures,
        })

    return render(request, 'ward/ward_tournament.html', context)


@ward_admin_required
@login_required
def WardGenerateFixturesView(request, pk):
    ward = request.user.ward
    phase = get_object_or_404(Phase, pk=pk, ward=ward, stage=Phase.Stage.WARD)

    if request.method == 'POST':
        form = FixtureGenerationForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            sport = data['sport']
            phase_sport, _ = PhaseSport.objects.get_or_create(
                phase=phase,
                sport=sport,
                defaults={'fixture_format': data['format'], 'legs': data.get('legs', 1)},
            )
            phase_sport.fixture_format = data['format']
            phase_sport.legs = data.get('legs', 1)
            phase_sport.save(update_fields=['fixture_format', 'legs'])

            try:
                generator = FixtureGenerator(phase_sport, config={
                    'start_date': data.get('start_date') and data['start_date'].isoformat(),
                    'duration': data.get('duration'),
                    'schedule_type': data.get('schedule_type', 'daily'),
                    'groups': data.get('groups', 2),
                    'max_matches_per_day': data.get('max_matches_per_day') or None,
                })
                generator.generate()
                log_audit_action(request.user, AuditLog.Action.GENERATE_FIXTURES, phase_sport, request=request)
                phase_sport.fixtures_generated = True
                phase_sport.save(update_fields=['fixtures_generated'])
                messages.success(request, f'Fixtures for {phase_sport.sport.name} generated successfully.')
            except ValidationError as e:
                messages.error(request, str(e))

            return redirect(f"{reverse('ward_tournament')}?phase_id={phase.pk}")
    else:
        initial = {}
        sport_id = request.GET.get('sport_id')
        if sport_id:
            initial['sport'] = sport_id
        form = FixtureGenerationForm(initial=initial)
        # restrict sport choices to this tournament's sports
        form.fields['sport'].queryset = phase.tournament.sports.filter(is_active=True)

    return render(request, 'ward/wardadmin_generate_fixture.html', {'form': form, 'phase': phase})


@ward_admin_required
@login_required
def WardNextRoundBuilderView(request, phase_sport_pk):
    ward = request.user.ward
    phase_sport = get_object_or_404(
        PhaseSport.objects.select_related('phase', 'sport'),
        pk=phase_sport_pk,
        phase__ward=ward,
        phase__stage=Phase.Stage.WARD,
    )
    phase = phase_sport.phase
    qualify_per_group = int(request.GET.get('qualify_per_group', 2))

    qualified_teams, next_round_number, error, info = resolve_next_round(phase_sport, qualify_per_group)
    ward_redirect = f"{reverse('ward_tournament')}?phase_id={phase.pk}"

    if error:
        messages.error(request, error)
        return redirect(ward_redirect)
    if info:
        messages.info(request, info)
        return redirect(ward_redirect)

    if request.method == 'POST':
        created, err = create_next_round_fixtures(phase_sport, request.POST, next_round_number)
        if err:
            messages.error(request, err)
            return redirect(request.path + f"?qualify_per_group={qualify_per_group}")
        messages.success(request, f"Round {next_round_number} created with {created} fixture(s).")
        return redirect(ward_redirect)

    return render(request, 'ward/ward_next_round_builder.html', {
        'phase': phase,
        'phase_sport': phase_sport,
        'qualified_teams': qualified_teams,
        'next_round_number': next_round_number,
        'active_nav': 'tournament',
    })


@ward_admin_required
def ward_edit_fixture_view(request, pk):
    fixture = get_object_or_404(Fixture, pk=pk)

    if request.method == 'POST':
        form = FixtureEditForm(request.POST, instance=fixture)
        if form.is_valid():
            form.save()
            log_audit_action(request.user, AuditLog.Action.UPDATE, fixture, request=request)
            return redirect('phase_detail', fixture.phase_sport.pk)
    else:
        form = FixtureEditForm(instance=fixture)

    return render(request, 'ward_admin_edit_fixture.html', {'form': form, 'fixture': fixture})


@ward_admin_required
@login_required(login_url='login_admin')
def ward_manage_team(request, pk):
    team = get_object_or_404(Team.objects.select_related('sport', 'ward'), pk=pk)
    players = team.players.all().order_by('jersey_number', 'name')

    from_phase_id = request.GET.get('from_phase')
    phase = get_object_or_404(Phase, pk=from_phase_id) if from_phase_id else None

    # Resolve the tournament new players get registered against
    tournament = phase.tournament if phase else None
    if not tournament:
        first_entry = team.phase_entries.select_related('phase__tournament').first()
        tournament = first_entry.phase.tournament if first_entry else None

    team_fixtures = (
        Fixture.objects
        .filter(Q(home_team=team) | Q(away_team=team))
        .select_related('home_team', 'away_team', 'phase_sport__sport', 'phase_sport__phase', 'result')
        .order_by('-kickoff_at', '-round_number')
    )

    if request.method == "POST":
        action = request.POST.get('action')

        if action == 'edit_team':
            name = request.POST.get('name', '').strip()
            if not name:
                messages.error(request, "Team name cannot be empty.")
            else:
                team.name = name
                team.home_ground = request.POST.get('home_ground', '').strip()
                team.coach_name = request.POST.get('coach_name', '').strip()
                team.coach_phone = request.POST.get('coach_phone', '').strip()
                team.save()
                log_audit_action(request.user, AuditLog.Action.UPDATE, team, request=request)
                messages.success(request, "Team details updated successfully.")

        elif action == 'add_player':
            if not tournament:
                messages.error(request, "This team isn't entered into any tournament yet. Add it to a phase first.")
            else:
                name = request.POST.get('player_name', '').strip()
                jersey_raw = request.POST.get('jersey_number', '').strip()
                position = request.POST.get('position', '').strip()

                if not name or not jersey_raw:
                    messages.error(request, "Both name and jersey number are required.")
                else:
                    try:
                        jersey = int(jersey_raw)
                    except ValueError:
                        messages.error(request, "Jersey number must be a whole number.")
                    else:
                        if Player.objects.filter(team=team, tournament=tournament, jersey_number=jersey).exists():
                            messages.error(request, f"Jersey number #{jersey} is already taken on this team.")
                        else:
                            player = Player.objects.create(
                                team=team,
                                tournament=tournament,
                                name=name,
                                jersey_number=jersey,
                                position=position,
                                created_by=request.user,
                            )
                            log_audit_action(request.user, AuditLog.Action.CREATE, player, request=request)
                            messages.success(request, f"{name} added to the squad.")

        elif action == 'remove_player':
            player = get_object_or_404(Player, pk=request.POST.get('player_id'), team=team)
            player_name = player.name
            player.delete()
            log_audit_action(request.user, AuditLog.Action.DELETE, player, request=request)
            messages.success(request, f"Removed {player_name} from the roster.")

        elif action == 'bulk_add_players':
            if not tournament:
                messages.error(request, "This team isn't entered into any tournament yet. Add it to a phase first.")
            else:
                csv_file = request.FILES.get('csv_file')
                if not csv_file:
                    messages.error(request, "Please choose a CSV file to upload.")
                elif not csv_file.name.lower().endswith('.csv'):
                    messages.error(request, "Please upload a file with a .csv extension.")
                else:
                    try:
                        decoded = csv_file.read().decode('utf-8-sig')
                    except UnicodeDecodeError:
                        messages.error(request, "Couldn't read that file. Please save it as UTF-8 CSV and try again.")
                    else:
                        reader = csv.DictReader(StringIO(decoded))
                        headers = {h.strip().lower() for h in (reader.fieldnames or [])}

                        if not {'name', 'jersey_number'}.issubset(headers):
                            messages.error(request, "CSV must include 'name' and 'jersey_number' columns.")
                        else:
                            valid_positions = {c[0] for c in Player.Position.choices}
                            existing_jerseys = set(
                                Player.objects.filter(team=team, tournament=tournament)
                                .values_list('jersey_number', flat=True)
                            )
                            seen_in_file = set()
                            to_create = []
                            errors = []

                            for i, row in enumerate(reader, start=2):  # Row 2 = first data row
                                row = {k.strip().lower(): (v or '').strip() for k, v in row.items()}
                                name = row.get('name', '')
                                jersey_raw = row.get('jersey_number', '')
                                position = row.get('position', '').upper()
                                national_id = row.get('national_id', '')

                                if not name or not jersey_raw:
                                    errors.append(f"Row {i}: missing name or jersey number.")
                                    continue
                                try:
                                    jersey = int(jersey_raw)
                                except ValueError:
                                    errors.append(f"Row {i}: jersey number '{jersey_raw}' is not a whole number.")
                                    continue
                                if jersey in existing_jerseys or jersey in seen_in_file:
                                    errors.append(f"Row {i}: jersey #{jersey} is already taken.")
                                    continue
                                if position and position not in valid_positions:
                                    errors.append(f"Row {i}: unknown position '{position}' - left blank.")
                                    position = ''

                                to_create.append(Player(
                                    team=team, tournament=tournament, name=name,
                                    jersey_number=jersey, position=position,
                                    national_id=national_id, created_by=request.user,
                                ))
                                seen_in_file.add(jersey)

                            if to_create:
                                Player.objects.bulk_create(to_create)
                                log_audit_action(
                                    request.user, AuditLog.Action.CREATE, team, request=request,
                                    changes={'bulk_players_added': len(to_create)}
                                )
                                messages.success(
                                    request,
                                    f"Added {len(to_create)} player{'s' if len(to_create) != 1 else ''} from CSV."
                                )
                            if errors:
                                preview = " | ".join(errors[:10])
                                more = f" (+{len(errors) - 10} more)" if len(errors) > 10 else ""
                                messages.warning(request, f"{len(errors)} row(s) skipped: {preview}{more}")

        redirect_url = reverse('ward_manage_team', kwargs={'pk': team.pk})
        if from_phase_id:
            redirect_url += f"?from_phase={from_phase_id}"
        return redirect(redirect_url)

    context = {
        'team': team,
        'phase': phase,
        'tournament': tournament,
        'players': players,
        'team_fixtures': team_fixtures,
        'squad_goals': players.aggregate(total=Sum('goals'))['total'] or 0,
        'positions': Player.Position.choices,
    }
    return render(request, 'ward/wardadmin_manage_team.html', context)


@ward_admin_required
@login_required(login_url='login_admin')
def ward_ResultEntryView(request, fixture_pk):
    fixture = get_object_or_404(
        Fixture.objects.select_related('home_team', 'away_team', 'phase_sport__phase__tournament'),
        pk=fixture_pk
    )
    tournament = fixture.phase_sport.phase.tournament
    home_players = Player.objects.filter(tournament=tournament, team=fixture.home_team).order_by('jersey_number')
    away_players = Player.objects.filter(tournament=tournament, team=fixture.away_team).order_by('jersey_number')

    result = Result.objects.filter(fixture=fixture).first()

    # Query existing Goals
    existing_goals = Goal.objects.filter(fixture=fixture).select_related('scorer', 'assisted_by') if result else []
    existing_home_goals = [g for g in existing_goals if g.scorer.team_id == fixture.home_team_id]
    existing_away_goals = [g for g in existing_goals if g.scorer.team_id == fixture.away_team_id]

    # Query existing Cards (Assuming you have a Booking/Card model)
    # Adjust "Booking" below to whatever your actual card model is named (e.g., Booking, Card, CardRecord)
    existing_cards = Card.objects.filter(fixture=fixture).select_related('player') if result else []
    existing_home_cards = [c for c in existing_cards if c.player.team_id == fixture.home_team_id]
    existing_away_cards = [c for c in existing_cards if c.player.team_id == fixture.away_team_id]

    if request.method == 'POST':
        home_score = int(request.POST.get('home_score') or 0)
        away_score = int(request.POST.get('away_score') or 0)

        with transaction.atomic():
            if result:
                result.home_score = home_score
                result.away_score = away_score
                result.save()
            else:
                result = Result.objects.create(
                    fixture=fixture, home_score=home_score, away_score=away_score, entered_by=request.user
                )

            fixture.status = Fixture.Status.COMPLETED
            fixture.save(update_fields=['status'])
            log_audit_action(request.user, AuditLog.Action.UPDATE, fixture, request=request)

            # --- Save Goals ---
            Goal.objects.filter(fixture=fixture).delete()
            scorer_ids = request.POST.getlist('scorer')
            assist_ids = request.POST.getlist('assisted_by')
            minutes = request.POST.getlist('minute')
            for scorer_id, assist_id, minute in zip(scorer_ids, assist_ids, minutes):
                if not scorer_id:
                    continue
                Goal.objects.create(
                    fixture=fixture, scorer_id=scorer_id, assisted_by_id=assist_id or None, minute=minute or None,
                )

            # --- Save Cards / Bookings ---
            Card.objects.filter(fixture=fixture).delete()
            card_players = request.POST.getlist('card_player')
            card_types = request.POST.getlist('card_type')
            card_minutes = request.POST.getlist('card_minute')
            for player_id, c_type, c_minute in zip(card_players, card_types, card_minutes):
                if not player_id or not c_type:
                    continue
                Card.objects.create(
                    fixture=fixture,
                    player_id=player_id,
                    card_type=c_type,  # 'yellow' or 'red'
                    minute=c_minute or None
                )

        messages.success(request, f'Result saved for {fixture}.')
        return redirect('ward_tournament')

    return render(request, 'ward/wardadmin_result_entry.html', {
        'fixture': fixture,
        'result': result,
        'home_players': home_players,
        'away_players': away_players,
        'existing_home_goals': existing_home_goals,
        'existing_away_goals': existing_away_goals,
        'existing_home_cards': existing_home_cards,
        'existing_away_cards': existing_away_cards,
        'active_nav': 'tournaments',
    })


@ward_admin_required
@login_required(login_url='login_admin')
def ward_edit_fixture_view(request, pk):
    fixture = get_object_or_404(Fixture, pk=pk)

    if request.method == 'POST':
        form = FixtureEditForm(request.POST, instance=fixture)
        if form.is_valid():
            form.save()
            return redirect('ward_tournament')
    else:
        form = FixtureEditForm(instance=fixture)

    return render(request, 'ward/wardadmin_edit_fixture.html', {'form': form, 'fixture': fixture})


@ward_admin_required
@login_required(login_url='login_admin')
def ward_TeamCreateView(request, phase_pk):
    phase = get_object_or_404(Phase, pk=phase_pk)

    if phase.stage != Phase.Stage.WARD:
        messages.error(
            request,
            'New teams can only be created at Ward level. '
            'For this phase, use "Select Teams" to bring up qualified teams instead.'
        )
        return redirect('phase_detail', pk=phase.pk)

    if request.method == 'POST':
        # 1. Handle CSV Import
        if 'csv_file' in request.FILES:
            csv_file = request.FILES['csv_file']
            data = csv_file.read().decode('utf-8')
            io_string = io.StringIO(data)
            reader = csv.DictReader(io_string)

            for row in reader:
                # Expecting columns: name, sport_id, home_ground, coach_name, coach_phone
                team = Team.objects.create(
                    name=row['name'],
                    sport_id=row['sport_id'],
                    ward=phase.ward,
                    home_ground=row.get('home_ground', ''),
                    coach_name=row.get('coach_name', ''),
                    coach_phone=row.get('coach_phone', ''),
                    created_by=request.user
                )
                log_audit_action(request.user, AuditLog.Action.CREATE, team, request=request)
                phase_entry = PhaseEntry.objects.create(
                    phase=phase,
                    team=team,
                    registered_by=request.user
                )
                log_audit_action(request.user, AuditLog.Action.CREATE, phase_entry, request=request)
            messages.success(request, "Bulk teams imported successfully.")
            return redirect('ward_dashboard')

        # 2. Handle Single Form
        form = TeamForm(request.POST)
        # Manually inject the ward since it's disabled in the form
        if form.is_valid():
            team = form.save(commit=False)
            team.ward = phase.ward
            team.created_by = request.user
            team.save()
            log_audit_action(request.user, AuditLog.Action.CREATE, team, request=request)
            phase_entry = PhaseEntry.objects.create(phase=phase, team=team, registered_by=request.user)
            log_audit_action(request.user, AuditLog.Action.CREATE, phase_entry, request=request)
            return redirect("ward_dashboard")
        form = TeamForm(initial={'ward': phase.ward})

    return render(request, 'ward/wardadmin_team_form.html', {'form': form, 'phase': phase})


@ward_admin_required
@login_required
def ward_newsroom(request):
    """
    Renders the Ward Admin Newsroom feed containing:
    1. County-Wide announcements (sub_county=None)
    2. Sub-County scoped updates matching the admin's ward's sub_county
    3. Tournament phase posts localized to the admin's ward/sub-county
    """
    if not hasattr(request.user, 'ward') or not request.user.ward:
        messages.error(request, "Access denied. You must be a Ward Admin to access this page.")
        return redirect('login')

    ward = request.user.ward
    sub_county = ward.sub_county

    # 1. Fetch local phases running inside this Ward or Sub-County to populate the form dropdown
    phases = Phase.objects.filter(
        Q(ward=ward) | Q(sub_county=sub_county)
    ).select_related('tournament').distinct()

    # 2. Fetch scoped feed: global announcements + localized/sub-county updates
    posts = NewsPost.objects.filter(
        Q(sub_county__isnull=True) |  # County-wide posts from County Admin
        Q(sub_county=sub_county) |  # Local Sub-County posts
        Q(phase__in=phases)  # Posts tied directly to their local phases
    ).select_related('author', 'sub_county', 'phase__tournament').prefetch_related(
        'comments__author'
    ).distinct().order_by('-published_at')

    context = {
        'active_nav': 'newsroom',
        'ward': ward,
        'sub_county': sub_county,
        'phases': phases,
        'posts': posts,
    }
    return render(request, 'ward/newsroom.html', context)


@ward_admin_required
@login_required
def news_create(request):
    """
    Handles publishing a news post. Scopes the post automatically
    to the Ward Admin's Sub-County.
    """
    if not hasattr(request.user, 'ward') or not request.user.ward:
        messages.error(request, "Access unauthorized.")
        return redirect('ward_newsroom')

    if request.method == 'POST':
        title = request.POST.get('title')
        tag = request.POST.get('tag')
        phase_id = request.POST.get('phase')
        body = request.POST.get('body')

        ward = request.user.ward
        sub_county = ward.sub_county

        # Initialize post scoped to the admin's local Sub-County context
        post = NewsPost(
            title=title,
            tag=tag,
            body=body,
            author=request.user,
            sub_county=sub_county
        )

        # If a specific phase scope was chosen, validate its access limits
        if phase_id:
            phase = get_object_or_404(Phase, id=phase_id)
            if phase.ward == ward or phase.sub_county == sub_county:
                post.phase = phase
            else:
                messages.error(request, "Security breach: Selected phase falls outside your ward jurisdiction.")
                return redirect('ward_newsroom')

        post.save()
        messages.success(request, "News post published successfully!")

    return redirect('ward_newsroom')


@ward_admin_required
@login_required
def news_edit(request, pk):
    """
    Enables Ward Admins to update their own posts or posts belonging
    specifically to their local sub_county scope.
    """
    if not hasattr(request.user, 'ward') or not request.user.ward:
        messages.error(request, "Access unauthorized.")
        return redirect('ward_newsroom')

    post = get_object_or_404(NewsPost, pk=pk)
    ward = request.user.ward
    sub_county = ward.sub_county

    # Authority check: only author or matching sub-county admin can modify
    if post.author != request.user and post.sub_county != sub_county:
        messages.error(request, "You do not have permission to modify this post.")
        return redirect('ward_newsroom')

    if request.method == 'POST':
        post.title = request.POST.get('title')
        post.tag = request.POST.get('tag')
        post.body = request.POST.get('body')

        phase_id = request.POST.get('phase')
        if phase_id:
            phase = get_object_or_404(Phase, id=phase_id)
            if phase.ward == ward or phase.sub_county == sub_county:
                post.phase = phase
            else:
                messages.error(request, "Invalid phase scope selected.")
                return redirect('ward_newsroom')
        else:
            post.phase = None  # Reset to general Ward/Sub-County scope

        post.save()
        messages.success(request, "News post updated successfully!")

    return redirect('ward_newsroom')


@ward_admin_required
@login_required
def news_delete(request, pk):
    """
    Enables Ward Admins to delete their local posts.
    """
    if not hasattr(request.user, 'ward') or not request.user.ward:
        messages.error(request, "Access unauthorized.")
        return redirect('ward_newsroom')

    post = get_object_or_404(NewsPost, pk=pk)
    ward = request.user.ward
    sub_county = ward.sub_county

    # Authority check: only author or matching sub-county admin can delete
    if post.author != request.user and post.sub_county != sub_county:
        messages.error(request, "Permission denied to delete this post.")
        return redirect('ward_newsroom')

    if request.method == 'POST':
        post.delete()
        messages.success(request, "News post deleted successfully!")

    return redirect('ward_newsroom')


@ward_admin_required
@login_required
def news_comment_delete(request, pk):
    """
    Moderation tool: Allows local admin to purge comments on their own
    posts or localized sub-county posts.
    """
    if not hasattr(request.user, 'ward') or not request.user.ward:
        messages.error(request, "Access unauthorized.")
        return redirect('ward_newsroom')

    comment = get_object_or_404(NewsComment, pk=pk)
    ward = request.user.ward
    sub_county = ward.sub_county

    # Permissions rules:
    # 1. User wrote the comment
    # 2. User is author of the NewsPost
    # 3. User is a local Admin overseeing the post's sub-county scope
    is_comment_author = comment.author == request.user
    is_post_author = comment.post.author == request.user
    is_local_moderator = comment.post.sub_county == sub_county

    if is_comment_author or is_post_author or is_local_moderator:
        comment.delete()
        messages.success(request, "Comment removed successfully.")
    else:
        messages.error(request, "You do not have permission to delete this comment.")

    return redirect('ward_newsroom')


@ward_admin_required
@login_required
def ward_account_settings(request):
    if not hasattr(request.user, 'ward') or not request.user.ward:
        messages.error(request, "Access denied. Only Ward Admins can access this panel.")
        return redirect('login')

    ward = request.user.ward

    # Initialize forms
    profile_form = WardAdminProfileForm(instance=request.user)
    password_form = PasswordChangeForm(user=request.user)

    if request.method == 'POST':
        form_type = request.POST.get('form_type')

        if form_type == 'profile':
            profile_form = WardAdminProfileForm(request.POST, instance=request.user)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Your profile settings have been updated.")
                return redirect('ward_account_settings')
            else:
                messages.error(request, "Please correct the profile errors below.")

        elif form_type == 'password':
            password_form = PasswordChangeForm(user=request.user, data=request.POST)
            if password_form.is_valid():
                user = password_form.save()
                # Crucial step to prevent logging the user out after a password change
                update_session_auth_hash(request, user)
                messages.success(request, "Your password was changed successfully.")
                return redirect('ward_account_settings')
            else:
                messages.error(request, "Please correct the password validation errors below.")

    context = {
        'active_nav': 'account',
        'ward': ward,
        'profile_form': profile_form,
        'password_form': password_form,
    }
    return render(request, 'ward/account.html', context)


# SUB COUNTY
@subcounty_admin_required
def subcounty_dashboard(request):
    sub_county = request.user.sub_county

    sc_phases = Phase.objects.filter(sub_county=sub_county).select_related('tournament')
    active_phases = sc_phases.filter(status__in=[Phase.Status.ONGOING, Phase.Status.UPCOMING])

    team_count = Team.objects.filter(phase_entries__phase__in=sc_phases).distinct().count()
    player_count = Player.objects.filter(team__ward__sub_county=sub_county).count()

    upcoming_fixtures = (
        Fixture.objects
        .filter(phase_sport__phase__in=sc_phases, status=Fixture.Status.SCHEDULED)
        .select_related('home_team', 'away_team', 'phase_sport__sport')
        .order_by('kickoff_at')[:5]
    )
    pending_results_count = Result.objects.filter(
        fixture__phase_sport__phase__in=sc_phases, verified=False
    ).count()

    context = {
        'active_nav': 'dashboard',
        'sub_county': sub_county,
        'sc_phases': sc_phases,
        'active_phase_count': active_phases.count(),
        'team_count': team_count,
        'player_count': player_count,
        'upcoming_fixtures': upcoming_fixtures,
        'pending_results_count': pending_results_count,
    }
    return render(request, 'subcounty/subcountyadmin_dashboard.html', context)


@subcounty_admin_required
def subcounty_tournament(request):
    sub_county = request.user.sub_county
    my_wards = Ward.objects.filter(sub_county=sub_county)

    # 1. Fetch tournaments that have phases involving this sub-county or its wards
    tournaments = Tournament.objects.filter(
        Q(phases__sub_county=sub_county) | Q(phases__ward__in=my_wards)
    ).distinct().order_by('-season', 'name')

    # Apply search/filters
    search_query = request.GET.get('search', '')
    if search_query:
        tournaments = tournaments.filter(
            Q(name__icontains=search_query) | Q(season__icontains=search_query)
        ).distinct()

    # 2. Count metrics specifically for this Sub-County scope
    active_ward_phases = Phase.objects.filter(
        stage=Phase.Stage.WARD,
        ward__in=my_wards,
        status=Phase.Status.ONGOING
    ).count()

    active_subcounty_phases = Phase.objects.filter(
        stage=Phase.Stage.SUB_COUNTY,
        sub_county=sub_county,
        status=Phase.Status.ONGOING
    ).count()

    total_wards_count = my_wards.count()

    context = {
        'active_nav': 'tournament',
        'sub_county': sub_county,
        'tournaments': tournaments,
        'active_ward_phases': active_ward_phases,
        'active_subcounty_phases': active_subcounty_phases,
        'total_wards_count': total_wards_count,
        'search_query': search_query,
    }
    return render(request, 'subcounty/tournament.html', context)


@subcounty_admin_required
def subcounty_tournament_detail(request, tournament_id):
    sub_county = request.user.sub_county
    tournament = get_object_or_404(Tournament, id=tournament_id)
    my_wards = Ward.objects.filter(sub_county=sub_county)

    # Fetch all Ward-level phases for this tournament belonging to wards in this sub-county
    ward_phases = Phase.objects.filter(
        tournament=tournament,
        stage=Phase.Stage.WARD,
        ward__in=my_wards
    ).select_related('ward').order_by('ward__name')

    # Fetch the Sub-County-level phase for this sub-county
    sub_county_phase = Phase.objects.filter(
        tournament=tournament,
        stage=Phase.Stage.SUB_COUNTY,
        sub_county=sub_county
    ).first()

    # Handle fast phase status updates (e.g., transition from upcoming -> ongoing)
    if request.method == 'POST':
        phase_id = request.POST.get('phase_id')
        new_status = request.POST.get('status')
        if new_status in Phase.Status.values:
            phase = get_object_or_404(Phase, id=phase_id)

            # Security: Ensure this phase belongs to the admin's sub-county or wards
            if (phase.ward and phase.ward in my_wards) or (phase.sub_county == sub_county):
                phase.status = new_status
                phase.save()
                messages.success(request, f"Phase updated to {phase.get_status_display()}.")
            else:
                messages.error(request, "Unauthorized operation.")

        return redirect('subcounty_tournament_detail', tournament_id=tournament.id)

    context = {
        'active_nav': 'tournament',
        'sub_county': sub_county,
        'tournament': tournament,
        'ward_phases': ward_phases,
        'sub_county_phase': sub_county_phase,
    }
    return render(request, 'subcounty/tournament_detail.html', context)


@subcounty_admin_required
def subcounty_phase_detail(request, pk):
    sub_county = request.user.sub_county
    my_wards = Ward.objects.filter(sub_county=sub_county)
    phase = get_object_or_404(
        Phase.objects.select_related('tournament', 'ward', 'sub_county'),
        Q(id=pk) & (Q(sub_county=sub_county) | Q(ward__in=my_wards))
    )

    if request.method == "POST" and "update_fixture_status" in request.POST:
        fixture_id = request.POST.get("fixture_id")
        new_status = request.POST.get("status")
        fixture = get_object_or_404(Fixture, id=fixture_id, phase_sport__phase=phase)
        if new_status in Fixture.Status.values:
            fixture.status = new_status
            fixture.save()
            messages.success(request, f"Match status updated to {fixture.get_status_display()}.")
            return redirect('subcounty_phase_detail', phase_id=phase.id)

    # Sports on the tournament, paired with their PhaseSport row (if generated yet)
    tournament_sports = phase.tournament.sports.filter(is_active=True).order_by('name')
    existing_phase_sports = {
        ps.sport_id: ps
        for ps in PhaseSport.objects.filter(phase=phase).select_related('sport')
    }
    sport_rows = [
        {'sport': sport, 'phase_sport': existing_phase_sports.get(sport.id)}
        for sport in tournament_sports
    ]

    entries = (
        PhaseEntry.objects.filter(phase=phase)
        .select_related('team', 'team__ward', 'team__sport', 'promoted_from')
        .order_by('team__name')
    )
    teams = Team.objects.filter(phase_entries__phase=phase).distinct().order_by('name')

    fixtures = Fixture.objects.filter(phase_sport__phase=phase).select_related(
        'home_team', 'away_team', 'phase_sport__sport', 'group', 'result'
    ).order_by('round_number', 'kickoff_at')

    postponed_fixtures = fixtures.filter(status=Fixture.Status.POSTPONED)
    active_fixtures = fixtures.exclude(status=Fixture.Status.POSTPONED)

    fixtures_by_sport = defaultdict(list)
    for f in active_fixtures:
        fixtures_by_sport[f.phase_sport.sport_id].append(f)

    fixture_dates = sorted({f.kickoff_at.date() for f in active_fixtures if f.kickoff_at})
    has_unscheduled = active_fixtures.filter(kickoff_at__isnull=True).exists()

    results = Result.objects.filter(fixture__phase_sport__phase=phase).select_related(
        'fixture__home_team', 'fixture__away_team', 'fixture__phase_sport__sport', 'verified_by'
    ).order_by('-entered_at')

    context = {
        'active_nav': 'tournament',
        'sub_county': sub_county,
        'phase': phase,
        'tournament': phase.tournament,
        'sport_rows': sport_rows,
        'fixtures_by_sport': dict(fixtures_by_sport),
        'entries': entries,
        'teams': teams,
        'fixtures': active_fixtures,
        'postponed_fixtures': postponed_fixtures,
        'fixture_dates': fixture_dates,
        'has_unscheduled': has_unscheduled,
        'results': results,
        'can_promote': phase.next_phase_lookup is not None,
    }
    return render(request, 'subcounty/phase_detail.html', context)


@subcounty_admin_required
def SubcountyGenerateFixturesView(request, pk):
    sub_county = request.user.sub_county
    my_wards = Ward.objects.filter(sub_county=sub_county)
    phase = get_object_or_404(
        Phase, Q(pk=pk) & (Q(sub_county=sub_county) | Q(ward__in=my_wards))
    )

    if request.method == 'POST':
        form = FixtureGenerationForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            sport = data['sport']
            phase_sport, _ = PhaseSport.objects.get_or_create(
                phase=phase, sport=sport,
                defaults={'fixture_format': data['format'], 'legs': data.get('legs', 1)},
            )
            phase_sport.fixture_format = data['format']
            phase_sport.legs = data.get('legs', 1)
            phase_sport.save(update_fields=['fixture_format', 'legs'])

            try:
                generator = FixtureGenerator(phase_sport, config={
                    'start_date': data.get('start_date') and data['start_date'].isoformat(),
                    'duration': data.get('duration'),
                    'schedule_type': data.get('schedule_type', 'daily'),
                    'groups': data.get('groups', 2),
                    'max_matches_per_day': data.get('max_matches_per_day') or None,
                })
                generator.generate()
                log_audit_action(request.user, AuditLog.Action.GENERATE_FIXTURES, phase_sport, request=request)
                phase_sport.fixtures_generated = True
                phase_sport.save(update_fields=['fixtures_generated'])
                messages.success(request, f'Fixtures for {phase_sport.sport.name} generated successfully.')
            except ValidationError as e:
                messages.error(request, str(e))

            return redirect('subcounty_phase_detail', pk=phase.pk)
    else:
        initial = {}
        sport_id = request.GET.get('sport_id')
        if sport_id:
            initial['sport'] = sport_id
        form = FixtureGenerationForm(initial=initial)
        form.fields['sport'].queryset = phase.tournament.sports.filter(is_active=True)

    return render(request, 'subcounty/generate_fixtures.html', {'form': form, 'phase': phase})


@subcounty_admin_required
def SubcountyNextRoundBuilderView(request, phase_sport_pk):
    sub_county = request.user.sub_county
    my_wards = Ward.objects.filter(sub_county=sub_county)
    phase_sport = get_object_or_404(
        PhaseSport.objects.select_related('phase', 'sport'),
        Q(pk=phase_sport_pk) & (Q(phase__sub_county=sub_county) | Q(phase__ward__in=my_wards))
    )
    phase = phase_sport.phase
    qualify_per_group = int(request.GET.get('qualify_per_group', 2))

    qualified_teams, next_round_number, error, info = resolve_next_round(phase_sport, qualify_per_group)
    redirect_url = redirect('subcounty_phase_detail', phase_id=phase.pk)

    if error:
        messages.error(request, error)
        return redirect_url
    if info:
        messages.info(request, info)
        return redirect_url

    if request.method == 'POST':
        created, err = create_next_round_fixtures(phase_sport, request.POST, next_round_number)
        if err:
            messages.error(request, err)
            return redirect(request.path + f"?qualify_per_group={qualify_per_group}")
        messages.success(request, f"Round {next_round_number} created with {created} fixture(s).")
        return redirect_url

    return render(request, 'subcounty/next_round_builder.html', {
        'phase': phase,
        'phase_sport': phase_sport,
        'qualified_teams': qualified_teams,
        'next_round_number': next_round_number,
        'active_nav': 'tournament',
    })


@subcounty_admin_required
def subcounty_phase_status_update(request, pk):
    sub_county = request.user.sub_county
    my_wards = Ward.objects.filter(sub_county=sub_county)
    phase = get_object_or_404(
        Phase, Q(pk=pk) & (Q(sub_county=sub_county) | Q(ward__in=my_wards))
    )

    if request.method == 'POST':
        new_status = request.POST.get('status')
        if new_status in Phase.Status.values:
            phase.status = new_status
            phase.save(update_fields=['status'])
            messages.success(request, f"Phase status updated to {phase.get_status_display()}.")
        else:
            messages.error(request, "Invalid status value.")

    return redirect('subcounty_phase_detail', phase_id=phase.pk)


@subcounty_admin_required
def subcounty_team_create(request, phase_pk):
    sub_county = request.user.sub_county
    my_wards = Ward.objects.filter(sub_county=sub_county)
    phase = get_object_or_404(
        Phase, Q(pk=phase_pk) & (Q(sub_county=sub_county) | Q(ward__in=my_wards)),
        stage=Phase.Stage.WARD,
    )

    if request.method == 'POST':
        csv_file = request.FILES.get('csv_file')

        if csv_file:
            # ---- Bulk CSV path ----
            try:
                decoded = csv_file.read().decode('utf-8-sig')
            except UnicodeDecodeError:
                messages.error(request, "Couldn't read that file — please upload a UTF-8 CSV.")
                return redirect('subcounty_team_create', phase_pk=phase.pk)

            reader = csv.DictReader(io.StringIO(decoded))
            created, skipped = 0, []

            for i, row in enumerate(reader, start=2):  # row 1 is the header
                name = (row.get('name') or '').strip()
                sport_name = (row.get('sport') or '').strip()
                ward_name = (row.get('ward') or '').strip()

                if not name or not sport_name or not ward_name:
                    skipped.append(f"Row {i}: missing name/sport/ward")
                    continue

                sport = Sport.objects.filter(name__iexact=sport_name, is_active=True).first()
                ward = my_wards.filter(name__iexact=ward_name).first()

                if not sport:
                    skipped.append(f"Row {i}: unknown sport '{sport_name}'")
                    continue
                if not ward:
                    skipped.append(f"Row {i}: '{ward_name}' isn't a ward in your sub-county")
                    continue
                if ward != phase.ward and phase.ward is not None:
                    skipped.append(f"Row {i}: '{ward_name}' doesn't match this phase's ward")
                    continue

                team, was_created = Team.objects.get_or_create(
                    name=name, sport=sport, ward=ward,
                    defaults={
                        'home_ground': (row.get('home_ground') or '').strip(),
                        'coach_name': (row.get('coach_name') or '').strip(),
                        'coach_phone': (row.get('coach_phone') or '').strip(),
                        'created_by': request.user,
                    }
                )
                if was_created:
                    log_audit_action(request.user, AuditLog.Action.CREATE, team, request=request)
                phase_entry, pe_created = PhaseEntry.objects.get_or_create(phase=phase, team=team)
                if pe_created:
                    log_audit_action(request.user, AuditLog.Action.CREATE, phase_entry, request=request)
                if was_created:
                    created += 1

            messages.success(request, f"{created} team(s) added from CSV.")
            if skipped:
                messages.warning(request, f"{len(skipped)} row(s) skipped: " + "; ".join(skipped[:5]) + (
                    "…" if len(skipped) > 5 else ""))
            return redirect('subcounty_phase_detail', phase_id=phase.pk)

        else:
            # ---- Single manual entry path ----
            form = SubcountyTeamForm(request.POST, sub_county=sub_county)
            if form.is_valid():
                team = form.save(commit=False)
                if phase.ward is not None and team.ward_id != phase.ward_id:
                    form.add_error('ward', "This team's ward must match the phase's ward.")
                else:
                    team.created_by = request.user
                    team.save()
                    log_audit_action(request.user, AuditLog.Action.CREATE, team, request=request)
                    phase_entry, pe_created = PhaseEntry.objects.get_or_create(phase=phase, team=team)
                    if pe_created:
                        log_audit_action(request.user, AuditLog.Action.CREATE, phase_entry, request=request)
                    messages.success(request, f'"{team.name}" was added.')
                    return redirect('subcounty_phase_detail', phase_id=phase.pk)
            # fall through to re-render with errors
    else:
        initial = {'ward': phase.ward} if phase.ward else {}
        form = SubcountyTeamForm(sub_county=sub_county, initial=initial)

    return render(request, 'subcounty/team_create.html', {
        'form': form,
        'phase': phase,
        'sub_county': sub_county,
    })


@subcounty_admin_required
def subcounty_manage_team(request, pk):
    sub_county = request.user.sub_county
    team = get_object_or_404(Team.objects.select_related('sport', 'ward'), pk=pk, ward__sub_county=sub_county)

    from_phase_id = request.GET.get('from_phase')
    phase = None
    if from_phase_id:
        my_wards = Ward.objects.filter(sub_county=sub_county)
        phase = Phase.objects.filter(
            Q(pk=from_phase_id) & (Q(sub_county=sub_county) | Q(ward__in=my_wards))
        ).select_related('tournament').first()

    # Resolve tournament context (matches superadmin logic)
    tournament = phase.tournament if phase else None
    if not tournament:
        first_entry = team.phase_entries.select_related('phase__tournament').first()
        tournament = first_entry.phase.tournament if first_entry else None

    if request.method == 'POST':
        action = request.POST.get('action')

        # Shared redirect URL setup
        redirect_url = reverse('subcounty_manage_team', kwargs={'pk': team.pk})
        if from_phase_id:
            redirect_url += f"?from_phase={from_phase_id}"

        if action == 'edit_team':
            name = request.POST.get('name', '').strip()
            if not name:
                messages.error(request, "Team name cannot be empty.")
            else:
                team.name = name
                team.home_ground = request.POST.get('home_ground', '').strip()
                team.coach_name = request.POST.get('coach_name', '').strip()
                team.coach_phone = request.POST.get('coach_phone', '').strip()
                team.save(update_fields=['name', 'home_ground', 'coach_name', 'coach_phone'])
                log_audit_action(request.user, AuditLog.Action.UPDATE, team, request=request)
                messages.success(request, "Team details updated successfully.")

        elif action == 'add_player':
            if not tournament:
                messages.error(request, "This team isn't entered into any tournament yet. Add it to a phase first.")
            else:
                player_name = request.POST.get('player_name', '').strip()
                jersey_raw = request.POST.get('jersey_number', '').strip()
                position = request.POST.get('position', '').strip()

                if not player_name or not jersey_raw:
                    messages.error(request, "Both name and jersey number are required.")
                else:
                    try:
                        jersey = int(jersey_raw)
                    except ValueError:
                        messages.error(request, "Jersey number must be a whole number.")
                    else:
                        if Player.objects.filter(team=team, tournament=tournament, jersey_number=jersey).exists():
                            messages.error(request, f"Jersey number #{jersey} is already taken on this team.")
                        else:
                            player = Player.objects.create(
                                team=team,
                                tournament=tournament,
                                name=player_name,
                                jersey_number=jersey,
                                position=position,
                                created_by=request.user,
                            )
                            log_audit_action(request.user, AuditLog.Action.CREATE, player, request=request)
                            messages.success(request, f'"{player_name}" added to the squad.')

        elif action == 'remove_player':
            player = get_object_or_404(Player, pk=request.POST.get('player_id'), team=team)
            player_name = player.name
            player.delete()
            log_audit_action(request.user, AuditLog.Action.DELETE, player, request=request)
            messages.success(request, f'"{player_name}" removed from the roster.')

        elif action == 'bulk_add_players':
            if not tournament:
                messages.error(request, "This team isn't entered into any tournament yet. Add it to a phase first.")
            else:
                csv_file = request.FILES.get('csv_file')
                if not csv_file:
                    messages.error(request, "Please choose a CSV file to upload.")
                elif not csv_file.name.lower().endswith('.csv'):
                    messages.error(request, "Please upload a file with a .csv extension.")
                else:
                    try:
                        decoded = csv_file.read().decode('utf-8-sig')
                    except UnicodeDecodeError:
                        messages.error(request, "Couldn't read that file. Please save it as UTF-8 CSV and try again.")
                    else:
                        reader = csv.DictReader(StringIO(decoded))
                        headers = {h.strip().lower() for h in (reader.fieldnames or [])}

                        if not {'name', 'jersey_number'}.issubset(headers):
                            messages.error(request, "CSV must include 'name' and 'jersey_number' columns.")
                        else:
                            valid_positions = {c[0] for c in Player.Position.choices}
                            existing_jerseys = set(
                                Player.objects.filter(team=team, tournament=tournament)
                                .values_list('jersey_number', flat=True)
                            )
                            seen_in_file = set()
                            to_create = []
                            errors = []

                            for i, row in enumerate(reader, start=2):  # Row 2 = first data row (row 1 is header)
                                row = {k.strip().lower(): (v or '').strip() for k, v in row.items()}
                                name = row.get('name', '')
                                jersey_raw = row.get('jersey_number', '')
                                position = row.get('position', '').upper()
                                national_id = row.get('national_id', '')

                                if not name or not jersey_raw:
                                    errors.append(f"Row {i}: missing name or jersey number.")
                                    continue
                                try:
                                    jersey = int(jersey_raw)
                                except ValueError:
                                    errors.append(f"Row {i}: jersey number '{jersey_raw}' is not a whole number.")
                                    continue
                                if jersey in existing_jerseys or jersey in seen_in_file:
                                    errors.append(f"Row {i}: jersey #{jersey} is already taken.")
                                    continue
                                if position and position not in valid_positions:
                                    errors.append(f"Row {i}: unknown position '{position}' - left blank.")
                                    position = ''

                                to_create.append(Player(
                                    team=team, tournament=tournament, name=name,
                                    jersey_number=jersey, position=position,
                                    national_id=national_id, created_by=request.user,
                                ))
                                seen_in_file.add(jersey)

                            if to_create:
                                Player.objects.bulk_create(to_create)
                                log_audit_action(
                                    request.user, AuditLog.Action.CREATE, team, request=request,
                                    changes={'bulk_players_added': len(to_create)}
                                )
                                messages.success(
                                    request,
                                    f"Added {len(to_create)} player{'s' if len(to_create) != 1 else ''} from CSV."
                                )
                            if errors:
                                preview = " | ".join(errors[:10])
                                more = f" (+{len(errors) - 10} more)" if len(errors) > 10 else ""
                                messages.warning(request, f"{len(errors)} row(s) skipped: {preview}{more}")

        return redirect(redirect_url)

    # Filtering team players based on determined tournament context
    tournament_filter = {'tournament': tournament} if tournament else {}
    players = Player.objects.filter(team=team, **tournament_filter).order_by('jersey_number', 'name')
    squad_goals = players.aggregate(total=Sum('goals'))['total'] or 0

    fixtures_qs = Fixture.objects.filter(
        Q(home_team=team) | Q(away_team=team)
    ).filter(
        Q(phase_sport__phase__sub_county=sub_county) | Q(phase_sport__phase__ward__sub_county=sub_county)
    ).select_related(
        'home_team', 'away_team', 'phase_sport__sport', 'phase_sport__phase', 'result'
    ).distinct().order_by('-kickoff_at', '-round_number')

    context = {
        'active_nav': 'tournament',
        'team': team,
        'phase': phase,
        'tournament': tournament,
        'players': players,
        'squad_goals': squad_goals,
        'positions': Player.Position.choices,
        'team_fixtures': fixtures_qs,
    }
    return render(request, 'subcounty/manage_team.html', context)


@subcounty_admin_required
def subcounty_add_fixture(request, phase_id):
    sub_county = request.user.sub_county
    my_wards = Ward.objects.filter(sub_county=sub_county)

    phase = get_object_or_404(
        Phase.objects.select_related('tournament', 'ward', 'sub_county'),
        Q(id=phase_id) & (Q(sub_county=sub_county) | Q(ward__in=my_wards))
    )

    if request.method == 'POST':
        form = FixtureForm(request.POST, phase=phase)
        if form.is_valid():
            form.save()
            messages.success(request, "Fixture successfully scheduled!")
            return redirect('subcounty_phase_detail', phase_id=phase.id)
    else:
        form = FixtureForm(phase=phase)

    context = {
        'active_nav': 'tournament',
        'sub_county': sub_county,
        'phase': phase,
        'form': form,
    }
    return render(request, 'subcounty/add_fixture.html', context)


@subcounty_admin_required
def SubcountyPhaseTeamSelectView(request, phase_pk):
    """For Sub-County / County / Final phases: pick from teams already qualified
    at the level below, instead of creating a new team."""
    phase = get_object_or_404(Phase, pk=phase_pk)

    if phase.stage == Phase.Stage.WARD:
        messages.error(request, 'Ward phases create teams directly — use "Add Team" instead.')
        return redirect('subbcounty_phase_detail', pk=phase.pk)

    feeder_entries = (
        PhaseEntry.objects
        .filter(phase__in=phase.feeder_phases)
        .exclude(team__phase_entries__phase=phase)  # already entered here
        .select_related('team', 'team__ward', 'phase')
        .order_by('team__name')
    )

    if request.method == 'POST':
        team_ids = request.POST.getlist('team_ids')
        if not team_ids:
            messages.error(request, 'Select at least one team to advance.')
            return redirect('subcounty_phase_team_select', phase_pk=phase.pk)

        created = 0
        for team_id in team_ids:
            source_entry = feeder_entries.filter(team_id=team_id).first()
            if not source_entry:
                continue
            phase_entry, was_created = PhaseEntry.objects.get_or_create(
                phase=phase, team_id=team_id,
                defaults={'promoted_from': source_entry, 'registered_by': request.user},
            )
            if was_created:
                log_audit_action(request.user, AuditLog.Action.CREATE, phase_entry, request=request)
                created += 1

        messages.success(request, f'{created} team{"s" if created != 1 else ""} advanced into {phase}.')
        return redirect('subcounty_phase_detail', pk=phase.pk)

    return render(request, 'subcounty/subcounty_team_select.html', {
        'phase': phase,
        'feeder_entries': feeder_entries,
        'active_nav': 'tournaments',
    })


@subcounty_admin_required
@login_required(login_url='login_admin')
@require_POST
def SubcountyPromoteTeamView(request, entry_id):
    entry = get_object_or_404(PhaseEntry.objects.select_related('phase__tournament', 'team'), pk=entry_id)
    lookup = entry.phase.next_phase_lookup

    if lookup is None:
        messages.error(request, f'{entry.team.name} is already at the final stage.')
        return redirect('phase_detail', pk=entry.phase.pk)

    try:
        next_phase = Phase.objects.get(tournament=entry.phase.tournament, **lookup)
    except Phase.DoesNotExist:
        messages.error(request, f'No matching phase exists yet for this tournament.')
        return redirect('subcounty_phase_detail', pk=entry.phase.pk)

    new_entry, created = PhaseEntry.objects.get_or_create(
        phase=next_phase, team=entry.team,
        defaults={'promoted_from': entry, 'registered_by': request.user},
    )
    if created:
        log_audit_action(
            request.user,
            AuditLog.Action.PROMOTE,
            obj=entry.team,
            changes={"from_phase": str(entry.phase), "to_phase": str(next_phase)},
            request=request
        )
        messages.success(request, f'{entry.team.name} promoted to {next_phase}.')
    else:
        messages.info(request, f'{entry.team.name} is already in {next_phase}.')

    return redirect('subcounty_phase_detail', pk=next_phase.pk)


@subcounty_admin_required
@login_required(login_url='login_admin')
def SubcountyResultEntryView(request, fixture_pk):
    fixture = get_object_or_404(
        Fixture.objects.select_related('home_team', 'away_team', 'phase_sport__phase__tournament'),
        pk=fixture_pk
    )
    tournament = fixture.phase_sport.phase.tournament
    home_players = Player.objects.filter(tournament=tournament, team=fixture.home_team).order_by('jersey_number')
    away_players = Player.objects.filter(tournament=tournament, team=fixture.away_team).order_by('jersey_number')

    result = Result.objects.filter(fixture=fixture).first()

    # Query existing Goals
    existing_goals = Goal.objects.filter(fixture=fixture).select_related('scorer', 'assisted_by') if result else []
    existing_home_goals = [g for g in existing_goals if g.scorer.team_id == fixture.home_team_id]
    existing_away_goals = [g for g in existing_goals if g.scorer.team_id == fixture.away_team_id]

    # Query existing Cards (Assuming Booking model)
    existing_cards = Card.objects.filter(fixture=fixture).select_related('player') if result else []
    existing_home_cards = [c for c in existing_cards if c.player.team_id == fixture.home_team_id]
    existing_away_cards = [c for c in existing_cards if c.player.team_id == fixture.away_team_id]

    if request.method == 'POST':
        home_score = int(request.POST.get('home_score') or 0)
        away_score = int(request.POST.get('away_score') or 0)

        with transaction.atomic():
            if result:
                result.home_score = home_score
                result.away_score = away_score
                result.save()
            else:
                result = Result.objects.create(
                    fixture=fixture, home_score=home_score, away_score=away_score, entered_by=request.user
                )

            fixture.status = Fixture.Status.COMPLETED
            fixture.save(update_fields=['status'])
            log_audit_action(request.user, AuditLog.Action.UPDATE, fixture, request=request)

            # --- Save Goals ---
            Goal.objects.filter(fixture=fixture).delete()
            scorer_ids = request.POST.getlist('scorer')
            assist_ids = request.POST.getlist('assisted_by')
            minutes = request.POST.getlist('minute')
            for scorer_id, assist_id, minute in zip(scorer_ids, assist_ids, minutes):
                if not scorer_id:
                    continue
                Goal.objects.create(
                    fixture=fixture, scorer_id=scorer_id, assisted_by_id=assist_id or None, minute=minute or None,
                )

            # --- Save Cards / Bookings ---
            Card.objects.filter(fixture=fixture).delete()
            card_players = request.POST.getlist('card_player')
            card_types = request.POST.getlist('card_type')
            card_minutes = request.POST.getlist('card_minute')
            for player_id, c_type, c_minute in zip(card_players, card_types, card_minutes):
                if not player_id or not c_type:
                    continue
                Card.objects.create(
                    fixture=fixture,
                    player_id=player_id,
                    card_type=c_type,
                    minute=c_minute or None
                )

        messages.success(request, f'Result saved for {fixture}.')
        return redirect('subcounty_phase_detail', phase_id=fixture.phase_sport.phase_id)

    return render(request, 'subcounty/subcountyadmin_result_entry.html', {
        'fixture': fixture,
        'result': result,
        'home_players': home_players,
        'away_players': away_players,
        'existing_home_goals': existing_home_goals,
        'existing_away_goals': existing_away_goals,
        'existing_home_cards': existing_home_cards,
        'existing_away_cards': existing_away_cards,
        'active_nav': 'phases',
    })

@subcounty_admin_required
@login_required(login_url='login_admin')
def Subcountyedit_fixture_view(request, pk):
    fixture = get_object_or_404(Fixture, pk=pk)

    if request.method == 'POST':
        form = FixtureEditForm(request.POST, instance=fixture)
        if form.is_valid():
            form.save()
            return redirect('subcounty_phase_detail', fixture.phase_sport.pk)
    else:
        form = FixtureEditForm(instance=fixture)

    return render(request, 'subcounty/subcounty_edit_fixture.html', {'form': form, 'fixture': fixture})


@subcounty_admin_required
def subcounty_newsroom(request):
    if not hasattr(request.user, 'sub_county') or not request.user.sub_county:
        messages.error(request, "Access denied. You must be a Ward Admin to access this page.")
        return redirect('login')

    ward = request.user.ward
    sub_county = ward.sub_county

    phases = Phase.objects.filter(
        Q(ward=ward) | Q(sub_county=sub_county)
    ).select_related('tournament').distinct()

    posts = NewsPost.objects.filter(
        Q(sub_county__isnull=True) |  # County-wide posts from County Admin
        Q(sub_county=sub_county) |  # Local Sub-County posts
        Q(phase__in=phases)  # Posts tied directly to their local phases
    ).select_related('author', 'sub_county', 'phase__tournament').prefetch_related(
        'comments__author'
    ).distinct().order_by('-published_at')

    context = {
        'active_nav': 'newsroom',
        'ward': ward,
        'sub_county': sub_county,
        'phases': phases,
        'posts': posts,
    }
    return render(request, 'subcounty/newsroom.html', context)


@subcounty_admin_required
def subcounty_news_create(request):
    if not hasattr(request.user, 'sub_county') or not request.user.sub_county:
        messages.error(request, "Access unauthorized.")
        return redirect('subcounty_newsroom')

    if request.method == 'POST':
        title = request.POST.get('title')
        tag = request.POST.get('tag')
        phase_id = request.POST.get('phase')
        body = request.POST.get('body')

        ward = request.user.ward
        sub_county = ward.sub_county

        # Initialize post scoped to the admin's local Sub-County context
        post = NewsPost(
            title=title,
            tag=tag,
            body=body,
            author=request.user,
            sub_county=sub_county
        )

        # If a specific phase scope was chosen, validate its access limits
        if phase_id:
            phase = get_object_or_404(Phase, id=phase_id)
            if phase.ward == ward or phase.sub_county == sub_county:
                post.phase = phase
            else:
                messages.error(request, "Security breach: Selected phase falls outside your ward jurisdiction.")
                return redirect('subcounty_newsroom')

        post.save()
        messages.success(request, "News post published successfully!")

    return redirect('subcounty_newsroom')


@subcounty_admin_required
def subcountynews_edit(request, pk):
    if not hasattr(request.user, 'sub_county') or not request.user.sub_county:
        messages.error(request, "Access unauthorized.")
        return redirect('subcounty_newsroom')

    post = get_object_or_404(NewsPost, pk=pk)
    ward = request.user.ward
    sub_county = ward.sub_county

    # Authority check: only author or matching sub-county admin can modify
    if post.author != request.user and post.sub_county != sub_county:
        messages.error(request, "You do not have permission to modify this post.")
        return redirect('subcounty_newsroom')

    if request.method == 'POST':
        post.title = request.POST.get('title')
        post.tag = request.POST.get('tag')
        post.body = request.POST.get('body')

        phase_id = request.POST.get('phase')
        if phase_id:
            phase = get_object_or_404(Phase, id=phase_id)
            if phase.ward == ward or phase.sub_county == sub_county:
                post.phase = phase
            else:
                messages.error(request, "Invalid phase scope selected.")
                return redirect('subcounty_newsroom')
        else:
            post.phase = None  # Reset to general Ward/Sub-County scope

        post.save()
        messages.success(request, "News post updated successfully!")

    return redirect('subcounty_newsroom')


@subcounty_admin_required
def subcountynews_delete(request, pk):
    if not hasattr(request.user, 'sub_county') or not request.user.sub_county:
        messages.error(request, "Access unauthorized.")
        return redirect('subcounty_newsroom')

    post = get_object_or_404(NewsPost, pk=pk)
    ward = request.user.ward
    sub_county = ward.sub_county

    # Authority check: only author or matching sub-county admin can delete
    if post.author != request.user and post.sub_county != sub_county:
        messages.error(request, "Permission denied to delete this post.")
        return redirect('subcounty_newsroom')

    if request.method == 'POST':
        post.delete()
        messages.success(request, "News post deleted successfully!")

    return redirect('subcounty_newsroom')


@subcounty_admin_required
def subcounty_news_comment_delete(request, pk):
    if not hasattr(request.user, 'sub_county') or not request.user.sub_county:
        messages.error(request, "Access unauthorized.")
        return redirect('subcounty_newsroom')

    comment = get_object_or_404(NewsComment, pk=pk)
    ward = request.user.ward
    sub_county = ward.sub_county

    # Permissions rules:
    # 1. User wrote the comment
    # 2. User is author of the NewsPost
    # 3. User is a local Admin overseeing the post's sub-county scope
    is_comment_author = comment.author == request.user
    is_post_author = comment.post.author == request.user
    is_local_moderator = comment.post.sub_county == sub_county

    if is_comment_author or is_post_author or is_local_moderator:
        comment.delete()
        messages.success(request, "Comment removed successfully.")
    else:
        messages.error(request, "You do not have permission to delete this comment.")

    return redirect('subcounty_newsroom')


@subcounty_admin_required
def subcounty_account_settings(request):
    if not hasattr(request.user, 'sub_county') or not request.user.sub_county:
        messages.error(request, "Access denied. Only Sub County Admins can access this panel.")
        return redirect('login')

    ward = request.user.ward

    # Initialize forms
    profile_form = WardAdminProfileForm(instance=request.user)
    password_form = PasswordChangeForm(user=request.user)

    if request.method == 'POST':
        form_type = request.POST.get('form_type')

        if form_type == 'profile':
            profile_form = WardAdminProfileForm(request.POST, instance=request.user)
            if profile_form.is_valid():
                profile_form.save()
                messages.success(request, "Your profile settings have been updated.")
                return redirect('ward_account_settings')
            else:
                messages.error(request, "Please correct the profile errors below.")

        elif form_type == 'password':
            password_form = PasswordChangeForm(user=request.user, data=request.POST)
            if password_form.is_valid():
                user = password_form.save()
                # Crucial step to prevent logging the user out after a password change
                update_session_auth_hash(request, user)
                messages.success(request, "Your password was changed successfully.")
                return redirect('subcounty_account_settings')
            else:
                messages.error(request, "Please correct the password validation errors below.")

    context = {
        'active_nav': 'account',
        'ward': ward,
        'profile_form': profile_form,
        'password_form': password_form,
    }
    return render(request, 'subcounty/account.html', context)


@subcounty_admin_required
@login_required
def subcounty_tournament(request):
    if not hasattr(request.user, 'sub_county') or not request.user.sub_county:
        return redirect('login')
    sub_county = request.user.sub_county

    if request.method == 'POST':
        form = SubcountyTournamentForm(request.POST, user=request.user)
        if form.is_valid():
            tournament = form.save(commit=False)
            tournament.sub_county = sub_county
            tournament.created_by = request.user
            tournament.save()
            form.save_m2m()

            phase = Phase.objects.create(
                tournament=tournament,
                sub_county=sub_county,
                stage='sub_county',
                status=tournament.status,
            )

            log_audit_action(request.user, AuditLog.Action.CREATE, tournament, request=request)
            messages.success(request, f'"{tournament.name}" was created for {sub_county.name} Ward.')
            return redirect(f"{request.path}?phase_id={phase.pk}")
        else:
            print('Ward tournament form errors:', form.errors.as_json())
    else:
        form = SubcountyTournamentForm(user=request.user)
        my_wards = Ward.objects.filter(sub_county=sub_county)

        # 1. Fetch tournaments that have phases involving this sub-county or its wards
        tournaments = Tournament.objects.filter(
            Q(phases__sub_county=sub_county) | Q(phases__ward__in=my_wards)
        ).distinct().order_by('-season', 'name')

        # Apply search/filters
        search_query = request.GET.get('search', '')
        if search_query:
            tournaments = tournaments.filter(
                Q(name__icontains=search_query) | Q(season__icontains=search_query)
            ).distinct()

        # 2. Count metrics specifically for this Sub-County scope
        active_ward_phases = Phase.objects.filter(
            stage=Phase.Stage.WARD,
            ward__in=my_wards,
            status=Phase.Status.ONGOING
        ).count()

        active_subcounty_phases = Phase.objects.filter(
            stage=Phase.Stage.SUB_COUNTY,
            sub_county=sub_county,
            status=Phase.Status.ONGOING
        ).count()

        total_wards_count = my_wards.count()

        context = {
            'form': form,
            'active_nav': 'tournament',
            'sub_county': sub_county,
            'tournaments': tournaments,
            'active_ward_phases': active_ward_phases,
            'active_subcounty_phases': active_subcounty_phases,
            'total_wards_count': total_wards_count,
            'search_query': search_query,
        }
    return render(request, 'subcounty/tournament.html', context)
