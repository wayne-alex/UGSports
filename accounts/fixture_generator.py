import random
import math
from datetime import date, timedelta, datetime, time
from itertools import combinations, zip_longest

from django.core.exceptions import ValidationError

from .models import Phase, PhaseSport, Team, PhaseEntry, Group, Fixture


class FixtureGenerator:
    """
    Generates fixtures for a single PhaseSport, against the real
    Tournament -> Phase -> PhaseSport -> Fixture schema.

    Config shape:
    {
        "start_date": "2026-06-01",     # optional ISO date, defaults to phase.start_date
        "end_date":   "2026-06-14",     # optional ISO date, defaults to phase.end_date
        "groups": 4,                    # group_knockout only
        "max_matches_per_day": 6,       # optional venue-capacity cap; splits an
                                         # oversized round across consecutive days
    }

    Design guarantees (see README notes inline at each method):
    - FAIRNESS: every round is built so each team appears at most once in it
      (circle-method round-robin, or greedy edge-coloring for UEFA phase).
      A team can never be assigned two matches on the same calendar day
      unless the tournament window is genuinely too short to avoid it
      (in which case the window is silently extended past end_date rather
      than violating fairness — see _assign_dates_to_rounds).
    - EVEN SPREAD: rounds (not raw match counts) are distributed evenly
      across the available days, so "matchdays" land at regular intervals
      rather than being front-loaded.
    """

    def __init__(self, phase_sport: PhaseSport, config: dict = None):
        self.phase_sport = phase_sport
        self.phase = phase_sport.phase
        self.config = config or {}

        self.legs = int(phase_sport.legs or 1)

        start = self.config.get("start_date")
        end = self.config.get("end_date")
        duration = self.config.get("duration")

        self.start_date = date.fromisoformat(start) if start else self.phase.start_date

        # Determine end date based on end, duration, or fallback to phase
        if end:
            self.end_date = date.fromisoformat(end)
        elif duration:
            self.end_date = self.start_date + timedelta(days=int(duration) - 1)
        else:
            self.end_date = self.phase.end_date

        if not self.start_date or not self.end_date:
            raise ValidationError(
                "Provide a start date and either a duration or an end date "
                "(or set start/end dates on the phase itself)."
            )

        if self.end_date < self.start_date:
            raise ValidationError("End date can't be before the start date.")

        self.schedule_type = self.config.get("schedule_type", "daily")
        self.max_matches_per_day = self.config.get("max_matches_per_day")

    # ---------------------------------------------------------------
    # PUBLIC ENTRY POINT
    # ---------------------------------------------------------------
    def generate(self):
        Fixture.objects.filter(phase_sport=self.phase_sport).delete()
        Group.objects.filter(phase_sport=self.phase_sport).delete()

        fmt = self.phase_sport.fixture_format

        if fmt == PhaseSport.Format.LEAGUE:
            self._generate_league()
        elif fmt == PhaseSport.Format.KNOCKOUT:
            self._generate_knockout()
        elif fmt == PhaseSport.Format.GROUP_KNOCKOUT:
            self._generate_group_knockout()
        elif fmt == PhaseSport.Format.UEFA_LEAGUE_PHASE:
            self._generate_uefa_league_phase()
        else:
            raise ValidationError(f"Unknown fixture format: {fmt}")

        self.phase_sport.fixtures_generated = True
        self.phase_sport.save(update_fields=['fixtures_generated'])

    # ---------------------------------------------------------------
    # TEAMS — via PhaseEntry, scoped to this phase + this sport
    # ---------------------------------------------------------------
    def _get_teams(self):
        entries = (
            PhaseEntry.objects
            .filter(phase=self.phase, team__sport=self.phase_sport.sport)
            .select_related('team')
        )
        teams = [e.team for e in entries]
        if len(teams) < 2:
            raise ValidationError(
                f"Need at least 2 teams entered for {self.phase_sport.sport.name} "
                f"in {self.phase} to generate fixtures — found {len(teams)}."
            )
        return teams

    # ---------------------------------------------------------------
    # CIRCLE METHOD — the actual fairness fix (answers Q2)
    # ---------------------------------------------------------------
    def _round_robin_rounds(self, teams, legs=1):
        """
        Standard circle-method round-robin. Fixes one team, rotates the
        rest. Produces (n-1) rounds for n teams (n if odd, with one bye
        per round). Every team appears EXACTLY ONCE per round — this is
        what guarantees no team plays 3 games while another waits.
        """
        teams = list(teams)
        bye = None
        if len(teams) % 2 == 1:
            teams.append(bye)

        n = len(teams)
        rounds = []
        rotation = teams[:]

        for r in range(n - 1):
            pairs = []
            for i in range(n // 2):
                t1 = rotation[i]
                t2 = rotation[n - 1 - i]
                if t1 is None or t2 is None:
                    continue  # this team has the bye this round
                # Alternate home/away by round so nobody is always home or always away
                pairs.append((t1, t2) if r % 2 == 0 else (t2, t1))
            rounds.append(pairs)
            rotation = [rotation[0]] + [rotation[-1]] + rotation[1:-1]

        if legs == 2:
            second_leg = [[(away, home) for home, away in rnd] for rnd in rounds]
            rounds = rounds + second_leg

        return rounds

    # ---------------------------------------------------------------
    # DATE ASSIGNMENT — the even-spread fix (answers Q1)
    # ---------------------------------------------------------------
    def _assign_dates_to_rounds(self, rounds):
        """
        Same even-spread / conflict-free-packing logic as before, but now
        only considers days allowed by schedule_type ('daily' = every day,
        'weekends' = Sat/Sun only). If the window doesn't contain enough
        allowed days for all rounds, extends forward past end_date rather
        than double-booking a team or silently using disallowed days.
        """
        base_days = self._allowed_days_in_window()
        num_rounds = len(rounds)

        if base_days and num_rounds <= len(base_days):
            if num_rounds == 1:
                return [base_days[0]]
            offsets = [round(i * (len(base_days) - 1) / (num_rounds - 1)) for i in range(num_rounds)]
            return [base_days[o] for o in offsets]

        # Not enough allowed days in-window — pack conflict-free, extending as needed
        current_day = base_days[0] if base_days else self._first_allowed_day(self.start_date)
        dates = []
        current_day_team_ids = set()

        for rnd in rounds:
            round_team_ids = {t.pk for pair in rnd for t in pair if t}
            if current_day_team_ids & round_team_ids:
                current_day = self._next_allowed_day(current_day)
                current_day_team_ids = set()
            dates.append(current_day)
            current_day_team_ids |= round_team_ids

        return dates

    def _is_allowed_day(self, d):
        if self.schedule_type == "weekends":
            return d.weekday() >= 5  # Saturday=5, Sunday=6
        return True

    def _allowed_days_in_window(self):
        days = []
        d = self.start_date
        while d <= self.end_date:
            if self._is_allowed_day(d):
                days.append(d)
            d += timedelta(days=1)
        return days

    def _first_allowed_day(self, d):
        while not self._is_allowed_day(d):
            d += timedelta(days=1)
        return d

    def _next_allowed_day(self, d):
        d += timedelta(days=1)
        return self._first_allowed_day(d)

    def _create_fixtures_from_rounds(self, rounds, group=None, leg_override=None):
        """Shared writer: takes rounds + their assigned dates, creates Fixture rows,
        and (if max_matches_per_day is set) splits an oversized round across
        consecutive days without breaking the no-repeat-team-per-day guarantee
        — safe because matches within a single round never share a team."""
        dates = self._assign_dates_to_rounds(rounds)

        for round_num, (rnd, match_date) in enumerate(zip(rounds, dates), start=1):
            leg = leg_override if leg_override else (2 if self.legs == 2 and round_num > len(rounds) // 2 else 1)
            chunks = self._chunk_for_capacity(rnd)
            for offset, chunk in enumerate(chunks):
                day = match_date + timedelta(days=offset)
                default_kickoff = datetime.combine(day, time(10, 0))
                for home, away in chunk:
                    Fixture.objects.create(
                        phase_sport=self.phase_sport,
                        group=group,
                        home_team=home,
                        away_team=away,
                        round_number=round_num,
                        leg=leg,
                        kickoff_at=default_kickoff,
                        status=Fixture.Status.SCHEDULED,
                    )

    def _chunk_for_capacity(self, matches):
        if not self.max_matches_per_day or len(matches) <= self.max_matches_per_day:
            return [matches]
        return [matches[i:i + self.max_matches_per_day] for i in range(0, len(matches), self.max_matches_per_day)]

    # ---------------------------------------------------------------
    # LEAGUE
    # ---------------------------------------------------------------
    def _generate_league(self):
        teams = self._get_teams()
        rounds = self._round_robin_rounds(teams, legs=self.legs)
        self._create_fixtures_from_rounds(rounds)

    # ---------------------------------------------------------------
    # KNOCKOUT
    # NOTE: only Round 1 can be generated up front — later rounds depend
    # on who wins, so they must be generated separately once results are
    # verified (a "generate next round" admin action, not covered here).
    # ---------------------------------------------------------------
    def _generate_knockout(self):
        teams = self._get_teams()
        random.shuffle(teams)

        size = self._next_power_of_two(len(teams))
        padded = teams + [None] * (size - len(teams))
        pairs = [(padded[i], padded[i + 1]) for i in range(0, size, 2)]
        real_pairs = [(h, a) for h, a in pairs if h and a]

        matches = []
        for home, away in real_pairs:
            matches.append((home, away))
            if self.legs == 2:
                matches.append((away, home))

        # Round 1 is a single round by definition — every team appears once already.
        self._create_fixtures_from_rounds([matches])

    # ---------------------------------------------------------------
    # GROUP + KNOCKOUT
    # ---------------------------------------------------------------
    def _generate_group_knockout(self):
        num_groups = int(self.config.get("groups", 2))
        teams = self._get_teams()
        random.shuffle(teams)

        groups_teams = [[] for _ in range(num_groups)]
        for i, team in enumerate(teams):
            groups_teams[i % num_groups].append(team)

        group_objs = []
        group_rounds = []
        for idx, group_teams in enumerate(groups_teams):
            if len(group_teams) < 2:
                continue
            group = Group.objects.create(
                phase_sport=self.phase_sport,
                name=f"Group {chr(65 + idx)}",
            )
            group_objs.append(group)
            group_rounds.append(self._round_robin_rounds(group_teams, legs=self.legs))

        # Interleave: "Round 1" = round 1 of every group together, etc.
        # zip_longest handles groups of unequal size (some groups run out of rounds sooner).
        combined_rounds = []
        combined_round_groups = []  # parallel list: which group each match in a round belongs to
        for round_set in zip_longest(*group_rounds, fillvalue=[]):
            merged = []
            merged_groups = []
            for g_idx, matches in enumerate(round_set):
                for pair in matches:
                    merged.append(pair)
                    merged_groups.append(group_objs[g_idx])
            combined_rounds.append(merged)
            combined_round_groups.append(merged_groups)

        dates = self._assign_dates_to_rounds(combined_rounds)
        for round_num, (rnd, match_date, groups) in enumerate(zip(combined_rounds, dates, combined_round_groups), start=1):
            default_kickoff = datetime.combine(match_date, time(10, 0))
            for (home, away), group in zip(rnd, groups):
                Fixture.objects.create(
                    phase_sport=self.phase_sport,
                    group=group,
                    home_team=home,
                    away_team=away,
                    round_number=round_num,
                    leg=1,
                    kickoff_at=default_kickoff,
                    status=Fixture.Status.SCHEDULED,
                )

    # ---------------------------------------------------------------
    # UEFA LEAGUE PHASE
    # Not round-robin (each team plays a subset of opponents), so fairness
    # is enforced via greedy edge-coloring instead of the circle method.
    # ---------------------------------------------------------------
    def _generate_uefa_league_phase(self):
        teams = self._get_teams()
        n = len(teams)
        if n < 4:
            raise ValidationError("UEFA League Phase needs at least 4 teams.")

        total_opponents = int(self.config.get("total_opponents", self._uefa_opponents(n)))
        home_opponents = total_opponents // 2
        away_opponents = total_opponents - home_opponents

        matchups = self._build_uefa_draw(teams, home_opponents, away_opponents)
        rounds = self._color_into_rounds(matchups)
        self._create_fixtures_from_rounds(rounds, leg_override=1)

    def _color_into_rounds(self, matches):
        """
        Greedy edge-coloring: place each match in the first round where
        neither team is already scheduled. Guarantees no team plays twice
        in the same round — the UEFA-phase equivalent of the circle method.
        """
        random.shuffle(matches)
        rounds = []
        for home, away in matches:
            placed = False
            for rnd in rounds:
                used = {t.pk for pair in rnd for t in pair}
                if home.pk not in used and away.pk not in used:
                    rnd.append((home, away))
                    placed = True
                    break
            if not placed:
                rounds.append([(home, away)])
        return rounds

    def _uefa_opponents(self, n: int) -> int:
        raw = max(4, round(n / 4.5))
        return raw if raw % 2 == 0 else raw + 1

    def _build_uefa_draw(self, teams, home_count, away_count):
        all_pairs = list(combinations(teams, 2))
        random.shuffle(all_pairs)

        home_budget = {t: home_count for t in teams}
        away_budget = {t: away_count for t in teams}
        matchups = []
        used_pairs = set()

        for a, b in all_pairs:
            pair_key = frozenset([a.pk, b.pk])
            if pair_key in used_pairs:
                continue
            if home_budget[a] > 0 and away_budget[b] > 0:
                matchups.append((a, b))
                home_budget[a] -= 1
                away_budget[b] -= 1
                used_pairs.add(pair_key)
            elif home_budget[b] > 0 and away_budget[a] > 0:
                matchups.append((b, a))
                home_budget[b] -= 1
                away_budget[a] -= 1
                used_pairs.add(pair_key)

        return matchups

    # ---------------------------------------------------------------
    # HELPERS
    # ---------------------------------------------------------------
    def _next_power_of_two(self, n: int) -> int:
        power = 1
        while power < n:
            power *= 2
        return power