import json
import os

from django.conf import settings
from django.contrib.staticfiles import finders
from django.db import models
from django.db.models import Count, Sum, F, Q, Prefetch
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render
from django.utils import timezone
from django.utils.timesince import timesince
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from accounts.models import Phase, PhaseSport, Sport, SubCounty, NewsPost, Tournament, Fixture, Ward, Team, \
    Player, NewsComment, Goal


# Helper Functions
def _compute_team_stats(fixtures):
    """Computes a league table from a list/queryset of Fixture objects
    (with select_related('home_team', 'away_team', 'result') already applied).
    Returns a list of dicts sorted by points, then goal difference, then goals for."""
    stats = {}

    def _get(team):
        if team.id not in stats:
            stats[team.id] = {
                'team': team, 'played': 0, 'won': 0, 'drawn': 0, 'lost': 0,
                'goals_for': 0, 'goals_against': 0, 'points': 0,
            }
        return stats[team.id]

    # seed every team that appears in the fixture list, even before any results exist
    for f in fixtures:
        _get(f.home_team)
        _get(f.away_team)

    for f in fixtures:
        result = getattr(f, 'result', None)
        if not result:
            continue
        home = _get(f.home_team)
        away = _get(f.away_team)

        home['played'] += 1
        away['played'] += 1
        home['goals_for'] += result.home_score
        home['goals_against'] += result.away_score
        away['goals_for'] += result.away_score
        away['goals_against'] += result.home_score

        if result.home_score > result.away_score:
            home['won'] += 1
            home['points'] += 3
            away['lost'] += 1
        elif result.home_score < result.away_score:
            away['won'] += 1
            away['points'] += 3
            home['lost'] += 1
        else:
            home['drawn'] += 1
            home['points'] += 1
            away['drawn'] += 1
            away['points'] += 1

    rows = list(stats.values())
    for r in rows:
        r['goal_difference'] = r['goals_for'] - r['goals_against']

    rows.sort(key=lambda r: (-r['points'], -r['goal_difference'], -r['goals_for'], r['team'].name))
    return rows


def _build_bracket_rounds(fixtures):
    """Groups fixtures by round_number, preserving order. Returns [(round_number, [fixtures]), ...]"""
    rounds = {}
    for f in fixtures:
        rounds.setdefault(f.round_number, []).append(f)
    return sorted(rounds.items())


def _draw_context(selected_phase):
    """Feeds the Draw tab. Bracket is computed live from Fixture/Result data."""
    print(
        f"\n[DRAW DEBUG] === _draw_context called with selected_phase = {selected_phase!r} (id={getattr(selected_phase, 'id', None)}) ===")

    if not selected_phase:
        print("[DRAW DEBUG] selected_phase is falsy -> returning empty queryset")
        return {'draw_phase_sports': PhaseSport.objects.none()}

    phase_sports = (
        PhaseSport.objects
        .filter(phase=selected_phase)
        .select_related('sport')
        .prefetch_related(
            Prefetch(
                'fixtures',
                queryset=Fixture.objects
                .select_related('home_team', 'away_team', 'result', 'group')
                .order_by('round_number', 'kickoff_at'),
            ),
        )
        .order_by('sport__name')
    )

    print(f"[DRAW DEBUG] raw phase_sports queryset SQL: {phase_sports.query}")
    phase_sports = list(phase_sports)
    print(f"[DRAW DEBUG] phase_sports count after list(): {len(phase_sports)}")

    for ps in phase_sports:
        print(
            f"\n[DRAW DEBUG] --- Processing PhaseSport id={ps.id} sport={ps.sport.name} format={ps.fixture_format!r} ---")
        all_fixtures = list(ps.fixtures.all())
        print(f"[DRAW DEBUG] all_fixtures count: {len(all_fixtures)}")

        print(
            f"[DRAW DEBUG] comparing ps.fixture_format ({ps.fixture_format!r}) == PhaseSport.Format.KNOCKOUT ({PhaseSport.Format.KNOCKOUT!r}) -> {ps.fixture_format == PhaseSport.Format.KNOCKOUT}")
        print(
            f"[DRAW DEBUG] comparing ps.fixture_format ({ps.fixture_format!r}) == PhaseSport.Format.GROUP_KNOCKOUT ({PhaseSport.Format.GROUP_KNOCKOUT!r}) -> {ps.fixture_format == PhaseSport.Format.GROUP_KNOCKOUT}")

        if ps.fixture_format == PhaseSport.Format.KNOCKOUT:
            print("[DRAW DEBUG] -> matched KNOCKOUT branch")
            ps.group_stage_complete = True
            ps.bracket_rounds = _build_bracket_rounds(all_fixtures)
            print(
                f"[DRAW DEBUG] bracket_rounds built: {len(ps.bracket_rounds)} rounds -> {[(rn, len(fx)) for rn, fx in ps.bracket_rounds]}")

        elif ps.fixture_format == PhaseSport.Format.GROUP_KNOCKOUT:
            print("[DRAW DEBUG] -> matched GROUP_KNOCKOUT branch")
            group_fixtures = [f for f in all_fixtures if f.group_id]
            knockout_fixtures = [f for f in all_fixtures if not f.group_id]
            ps.group_stage_complete = bool(group_fixtures) and all(hasattr(f, 'result') for f in group_fixtures)
            ps.bracket_rounds = _build_bracket_rounds(knockout_fixtures) if ps.group_stage_complete else []
            print(
                f"[DRAW DEBUG] group_fixtures={len(group_fixtures)} knockout_fixtures={len(knockout_fixtures)} group_stage_complete={ps.group_stage_complete}")

        else:
            print(
                f"[DRAW DEBUG] -> fell to ELSE branch (format did not match either constant) - bracket_rounds set to []")
            ps.group_stage_complete = None
            ps.bracket_rounds = []

    print(f"\n[DRAW DEBUG] === returning draw_phase_sports with {len(phase_sports)} items ===\n")
    return {'draw_phase_sports': phase_sports}


# Create your views here.
def splash(request):
    return render(request, 'splash.html')


def _resolve_phase(tournament, phase_id):
    phases = Phase.objects.filter(tournament=tournament).select_related('ward', 'sub_county')

    selected_phase = None
    if phase_id:
        selected_phase = phases.filter(pk=phase_id).first()

    if not selected_phase:
        # "most recent" = the phase currently ongoing, else the most recently started one
        selected_phase = (
                phases.filter(status=Phase.Status.ONGOING).order_by('-start_date', '-pk').first()
                or phases.order_by('-start_date', '-pk').first()
        )
    return selected_phase, phases


def _news_sub_county_id(selected_phase):
    """News scoping follows the phase: ward phases inherit their parent
    sub-county's news, county/final phases see county-wide news only."""
    if not selected_phase:
        return ''
    if selected_phase.sub_county_id:
        return str(selected_phase.sub_county_id)
    if selected_phase.ward_id:
        return str(selected_phase.ward.sub_county_id)
    return ''


def _news_context(selected_phase):
    """
    Returns news for the specific phase OR county-wide news (phase is None).
    """
    # Get news for this specific phase OR news that has no phase (County-Wide)
    news = NewsPost.objects.filter(
        models.Q(phase=selected_phase) | models.Q(phase__isnull=True)
    ).select_related('author', 'phase', 'phase__tournament').order_by('-published_at')

    return {'news_posts': news}


def _news_queryset_for_phase(tournament, selected_phase):
    """
    Scope rules:
      - County-wide posts (no phase, no sub_county) always show.
      - If selected_phase is a Ward phase: also show Sub-County-wide posts for
        that ward's sub-county, plus posts tied to this exact ward phase.
      - If selected_phase is a Sub-County phase: also show posts tied to this
        exact sub-county phase (ward-level posts from below don't bubble up).
      - If selected_phase is County/Final (or None, i.e. tournament overview):
        show county-wide posts plus anything tied to a phase in this tournament.
    """
    county_wide = Q(phase__isnull=True, sub_county__isnull=True)

    if selected_phase is None:
        # Tournament overview: county-wide + every phase-linked post in this tournament
        scoped = Q(phase__tournament=tournament)
        return NewsPost.objects.filter(county_wide | scoped)

    if selected_phase.stage == Phase.Stage.WARD:
        sub_county = selected_phase.ward.sub_county
        sub_county_wide = (
                Q(phase__stage=Phase.Stage.SUB_COUNTY, phase__sub_county=sub_county) |
                Q(phase__isnull=True, sub_county=sub_county)
        )
        this_ward = Q(phase=selected_phase)
        return NewsPost.objects.filter(county_wide | sub_county_wide | this_ward)

    if selected_phase.stage == Phase.Stage.SUB_COUNTY:
        this_sub_county = (
                Q(phase=selected_phase) |
                Q(phase__isnull=True, sub_county=selected_phase.sub_county)
        )
        return NewsPost.objects.filter(county_wide | this_sub_county)

    # COUNTY or FINAL stage
    return NewsPost.objects.filter(county_wide | Q(phase=selected_phase))


def client_dashboard(request, pk):
    tournament = get_object_or_404(Tournament.objects.prefetch_related('sports'), pk=pk)
    phase_id = request.GET.get('phase') or ''
    selected_phase, phases = _resolve_phase(tournament, phase_id)

    print(f"\n[DASHBOARD DEBUG] selected_phase = {selected_phase!r} (id={getattr(selected_phase, 'id', None)})")

    # --- Stage Progress Logic ---
    stage_progress = []
    for stage_value, stage_label in Phase.Stage.choices:
        stage_phases = phases.filter(stage=stage_value)
        if not stage_phases.exists():
            status = 'upcoming'
        elif stage_phases.filter(status=Phase.Status.ONGOING).exists():
            status = 'active'
        elif all(p.status == Phase.Status.COMPLETED for p in stage_phases):
            status = 'done'
        else:
            status = 'upcoming'
        stage_progress.append({'value': stage_value, 'label': stage_label, 'status': status})

    # --- Filter Teams by selected_phase ---
    if selected_phase:
        teams = Team.objects.filter(phase_entries__phase=selected_phase).distinct()
    else:
        teams = Team.objects.filter(phase_entries__phase__tournament=tournament).distinct()
    teams = (
        teams.annotate(total_goals=Sum('players__goals'), player_count=Count('players', distinct=True))
        .order_by('-total_goals', 'name')
    )

    # --- Filter Players by the Teams in the selected_phase ---
    if selected_phase:
        players = (
            Player.objects.filter(team__in=teams)
            .select_related('team')
            .order_by('-goals', 'jersey_number')
        )
    else:
        players = (
            Player.objects.filter(tournament=tournament, team__in=teams)
            .select_related('team')
            .order_by('-goals', 'jersey_number')
        )

    # --- Fixtures Logic ---
    fixtures_ctx = _fixtures_context(selected_phase)
    fixtures_qs = fixtures_ctx.get('fixtures')
    fixture_dates = []
    has_unscheduled = False
    if fixtures_qs is not None:
        fixture_dates = sorted({f.kickoff_at.date() for f in fixtures_qs if f.kickoff_at})
        has_unscheduled = fixtures_qs.filter(kickoff_at__isnull=True).exists()
    live_count = 0
    if 'fixtures' in fixtures_ctx:
        live_count = fixtures_ctx['fixtures'].filter(status='live').count()

    # --- Next Match Logic ---
    next_fixture = None
    if 'fixtures' in fixtures_ctx:
        next_fixture = fixtures_ctx['fixtures'].filter(
            status='scheduled',
            kickoff_at__gt=timezone.now()
        ).order_by('kickoff_at').first()

    # --- News Logic ---
    news_posts = (
        _news_queryset_for_phase(tournament, selected_phase)
        .select_related('author', 'phase', 'phase__tournament', 'phase__ward', 'phase__sub_county', 'sub_county')
        .distinct()
        .order_by('-published_at')
    )
    news_count = news_posts.count()

    # --- Draw tab data ---
    draw_ctx = _draw_context(selected_phase)
    print(f"[DASHBOARD DEBUG] draw_phase_sports count = {len(draw_ctx['draw_phase_sports'])}")
    for ps in draw_ctx['draw_phase_sports']:
        print(
            f"[DASHBOARD DEBUG]   -> {ps.sport.name} | format={ps.fixture_format} | bracket_rounds={len(ps.bracket_rounds)}")
    today = timezone.localdate()
    today_count = 0
    if fixtures_qs is not None:
        today_count = fixtures_qs.filter(kickoff_at__date=today).count()

    # --- Assemble Final Context ---
    context = {
        'tournament': tournament,
        'today': today,
        'today_count': today_count,
        'stage_progress': stage_progress,
        'phases': phases,
        'selected_phase': selected_phase,
        'teams': teams,
        'players': players,
        'live_count': live_count,
        'next_fixture': next_fixture,
        'news_count': news_count,
        'news_posts': news_posts,
        'fixture_dates': fixture_dates,
        'has_unscheduled': has_unscheduled,
        **_standings_context(selected_phase),
        **draw_ctx,
        **fixtures_ctx,
    }
    return render(request, 'client_dashboard.html', context)


def client_tournament_standings_poll(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk)
    selected_phase, _ = _resolve_phase(tournament, request.GET.get('phase') or '')

    ctx = _standings_context(selected_phase)
    phase_sports = ctx['phase_sports']

    def serialize_rows(rows):
        return [
            {
                'team_id': r['team'].id,
                'team_name': r['team'].name,
                'played': r['played'],
                'won': r['won'],
                'drawn': r['drawn'],
                'lost': r['lost'],
                'goals_for': r['goals_for'],
                'goals_against': r['goals_against'],
                'goal_difference': r['goal_difference'],
                'points': r['points'],
            }
            for r in rows
        ]

    data = []
    for ps in phase_sports:
        entry = {
            'id': ps.id,
            'sport_name': ps.sport.name,
            'fixture_format': ps.fixture_format,
            'groups': [],
            'standings': [],
        }
        if ps.fixture_format == PhaseSport.Format.GROUP_KNOCKOUT:
            for group in ps.groups.all():
                entry['groups'].append({
                    'id': group.id,
                    'name': group.name,
                    'standings': serialize_rows(group.computed_standings),
                })
        else:
            entry['standings'] = serialize_rows(getattr(ps, 'computed_standings', []))
        data.append(entry)

    return JsonResponse({'phase_sports': data})


def client_tournament_fixtures_poll(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk)
    selected_phase, _ = _resolve_phase(tournament, request.GET.get('phase') or '')

    ctx = _fixtures_context(selected_phase)
    fixtures = ctx.get('fixtures')

    data = []
    dates_set = set()
    has_unscheduled = False

    if fixtures is not None:
        for f in fixtures:
            result = getattr(f, 'result', None)
            if f.kickoff_at:
                dates_set.add(f.kickoff_at.date())
            else:
                has_unscheduled = True
            data.append({
                'id': f.id,
                'sport_name': f.phase_sport.sport.name if f.phase_sport_id else '',
                'date': f.kickoff_at.strftime('%Y-%m-%d') if f.kickoff_at else 'unscheduled',
                'time': f.kickoff_at.strftime('%H:%M') if f.kickoff_at else None,
                'venue': f.venue,
                'status': f.status,
                'home_team': f.home_team.name,
                'away_team': f.away_team.name,
                'home_score': result.home_score if result else None,
                'away_score': result.away_score if result else None,
            })

    dates_sorted = sorted(dates_set)
    return JsonResponse({
        'fixtures': data,
        'dates': [
            {'iso': d.strftime('%Y-%m-%d'), 'day': d.strftime('%a'), 'label': d.strftime('%d %b')}
            for d in dates_sorted
        ],
        'has_unscheduled': has_unscheduled,
    })


def client_tournament_news_poll(request, pk):
    tournament = get_object_or_404(Tournament, pk=pk)
    selected_phase, _ = _resolve_phase(tournament, request.GET.get('phase') or '')

    ctx = _news_context(_news_sub_county_id(selected_phase))
    news_posts = ctx.get('news_posts')

    data = []
    if news_posts is not None:
        for post in news_posts:
            comments = list(post.comments.all())
            data.append({
                'id': post.id,
                'tag_display': post.get_tag_display(),
                'title': post.title,
                'body_preview': ' '.join(post.body.split()[:35]) + ('...' if len(post.body.split()) > 35 else ''),
                'author': post.author.get_full_name() if post.author else 'County Sports Dept.',
                'scope': f"{post.phase.tournament.name} ({post.phase.scope_label})" if post.phase else 'County-Wide',
                'published_ago': timesince(post.published_at) + ' ago',
                'like_count': post.like_count,
                'comments': [
                    {
                        'id': c.id,
                        'author': c.display_author_name(),
                        'content': c.content,
                        'created_ago': timesince(c.created_at) + ' ago',
                    }
                    for c in comments
                ],
            })

    return JsonResponse({'news_posts': data})


def client_lobby(request):
    tournaments = (
        Tournament.objects
        .filter(status__in=[Tournament.Status.ONGOING, Tournament.Status.UPCOMING])
        .prefetch_related('sports')
        .annotate(
            phase_count=Count('phases', distinct=True),
            team_count=Count('phases__entries', distinct=True),
        )
        .order_by('status', 'start_date')
    )

    news_posts = (
        NewsPost.objects
        .select_related('sub_county', 'author', 'phase', 'phase__tournament')
        .order_by('-published_at')[:20]
    )

    context = {
        'tournaments': tournaments,
        'sports': Sport.objects.filter(is_active=True).order_by('name'),
        'sub_counties': SubCounty.objects.order_by('name'),
        'news_posts': news_posts,
    }
    return render(request, 'client_lobby.html', context)


def _standings_context(selected_phase):
    """Feeds the Tables tab. Standings are computed live from Fixture/Result data
    rather than a stored Standing model, so there's nothing to keep in sync."""
    if not selected_phase:
        return {'phase_sports': PhaseSport.objects.none()}

    phase_sports = (
        PhaseSport.objects
        .filter(phase=selected_phase)
        .select_related('sport')
        .prefetch_related(
            'groups',
            Prefetch(
                'fixtures',
                queryset=Fixture.objects.select_related('home_team', 'away_team', 'result', 'group'),
            ),
        )
        .order_by('sport__name')
    )

    phase_sports = list(phase_sports)  # force evaluation so we can attach computed attrs
    for ps in phase_sports:
        all_fixtures = list(ps.fixtures.all())
        if ps.fixture_format == PhaseSport.Format.GROUP_KNOCKOUT:
            for group in ps.groups.all():
                group_fixtures = [f for f in all_fixtures if f.group_id == group.id]
                group.computed_standings = _compute_team_stats(group_fixtures)
        else:
            ps.computed_standings = _compute_team_stats(all_fixtures)

    return {'phase_sports': phase_sports}


def client_phase_detail(request, phase_pk):
    """Entry point from the lobby card — resolves a Phase back to its Tournament
    dashboard, pre-filtered to that phase's ward/sub-county."""
    phase = get_object_or_404(Phase.objects.select_related('tournament', 'ward', 'sub_county'), pk=phase_pk)
    params = {}
    if phase.ward_id:
        params['ward'] = phase.ward_id
        params['sub_county'] = phase.ward.sub_county_id
    elif phase.sub_county_id:
        params['sub_county'] = phase.sub_county_id

    from django.urls import reverse
    from urllib.parse import urlencode
    url = reverse('client_tournament_detail', args=[phase.tournament_id])
    if params:
        url = f"{url}?{urlencode(params)}"
    return redirect(url)


def _fixtures_context(selected_phase):
    fixtures = (
        Fixture.objects.filter(phase_sport__phase=selected_phase)
        .select_related('home_team', 'away_team', 'phase_sport__sport', 'result')
        .order_by('round_number', 'kickoff_at')
        if selected_phase else Fixture.objects.none()
    )
    return {'fixtures': fixtures, 'selected_phase': selected_phase}


def client_tournament_section(request, pk):
    tournament = get_object_or_404(Tournament.objects.prefetch_related('sports'), pk=pk)
    sub_county_id = request.GET.get('sub_county') or ''
    ward_id = request.GET.get('ward') or ''
    selected_phase, phases = _resolve_phase(tournament, sub_county_id, ward_id)

    stage_progress = []
    for stage_value, stage_label in Phase.Stage.choices:
        stage_phases = phases.filter(stage=stage_value)
        if not stage_phases.exists():
            status = 'upcoming'
        elif stage_phases.filter(status=Phase.Status.ONGOING).exists():
            status = 'active'
        elif all(p.status == Phase.Status.COMPLETED for p in stage_phases):
            status = 'done'
        else:
            status = 'upcoming'
        stage_progress.append({'value': stage_value, 'label': stage_label, 'status': status})

    teams = (
        Team.objects.filter(phase_entries__phase__tournament=tournament)
        .distinct().order_by('name')
    )
    players = (
        Player.objects.filter(tournament=tournament, team__in=teams)
        .select_related('team')
        .order_by('team_id', '-goals', 'jersey_number')
    )

    context = {
        'tournament': tournament,
        'stage_progress': stage_progress,
        'sub_counties': SubCounty.objects.order_by('name'),
        'wards': Ward.objects.filter(sub_county_id=sub_county_id).order_by(
            'name') if sub_county_id else Ward.objects.none(),
        'sub_county_id': sub_county_id,
        'ward_id': ward_id,
        'teams': teams,
        'players': players,
        **_standings_context(selected_phase),
        **_fixtures_context(selected_phase),
        **_news_context(sub_county_id),
    }
    return render(request, 'client/partials/_tournament_content.html', context)


@csrf_exempt
@require_POST
def like_news_post(request, post_id):
    """
    Endpoint to increment the like count of a specific NewsPost.
    """
    post = get_object_or_404(NewsPost, id=post_id)

    # Use F() to prevent race conditions when multiple users like simultaneously
    NewsPost.objects.filter(id=post_id).update(like_count=F('like_count') + 1)

    # Refresh from db to get the updated integer value to send back
    post.refresh_from_db()

    return JsonResponse({
        'status': 'success',
        'like_count': post.like_count
    })


@csrf_exempt
@require_POST
def add_news_comment(request, post_id):
    """
    Endpoint to submit a new comment.
    """
    post = get_object_or_404(NewsPost, id=post_id)

    # Handle both JSON payloads and standard form data
    try:
        data = json.loads(request.body)
        content = data.get('content', '').strip()
        guest_name = data.get('guest_name', 'Anonymous Viewer').strip()
    except json.JSONDecodeError:
        content = request.POST.get('content', '').strip()
        guest_name = request.POST.get('guest_name', 'Anonymous Viewer').strip()

    if not content:
        return JsonResponse({'status': 'error', 'message': 'Comment content is required.'}, status=400)

    # Create the comment
    comment = NewsComment(post=post, content=content)

    if request.user.is_authenticated:
        comment.author = request.user
    else:
        comment.guest_name = guest_name

    comment.save()

    return JsonResponse({
        'status': 'success',
        'comment': {
            'id': comment.id,
            'author': comment.display_author_name(),
            'content': comment.content,
            'created_at': comment.created_at.strftime('%Y-%m-%d %H:%M:%S')
        }
    })


@require_GET
def poll_news_stats(request, post_id):
    """
    Endpoint for the frontend to periodically poll the latest likes and comments.
    """
    post = get_object_or_404(NewsPost, id=post_id)

    # Grab the 5 most recent comments to update the UI dynamically
    recent_comments = post.comments.all()[:5]

    comments_data = [{
        'id': c.id,
        'author': c.display_author_name(),
        'content': c.content,
        'created_at': c.created_at.strftime('%Y-%m-%d %H:%M:%S')
    } for c in recent_comments]

    return JsonResponse({
        'status': 'success',
        'like_count': post.like_count,
        'latest_comments': comments_data
    })


def fixture_detail(request, pk):
    fixture = get_object_or_404(
        Fixture.objects
        .select_related('home_team', 'away_team', 'result', 'phase_sport__sport', 'phase_sport__phase', 'group')
        .prefetch_related(
            Prefetch('goals',
                     queryset=Goal.objects.select_related('scorer', 'assisted_by').order_by('minute', 'created_at'))
        ),
        pk=pk
    )

    current_tournament = fixture.phase_sport.phase.tournament

    # Query all registered players for both teams
    home_players = list(fixture.home_team.players.filter(tournament=current_tournament).order_by('jersey_number'))
    away_players = list(fixture.away_team.players.filter(tournament=current_tournament).order_by('jersey_number'))

    def build_lineup_and_bench(players):
        """
        Splits a flat list of players into a 4-3-3 formation and places the rest on the bench.
        Formation definition: 1 GK, 4 DEF, 3 MID, 3 ATT = 11 starters.
        """
        # Separate pool by position
        gks = [p for p in players if p.position == 'GK']
        defs = [p for p in players if p.position == 'DEF']
        mids = [p for p in players if p.position == 'MID']
        atts = [p for p in players if p.position == 'ATT']

        # Slice the starters based on a 4-3-3 formation rule
        starting_gk = gks[:1]
        starting_def = defs[:4]
        starting_mid = mids[:3]
        starting_att = atts[:3]

        # Everything left over goes to the bench
        bench = gks[1:] + defs[4:] + mids[3:] + atts[3:]

        # Sort bench by jersey number for clean presentation
        bench.sort(key=lambda x: x.jersey_number)

        return {
            'gk': starting_gk,
            'def': starting_def,
            'mid': starting_mid,
            'att': starting_att,
            'bench': bench,
            'all': players,
        }

    home_lineup = build_lineup_and_bench(home_players)
    away_lineup = build_lineup_and_bench(away_players)

    # Head-to-head calculations
    h2h = (
        Fixture.objects.filter(
            Q(home_team=fixture.home_team, away_team=fixture.away_team) |
            Q(home_team=fixture.away_team, away_team=fixture.home_team)
        )
        .exclude(pk=fixture.pk)
        .filter(result__isnull=False)
        .select_related('home_team', 'away_team', 'result')
        .order_by('-kickoff_at')[:5]
    )

    context = {
        'fixture': fixture,
        'result': getattr(fixture, 'result', None),
        'goals': list(fixture.goals.all()),
        'cards': list(fixture.cards.all()),
        'formation_label': '4-3-3',  # Pass a visible label to your template tag

        'home_lineup': home_lineup,
        'away_lineup': away_lineup,

        'h2h': h2h,
    }
    return render(request, 'fixture_detail.html', context)


def team_detail_api(request, team_id):
    team = get_object_or_404(Team.objects.select_related('ward'), id=team_id)
    phase_id = request.GET.get('phase')
    selected_phase = get_object_or_404(Phase, id=phase_id) if phase_id else None

    # All fixtures this team has played within the selected phase
    fixtures_qs = Fixture.objects.filter(
        Q(home_team=team) | Q(away_team=team)
    ).select_related('home_team', 'away_team', 'result', 'phase_sport', 'group')

    if selected_phase:
        fixtures_qs = fixtures_qs.filter(phase_sport__phase=selected_phase)

    fixtures_qs = fixtures_qs.order_by('round_number', 'kickoff_at')
    fixtures = list(fixtures_qs)

    # --- Overview stats (played/won/drawn/lost/gf/ga/gd/win_rate) ---
    played = won = drawn = lost = gf = ga = 0
    recent_form = []  # list of 'W' / 'D' / 'L', most recent last

    for f in fixtures:
        result = getattr(f, 'result', None)
        if not result:
            continue
        is_home = f.home_team_id == team.id
        team_score = result.home_score if is_home else result.away_score
        opp_score = result.away_score if is_home else result.home_score

        played += 1
        gf += team_score
        ga += opp_score

        if team_score > opp_score:
            won += 1
            recent_form.append('W')
        elif team_score < opp_score:
            lost += 1
            recent_form.append('L')
        else:
            drawn += 1
            recent_form.append('D')

    win_rate = round((won / played) * 100) if played else 0
    goal_difference = gf - ga
    recent_form = recent_form[-5:]  # last 5 results only

    # --- Table position / knockout status ---
    table_info = {'type': None}
    phase_sport = fixtures[0].phase_sport if fixtures else None

    if phase_sport:
        if phase_sport.fixture_format in ('league', 'uefa_league_phase'):
            all_fixtures = list(
                Fixture.objects.filter(phase_sport=phase_sport)
                .select_related('home_team', 'away_team', 'result')
            )
            rows = _compute_team_stats(all_fixtures)
            for idx, row in enumerate(rows, start=1):
                if row['team'].id == team.id:
                    table_info = {'type': 'table', 'position': idx, 'total_teams': len(rows), 'points': row['points']}
                    break

        elif phase_sport.fixture_format == 'group_knockout':
            team_group_fixture = next((f for f in fixtures if f.group_id), None)
            if team_group_fixture:
                group_fixtures = list(
                    Fixture.objects.filter(group=team_group_fixture.group)
                    .select_related('home_team', 'away_team', 'result')
                )
                rows = _compute_team_stats(group_fixtures)
                for idx, row in enumerate(rows, start=1):
                    if row['team'].id == team.id:
                        table_info = {
                            'type': 'group_table', 'group_name': team_group_fixture.group.name,
                            'position': idx, 'total_teams': len(rows), 'points': row['points']
                        }
                        break
            knockout_fixtures = [f for f in fixtures if not f.group_id]
            if knockout_fixtures:
                last_ko = knockout_fixtures[-1]
                won_last = (
                        getattr(last_ko, 'result', None) and (
                        (last_ko.home_team_id == team.id and last_ko.result.home_score > last_ko.result.away_score) or
                        (last_ko.away_team_id == team.id and last_ko.result.away_score > last_ko.result.home_score)
                )
                )
                table_info['knockout_round'] = last_ko.round_number
                table_info['knockout_status'] = 'won' if won_last else (
                    'eliminated' if getattr(last_ko, 'result', None) else 'pending')

        elif phase_sport.fixture_format == 'knockout':
            if fixtures:
                last_ko = fixtures[-1]
                won_last = (
                        getattr(last_ko, 'result', None) and (
                        (last_ko.home_team_id == team.id and last_ko.result.home_score > last_ko.result.away_score) or
                        (last_ko.away_team_id == team.id and last_ko.result.away_score > last_ko.result.home_score)
                )
                )
                table_info = {
                    'type': 'knockout',
                    'round': last_ko.round_number,
                    'status': 'won' if won_last else ('eliminated' if getattr(last_ko, 'result', None) else 'pending'),
                }

    # --- Fixtures list for display (recent + upcoming) ---
    fixtures_data = []
    for f in fixtures:
        result = getattr(f, 'result', None)
        is_home = f.home_team_id == team.id
        opponent = f.away_team if is_home else f.home_team
        fixtures_data.append({
            'opponent': opponent.name,
            'is_home': is_home,
            'kickoff_at': f.kickoff_at.strftime('%d %b, %H:%M') if f.kickoff_at else 'TBD',
            'status': f.status,
            'score': f'{result.home_score}-{result.away_score}' if result else None,
        })

    # --- Players ---
    players_qs = team.players.order_by('-goals', 'jersey_number')
    if selected_phase:
        players_qs = players_qs.filter(tournament=selected_phase.tournament)

    players_data = [
        {
            'name': p.name,
            'jersey_number': p.jersey_number,
            'position': p.get_position_display() or 'Unassigned',
            'goals': p.goals,
            'assists': p.assists,
            'yellow_card': p.yellow_card,
            'red_card': p.red_card,
            'is_captain': p.is_captain,
        }
        for p in players_qs
    ]

    return JsonResponse({
        'team': {'id': team.id, 'name': team.name, 'ward': team.ward.name if team.ward else '','home_ground':team.home_ground,'coach_name':team.coach_name,'coach_phone':team.coach_phone},
        'overview': {
            'played': played, 'won': won, 'drawn': drawn, 'lost': lost,
            'goals_for': gf, 'goals_against': ga, 'goal_difference': goal_difference,
            'win_rate': win_rate,
        },
        'recent_form': recent_form,
        'table_info': table_info,
        'fixtures': fixtures_data,
        'players': players_data,
    })


def service_worker(request):
    sw_path = finders.find('sw.js')
    if not sw_path:
        raise Http404("service worker file not found")
    with open(sw_path, 'r') as f:
        content = f.read()
    return HttpResponse(content, content_type='application/javascript')


def offline_view(request):
    return render(request, 'offline.html')
