from django.db.models import Max

from accounts.models import PhaseSport, Fixture, Group


def _compute_group_standings(fixtures):
    """On-the-fly P/W/D/L/GF/GA/Pts table from completed fixtures only —
    Group has no stored teams M2M, so this derives membership from the
    fixtures themselves rather than a lookup table."""
    table = {}
    teams_in_group = set()
    for f in fixtures:
        teams_in_group.add(f.home_team)
        teams_in_group.add(f.away_team)

    for team in teams_in_group:
        table[team.pk] = {'team': team, 'played': 0, 'won': 0, 'drawn': 0, 'lost': 0, 'gf': 0, 'ga': 0, 'points': 0}

    for f in fixtures:
        if not hasattr(f, 'result'):
            continue
        r = f.result
        h, a = table[f.home_team.pk], table[f.away_team.pk]
        h['played'] += 1;
        a['played'] += 1
        h['gf'] += r.home_score;
        h['ga'] += r.away_score
        a['gf'] += r.away_score;
        a['ga'] += r.home_score
        if r.home_score > r.away_score:
            h['won'] += 1;
            h['points'] += 3;
            a['lost'] += 1
        elif r.away_score > r.home_score:
            a['won'] += 1;
            a['points'] += 3;
            h['lost'] += 1
        else:
            h['drawn'] += 1;
            a['drawn'] += 1;
            h['points'] += 1;
            a['points'] += 1

    rows = list(table.values())
    rows.sort(key=lambda row: (-row['points'], -(row['gf'] - row['ga']), -row['gf']))
    return rows


def _determine_knockout_winner(fixture):
    """Winner of a completed knockout fixture, or None if it's a draw
    (which a knockout tie can't stay as — needs a manual decisive edit)."""
    if not hasattr(fixture, 'result'):
        return None
    r = fixture.result
    if r.home_score > r.away_score:
        return fixture.home_team
    if r.away_score > r.home_score:
        return fixture.away_team
    return None

def resolve_next_round(phase_sport, qualify_per_group=2):
    """
    Returns (qualified_teams, next_round_number, error_message, info_message).
    Exactly one of error_message/info_message will be set if things aren't ready
    to build a next round; both None means proceed normally.
    """
    fmt = phase_sport.fixture_format
    if fmt not in (PhaseSport.Format.KNOCKOUT, PhaseSport.Format.GROUP_KNOCKOUT):
        return [], None, "Next-round generation only applies to Knockout or Group + Knockout formats.", None

    fixtures = Fixture.objects.filter(phase_sport=phase_sport).select_related(
        'home_team', 'away_team', 'result', 'group'
    )
    qualified_teams = []
    next_round_number = None

    if fmt == PhaseSport.Format.KNOCKOUT:
        current_round = fixtures.aggregate(m=Max('round_number'))['m'] or 0
        current_fixtures = fixtures.filter(round_number=current_round)

        if current_fixtures.filter(result__isnull=True).exists():
            return [], None, f"Round {current_round} still has unplayed fixtures — enter all results first.", None

        unresolved = [f for f in current_fixtures if _determine_knockout_winner(f) is None]
        if unresolved:
            return [], None, (
                f"{len(unresolved)} fixture(s) in Round {current_round} are drawn — "
                "a knockout tie can't stay level. Edit those results to a decisive score first."
            ), None

        qualified_teams = [_determine_knockout_winner(f) for f in current_fixtures]
        next_round_number = current_round + 1

        if len(qualified_teams) == 1:
            return [], None, None, f"{qualified_teams[0].name} has already won the tournament — no further rounds needed."

    else:  # GROUP_KNOCKOUT
        group_fixtures = fixtures.filter(group__isnull=False)
        if group_fixtures.filter(result__isnull=True).exists():
            return [], None, "Group stage still has unplayed fixtures — enter all results first.", None

        for group in Group.objects.filter(phase_sport=phase_sport):
            standings = _compute_group_standings(group_fixtures.filter(group=group))
            qualified_teams.extend([row['team'] for row in standings[:qualify_per_group]])

        next_round_number = (fixtures.aggregate(m=Max('round_number'))['m'] or 0) + 1

    return qualified_teams, next_round_number, None, None


def create_next_round_fixtures(phase_sport, post_data, next_round_number):
    """
    Returns (created_count, error_message). error_message is set if pairings were invalid;
    in that case nothing is saved.
    """
    pair_count = int(post_data.get('pair_count', 0))
    used_team_ids = set()
    pairings = []

    for i in range(pair_count):
        home_id = post_data.get(f'home_{i}')
        away_id = post_data.get(f'away_{i}')
        if not home_id or not away_id:
            continue
        if home_id == away_id:
            return 0, "A team can't play itself — check your pairings."
        if home_id in used_team_ids or away_id in used_team_ids:
            return 0, "A team appears in more than one pairing — each qualified team can only play once this round."
        used_team_ids.add(home_id)
        used_team_ids.add(away_id)
        pairings.append((home_id, away_id))

    created = 0
    for home_id, away_id in pairings:
        Fixture.objects.create(
            phase_sport=phase_sport,
            home_team_id=home_id,
            away_team_id=away_id,
            round_number=next_round_number,
            status=Fixture.Status.SCHEDULED,
        )
        created += 1

    return created, None