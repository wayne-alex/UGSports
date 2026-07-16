from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver


# ============================================================
# GEOGRAPHY
# ============================================================

class SubCounty(models.Model):
    name = models.CharField(max_length=100, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Sub-Counties"
        ordering = ['name']

    def __str__(self):
        return self.name


class Ward(models.Model):
    sub_county = models.ForeignKey(SubCounty, on_delete=models.CASCADE, related_name='wards')
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Wards"
        unique_together = ('sub_county', 'name')
        ordering = ['sub_county__name', 'name']

    def __str__(self):
        return f"{self.name} ({self.sub_county.name})"


class Sport(models.Model):
    name = models.CharField(max_length=60, unique=True)
    rules_summary = models.CharField(max_length=255, help_text="e.g. '11 vs 11, 2x45min halves'")
    players_per_side = models.PositiveSmallIntegerField(default=11)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


# ============================================================
# USERS
# ============================================================

class User(AbstractUser):
    class Role(models.TextChoices):
        SYSTEM_ADMIN = 'system_admin', 'System Admin'
        COUNTY_ICT_OFFICER = 'county_ict', 'County ICT Officer'
        SUB_COUNTY_ADMIN = 'sub_county_admin', 'Sub-County Admin'
        WARD_ADMIN = 'ward_admin', 'Ward Admin'

    role = models.CharField(max_length=20, choices=Role.choices)

    sub_county = models.ForeignKey(
        SubCounty, on_delete=models.PROTECT, null=True, blank=True, related_name='admins'
    )
    ward = models.ForeignKey(
        Ward, on_delete=models.PROTECT, null=True, blank=True, related_name='admins'
    )
    phone_number = models.CharField(max_length=20, blank=True)

    def clean(self):
        super().clean()
        if self.role == self.Role.WARD_ADMIN:
            if not self.ward:
                raise ValidationError("Ward Admins must be assigned a ward.")
            self.sub_county = self.ward.sub_county
        elif self.role == self.Role.SUB_COUNTY_ADMIN:
            if not self.sub_county:
                raise ValidationError("Sub-County Admins must be assigned a sub-county.")
            self.ward = None
        else:
            self.ward = None
            self.sub_county = None

    @property
    def scope_label(self):
        if self.role == self.Role.WARD_ADMIN:
            return f"{self.ward} Ward"
        if self.role == self.Role.SUB_COUNTY_ADMIN:
            return f"{self.sub_county} Sub-County"
        return "County-wide"

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"


# ============================================================
# TOURNAMENT + PHASE
# ============================================================

class Tournament(models.Model):
    """The overall competition, e.g. 'Governor's Cup 2026'. No self-referencing tree —
    all stage/scope logic lives on Phase."""

    class Status(models.TextChoices):
        UPCOMING = 'upcoming', 'Upcoming'
        ONGOING = 'ongoing', 'Ongoing'
        COMPLETED = 'completed', 'Completed'

    name = models.CharField(max_length=150)
    season = models.CharField(max_length=20, help_text="e.g. '2026'")
    sports = models.ManyToManyField(Sport, related_name='tournaments')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPCOMING)
    ward = models.ForeignKey(
        Ward, on_delete=models.PROTECT, null=True, blank=True, related_name='Tournament'
    )
    sub_county = models.ForeignKey(
        SubCounty, on_delete=models.PROTECT, null=True, blank=True, related_name='Tournament'
    )

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='tournaments_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-season', 'name']
        unique_together = ('name', 'season')

    def __str__(self):
        return f"{self.name} ({self.season})"


class Phase(models.Model):
    """A stage of a Tournament. Ward-stage tournaments produce one Phase row PER WARD;
    sub-county produces one PER SUB-COUNTY; county/final produce a single Phase row."""

    class Stage(models.TextChoices):
        WARD = 'ward', 'Ward Phase'
        SUB_COUNTY = 'sub_county', 'Sub-County Phase'
        COUNTY = 'county', 'County Phase'
        FINAL = 'final', 'Final Phase'

    class Status(models.TextChoices):
        UPCOMING = 'upcoming', 'Upcoming'
        ONGOING = 'ongoing', 'Ongoing'
        COMPLETED = 'completed', 'Completed'

    STAGE_ORDER = {
        Stage.WARD: 1,
        Stage.SUB_COUNTY: 2,
        Stage.COUNTY: 3,
        Stage.FINAL: 4,
    }

    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name='phases')
    stage = models.CharField(max_length=20, choices=Stage.choices)
    order = models.PositiveSmallIntegerField(editable=False, default=1)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPCOMING)

    # Scope — exactly one populated for ward/sub_county stages, both null for county/final.
    ward = models.ForeignKey(
        Ward, on_delete=models.PROTECT, null=True, blank=True, related_name='phases'
    )
    sub_county = models.ForeignKey(
        SubCounty, on_delete=models.PROTECT, null=True, blank=True, related_name='phases'
    )

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='phases_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('tournament', 'stage', 'ward', 'sub_county')
        ordering = ['tournament', 'order', 'ward__name', 'sub_county__name']

    def clean(self):
        super().clean()
        if self.stage == self.Stage.WARD:
            if not self.ward:
                raise ValidationError("A Ward Phase must have a ward set.")
            self.sub_county = None
        elif self.stage == self.Stage.SUB_COUNTY:
            if not self.sub_county:
                raise ValidationError("A Sub-County Phase must have a sub-county set.")
            self.ward = None
        else:  # COUNTY, FINAL
            self.ward = None
            self.sub_county = None

    def save(self, *args, **kwargs):
        self.order = self.STAGE_ORDER[self.stage]
        super().save(*args, **kwargs)

    @property
    def next_phase_lookup(self):
        """Kwargs to find the phase a team promotes INTO from here, or None at the top."""
        if self.stage == self.Stage.WARD:
            return {'stage': self.Stage.SUB_COUNTY, 'sub_county': self.ward.sub_county}
        if self.stage == self.Stage.SUB_COUNTY:
            return {'stage': self.Stage.COUNTY}
        if self.stage == self.Stage.COUNTY:
            return {'stage': self.Stage.FINAL}
        return None

    @property
    def feeder_phases(self):
        """Phases whose teams are eligible to be promoted INTO this one."""
        if self.stage == self.Stage.SUB_COUNTY:
            return Phase.objects.filter(
                tournament=self.tournament, stage=self.Stage.WARD, ward__sub_county=self.sub_county
            )
        if self.stage == self.Stage.COUNTY:
            return Phase.objects.filter(tournament=self.tournament, stage=self.Stage.SUB_COUNTY)
        if self.stage == self.Stage.FINAL:
            return Phase.objects.filter(tournament=self.tournament, stage=self.Stage.COUNTY)
        return Phase.objects.none()

    @property
    def scope_label(self):
        if self.ward:
            return f"{self.ward.name} Ward"
        if self.sub_county:
            return f"{self.sub_county.name} Sub-County"
        if self.stage == self.Stage.FINAL:
            return "Final"
        return "County"

    def __str__(self):
        return f"{self.get_stage_display()} — {self.scope_label} ({self.tournament.name})"


class PhaseSport(models.Model):
    """Which sports run in a phase, and how fixtures are built for each.
    Format can differ per phase — round-robin at ward level, knockout at final."""

    class Format(models.TextChoices):
        LEAGUE = 'league', 'League'
        KNOCKOUT = 'knockout', 'Knockout'
        GROUP_KNOCKOUT = 'group_knockout', 'Group Stage + Knockout'
        UEFA_LEAGUE_PHASE = 'uefa_league_phase', 'UEFA-Style League Phase'

    phase = models.ForeignKey(Phase, on_delete=models.CASCADE, related_name='phase_sports')
    sport = models.ForeignKey(Sport, on_delete=models.PROTECT, related_name='phase_entries')
    fixture_format = models.CharField(max_length=20, choices=Format.choices, default=Format.LEAGUE)
    legs = models.PositiveSmallIntegerField(default=1, help_text="1 = single round-robin, 2 = home & away")
    fixtures_generated = models.BooleanField(default=False)

    class Meta:
        unique_together = ('phase', 'sport')
        verbose_name = "Phase Sport"
        verbose_name_plural = "Phase Sports"

    def __str__(self):
        return f"{self.phase} — {self.sport.name}"


class Group(models.Model):
    """Optional group-stage subdivision, e.g. 'Group A'."""
    phase_sport = models.ForeignKey(PhaseSport, on_delete=models.CASCADE, related_name='groups',null=True)
    name = models.CharField(max_length=50)

    class Meta:
        unique_together = ('phase_sport', 'name')

    def __str__(self):
        return f"{self.name} — {self.phase_sport}"


# ============================================================
# TEAMS, ENTRIES, PLAYERS
# ============================================================

class Team(models.Model):
    """A team's permanent identity, scoped only to its home ward. Never tied to a
    single tournament or phase directly — see PhaseEntry for participation."""

    name = models.CharField(max_length=120)
    sport = models.ForeignKey(Sport, on_delete=models.PROTECT, related_name='teams')
    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name='teams')
    home_ground = models.CharField(max_length=150, blank=True)

    coach_name = models.CharField(max_length=120, blank=True)
    coach_phone = models.CharField(max_length=20, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='teams_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('name', 'sport', 'ward')
        ordering = ['name']

    @property
    def sub_county(self):
        return self.ward.sub_county

    def __str__(self):
        return f"{self.name} ({self.ward.name})"


class PhaseEntry(models.Model):
    """A team's registration/participation in one specific Phase. This is what lets
    the same Team appear in Ward -> Sub-County -> County -> Final without duplication."""

    phase = models.ForeignKey(Phase, on_delete=models.CASCADE, related_name='entries')
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='phase_entries')

    # If this entry exists because the team qualified from a lower phase, point back at it.
    # Null = directly registered at this phase (e.g. teams entered straight into County).
    promoted_from = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='promotions'
    )

    registered_at = models.DateTimeField(auto_now_add=True)
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='entries_registered'
    )

    class Meta:
        unique_together = ('phase', 'team')
        verbose_name_plural = "Phase Entries"

    def clean(self):
        super().clean()
        if self.team.sport_id and self.phase.phase_sports.exclude(sport_id=self.team.sport_id).exists() \
                and not self.phase.phase_sports.filter(sport_id=self.team.sport_id).exists():
            raise ValidationError("This team's sport is not part of this phase.")

    def __str__(self):
        return f"{self.team.name} @ {self.phase}"

class Player(models.Model):
    class Position(models.TextChoices):
        GOALKEEPER = 'GK', 'Goalkeeper'
        DEFENDER = 'DEF', 'Defender'
        MIDFIELDER = 'MID', 'Midfielder'
        ATTACKER = 'ATT', 'Attacker'

    name = models.CharField(max_length=120)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='players')
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE, related_name='registered_players')

    position = models.CharField(max_length=3, choices=Position.choices, blank=True)
    jersey_number = models.PositiveSmallIntegerField()
    national_id = models.CharField(max_length=20, blank=True)
    goals = models.PositiveIntegerField(default=0)
    assists = models.PositiveIntegerField(default=0)

    yellow_card = models.IntegerField(default=0)
    red_card = models.IntegerField(default=0)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='players_registered'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('team', 'tournament', 'jersey_number')
        ordering = ['team', 'jersey_number']

    def __str__(self):
        return f"{self.name} — #{self.jersey_number} ({self.team.name}) · {self.goals} goals"


# ============================================================
# FIXTURES, RESULTS, STANDINGS
# ============================================================

class Fixture(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = 'scheduled', 'Scheduled'
        LIVE = 'live', 'Live'
        COMPLETED = 'completed', 'Completed'
        POSTPONED = 'postponed', 'Postponed'
        CANCELLED = 'cancelled', 'Cancelled'

    phase_sport = models.ForeignKey(PhaseSport, on_delete=models.CASCADE, related_name='fixtures',null=True)
    group = models.ForeignKey(Group, on_delete=models.SET_NULL, null=True, blank=True, related_name='fixtures')

    home_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='home_fixtures')
    away_team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='away_fixtures')

    round_number = models.PositiveSmallIntegerField(help_text="Matchday / round index")
    leg = models.PositiveSmallIntegerField(default=1)
    venue = models.CharField(max_length=150, blank=True)
    kickoff_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)

    class Meta:
        ordering = ['round_number', 'kickoff_at']

    def clean(self):
        if self.home_team_id == self.away_team_id:
            raise ValidationError("A team cannot play itself.")

    def __str__(self):
        return f"{self.home_team} vs {self.away_team} (R{self.round_number})"


class Result(models.Model):
    fixture = models.OneToOneField(Fixture, on_delete=models.CASCADE, related_name='result')
    home_score = models.PositiveSmallIntegerField()
    away_score = models.PositiveSmallIntegerField()

    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='results_entered'
    )
    entered_at = models.DateTimeField(auto_now_add=True)

    verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='results_verified'
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.fixture}: {self.home_score}-{self.away_score}"


class Standing(models.Model):
    """Auto-computed league table row, scoped to a PhaseSport (so ward-level and
    county-level tables for the same sport never mix)."""

    group = models.ForeignKey(Group, on_delete=models.CASCADE, null=True, blank=True, related_name='standings')
    phase_sport = models.ForeignKey(PhaseSport, on_delete=models.CASCADE, related_name='standings',null=True)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name='standings')

    played = models.PositiveSmallIntegerField(default=0)
    won = models.PositiveSmallIntegerField(default=0)
    drawn = models.PositiveSmallIntegerField(default=0)
    lost = models.PositiveSmallIntegerField(default=0)
    goals_for = models.PositiveSmallIntegerField(default=0)
    goals_against = models.PositiveSmallIntegerField(default=0)
    points = models.SmallIntegerField(default=0)

    class Meta:
        unique_together = ('phase_sport', 'team')
        ordering = ['-points', '-goals_for']

    @property
    def goal_difference(self):
        return self.goals_for - self.goals_against

    def __str__(self):
        return f"{self.team.name} — {self.points} pts"


# ============================================================
# AUDIT LOG
# ============================================================

class AuditLog(models.Model):
    class Action(models.TextChoices):
        CREATE = 'create', 'Create'
        UPDATE = 'update', 'Update'
        DELETE = 'delete', 'Delete'
        VERIFY = 'verify', 'Verify'
        GENERATE_FIXTURES = 'generate_fixtures', 'Generate Fixtures'
        PROMOTE = 'promote', 'Promote Team'
        LOGIN = 'login', 'Login'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='audit_entries'
    )
    action = models.CharField(max_length=30, choices=Action.choices)

    content_type = models.ForeignKey(ContentType, on_delete=models.SET_NULL, null=True, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey('content_type', 'object_id')
    object_repr = models.CharField(max_length=255, blank=True, help_text="Snapshot of str(obj) at time of action")

    changes = models.JSONField(null=True, blank=True, help_text="e.g. {'field': ['old', 'new']}")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.user} {self.get_action_display()} {self.object_repr} @ {self.timestamp:%Y-%m-%d %H:%M}"


# ============================================================
# NEWS
# ============================================================

class NewsPost(models.Model):
    TAG_CHOICES = [
        ('announcement', 'Official Announcement'),
        ('match_report', 'Match Report'),
        ('fixture_update', 'Fixture Update'),
        ('general', 'General'),
    ]

    title = models.CharField(max_length=255, help_text="Headline of the post")
    tag = models.CharField(max_length=30, choices=TAG_CHOICES, default='general')

    sub_county = models.ForeignKey(
        SubCounty, on_delete=models.CASCADE, null=True, blank=True,
        related_name='news_posts', help_text="Leave blank for County-Wide scope"
    )
    phase = models.ForeignKey(
        Phase, on_delete=models.CASCADE, null=True, blank=True,
        related_name='news_posts', help_text="Link this news to a specific phase"
    )

    body = models.TextField(help_text="The full story content")

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='authored_news'
    )

    published_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    like_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-published_at']

    def __str__(self):
        return self.title


class NewsComment(models.Model):
    post = models.ForeignKey(NewsPost, on_delete=models.CASCADE, related_name='comments')

    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    guest_name = models.CharField(max_length=100, default="Anonymous Viewer", blank=True)

    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def display_author_name(self):
        if self.author:
            return self.author.get_full_name() or self.author.username
        return self.guest_name

class Goal(models.Model):
    fixture = models.ForeignKey(Fixture, on_delete=models.CASCADE, related_name='goals')
    scorer = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='goals_scored')
    assisted_by = models.ForeignKey(
        Player, on_delete=models.SET_NULL, null=True, blank=True, related_name='assists_made'
    )
    minute = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['minute', 'created_at']

    def clean(self):
        if self.assisted_by_id and self.assisted_by_id == self.scorer_id:
            raise ValidationError("A player can't assist their own goal.")

    def __str__(self):
        minute = f"{self.minute}'" if self.minute else ""
        return f"{self.scorer.name} {minute} — {self.fixture}"

class Card(models.Model):
    class CardType(models.TextChoices):
        YELLOW = 'yellow', 'Yellow Card'
        RED = 'red', 'Red Card'

    fixture = models.ForeignKey(Fixture, on_delete=models.CASCADE, related_name='cards')
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='cards_received')
    card_type = models.CharField(max_length=10, choices=CardType.choices)
    minute = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['minute', 'created_at']

    def __str__(self):
        return f"{self.get_card_type_display()} — {self.player.name} ({self.fixture})"


@receiver([post_save, post_delete], sender=Card)
def sync_player_card_tallies(sender, instance, **kwargs):
    """Same pattern as sync_player_goal_tallies — recomputed from real events,
    never incremented by hand, so it can't drift even across repeated edits."""
    instance.player.yellow_card = instance.player.cards_received.filter(card_type=Card.CardType.YELLOW).count()
    instance.player.red_card = instance.player.cards_received.filter(card_type=Card.CardType.RED).count()
    instance.player.save(update_fields=['yellow_card', 'red_card'])

@receiver([post_save, post_delete], sender=Goal)
def sync_player_goal_tallies(sender, instance, **kwargs):
    """Keep Player.goals accurate whenever a Goal is added, edited, or removed —
    recomputed from real events rather than incremented by hand, so it can never drift."""
    instance.scorer.goals = instance.scorer.goals_scored.count()
    instance.scorer.save(update_fields=['goals'])
    if instance.assisted_by_id:
        instance.assisted_by.assists = instance.assisted_by.assists_made.count()
        instance.assisted_by.save(update_fields=['assists'])