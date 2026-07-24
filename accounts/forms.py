from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from .models import Tournament, Sport, Phase, Team, Player, PhaseEntry, Fixture, PhaseSport, Group, Ward, SubCounty

User = get_user_model()


class TournamentForm(forms.ModelForm):
    sports = forms.ModelMultipleChoiceField(
        queryset=Sport.objects.filter(is_active=True),
        widget=forms.CheckboxSelectMultiple,
        required=True,
    )

    class Meta:
        model = Tournament
        fields = ['name', 'season', 'sub_county', 'ward', 'status', 'sports', 'start_date', 'end_date']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['sub_county'].required = False
        self.fields['ward'].required = False

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get('start_date'), cleaned.get('end_date')
        if start and end and end < start:
            raise forms.ValidationError("End date can't be before the start date.")

        ward = cleaned.get('ward')
        sub_county = cleaned.get('sub_county')
        if ward and sub_county and ward.sub_county_id != sub_county.id:
            self.add_error('ward', "Selected ward doesn't belong to the selected sub-county.")
        return cleaned


class PhaseForm(forms.ModelForm):
    fixture_format = forms.ChoiceField(
        choices=PhaseSport.Format.choices,
        initial=PhaseSport.Format.LEAGUE,
        label="Fixture Format"
    )
    legs = forms.IntegerField(
        initial=1,
        min_value=1,
        label="Legs / Rounds",
        help_text="1 = Single round, 2 = Home & Away"
    )

    class Meta:
        model = Phase
        fields = ['stage', 'ward', 'sub_county', 'start_date', 'end_date', 'status']

    def __init__(self, *args, **kwargs):
        self.tournament = kwargs.pop('tournament', None)
        super().__init__(*args, **kwargs)
        self.fields['ward'].required = False
        self.fields['sub_county'].required = False

    def clean(self):
        cleaned = super().clean()
        stage = cleaned.get('stage')
        ward = cleaned.get('ward')
        sub_county = cleaned.get('sub_county')

        if stage == Phase.Stage.WARD and not ward:
            self.add_error('ward', "A Ward Phase must have a ward selected.")
        if stage == Phase.Stage.SUB_COUNTY and not sub_county:
            self.add_error('sub_county', "A Sub-County Phase must have a sub-county selected.")

        if self.tournament and stage:
            qs = Phase.objects.filter(tournament=self.tournament, stage=stage, ward=ward, sub_county=sub_county)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("A phase for this stage and scope already exists in this tournament.")
        return cleaned


class TeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ['name', 'sport', 'ward', 'home_ground', 'coach_name', 'coach_phone']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Disable the ward field so it's read-only
        self.fields['ward'].disabled = True
        # Ensure it's still required
        self.fields['ward'].required = False


class PlayerForm(forms.ModelForm):
    class Meta:
        model = Player
        fields = ['name', 'team', 'jersey_number', 'position', 'national_id']

    def __init__(self, *args, **kwargs):
        self.tournament = kwargs.pop('tournament', None)
        super().__init__(*args, **kwargs)
        if self.tournament:
            self.fields['team'].queryset = Team.objects.filter(
                phase_entries__phase__tournament=self.tournament
            ).distinct()


class FixtureGenerationForm(forms.Form):
    sport = forms.ModelChoiceField(queryset=Sport.objects.filter(is_active=True), label="Sport")
    start_date = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}))
    duration = forms.IntegerField(initial=30, label="Duration (Days)",
                                  widget=forms.NumberInput(attrs={'class': 'form-control'}))
    schedule_type = forms.ChoiceField(
        choices=[('daily', 'Daily — any day of the week'), ('weekends', 'Weekends only — Sat & Sun')],
        initial='daily',
        widget=forms.Select(attrs={'class': 'form-control'}),
    )
    format = forms.ChoiceField(choices=[
        ('league', 'League'),
        ('knockout', 'Knockout'),
        ('group_knockout', 'Group + Knockout'),  # matches PhaseSport.Format exactly
    ], widget=forms.Select(attrs={'class': 'form-control', 'id': 'formatSelect'}))
    legs = forms.ChoiceField(choices=[(1, 'Single Leg'), (2, 'Home & Away')],
                             widget=forms.Select(attrs={'class': 'form-control'}))
    groups = forms.IntegerField(initial=4, required=False, widget=forms.NumberInput(attrs={'class': 'form-control'}))
    qualify_per_group = forms.IntegerField(initial=2, required=False,
                                           widget=forms.NumberInput(attrs={'class': 'form-control'}))


class FixtureEditForm(forms.ModelForm):
    class Meta:
        model = Fixture
        fields = ['kickoff_at', 'venue', 'status']
        widgets = {
            'kickoff_at': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
            'venue': forms.TextInput(attrs={'class': 'form-control'}),
            'status': forms.Select(attrs={'class': 'form-control'}),
        }


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']


class WardAdminProfileForm(forms.ModelForm):
    first_name = forms.CharField(max_length=150, required=True)
    last_name = forms.CharField(max_length=150, required=True)
    email = forms.EmailField(required=True)

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Lock down the email field
        self.fields['email'].widget.attrs['readonly'] = 'readonly'

    def clean_email(self):
        # Security fallback: Ignore any submitted email value and keep the original
        return self.instance.email


class FixtureForm(forms.ModelForm):
    class Meta:
        model = Fixture
        fields = ['phase_sport', 'group', 'home_team', 'away_team', 'round_number', 'leg', 'venue', 'kickoff_at',
                  'status']
        widgets = {
            'kickoff_at': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-input'}),
            'venue': forms.TextInput(attrs={'placeholder': 'Enter field/stadium name', 'class': 'form-input'}),
            'round_number': forms.NumberInput(attrs={'min': '1', 'class': 'form-input'}),
            'leg': forms.NumberInput(attrs={'min': '1', 'class': 'form-input'}),
            'phase_sport': forms.Select(attrs={'class': 'form-select'}),
            'group': forms.Select(attrs={'class': 'form-select'}),
            'home_team': forms.Select(attrs={'class': 'form-select'}),
            'away_team': forms.Select(attrs={'class': 'form-select'}),
            'status': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        # We pass the phase into the form from the view for scoping
        self.phase = kwargs.pop('phase', None)
        super().__init__(*args, **kwargs)

        if self.phase:
            # Only show PhaseSports associated with this Phase
            self.fields['phase_sport'].queryset = PhaseSport.objects.filter(phase=self.phase)

            # Only show groups associated with this phase
            self.fields['group'].queryset = Group.objects.filter(phase_sport__phase=self.phase)

            # Scope teams based on Ward vs Sub-County phase stages
            if self.phase.stage == 'ward':
                teams_qs = Team.objects.filter(ward=self.phase.ward)
            else:
                teams_qs = Team.objects.filter(ward__sub_county=self.phase.sub_county)

            self.fields['home_team'].queryset = teams_qs
            self.fields['away_team'].queryset = teams_qs

    def clean(self):
        cleaned_data = super().clean()
        home_team = cleaned_data.get('home_team')
        away_team = cleaned_data.get('away_team')

        if home_team and away_team and home_team == away_team:
            raise ValidationError("A team cannot play against itself.")
        return cleaned_data


class WardTournamentForm(forms.ModelForm):
    sports = forms.ModelMultipleChoiceField(
        queryset=Sport.objects.filter(is_active=True),
        widget=forms.CheckboxSelectMultiple,
        required=True,
    )

    class Meta:
        model = Tournament
        fields = ['name', 'season', 'status', 'sports', 'start_date', 'end_date']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get('start_date'), cleaned.get('end_date')
        if start and end and end < start:
            raise forms.ValidationError("End date can't be before the start date.")
        return cleaned


class WardPhaseForm(forms.ModelForm):
    fixture_format = forms.ChoiceField(
        choices=PhaseSport.Format.choices,
        initial=PhaseSport.Format.LEAGUE,
        label="Fixture Format"
    )
    legs = forms.IntegerField(
        initial=1,
        min_value=1,
        label="Legs / Rounds",
        help_text="1 = Single round, 2 = Home & Away"
    )

    class Meta:
        model = Phase
        fields = ['stage', 'ward', 'start_date', 'end_date', 'status']

    def __init__(self, *args, **kwargs):
        self.tournament = kwargs.pop('tournament', None)
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        if self.user and getattr(self.user, 'ward', None):
            ward = self.user.ward
            # Lock ward to the admin's own ward — no picker needed.
            self.fields['ward'].queryset = Ward.objects.filter(pk=ward.pk)
            self.fields['ward'].initial = ward
            self.fields['ward'].disabled = True
            # Ward admins only ever create Ward-stage phases.
            self.fields['stage'].choices = [
                (Phase.Stage.WARD, dict(Phase.Stage.choices)[Phase.Stage.WARD])
            ]
            self.fields['stage'].initial = Phase.Stage.WARD

        # Lock stage/ward on edit too — scope shouldn't change after creation.
        if self.instance and self.instance.pk:
            self.fields['stage'].disabled = True
            self.fields['ward'].disabled = True

    def clean(self):
        cleaned = super().clean()

        # Defense in depth: force scope back to the admin's own ward
        # regardless of what was (or wasn't) submitted for disabled fields.
        if self.user and getattr(self.user, 'ward', None):
            cleaned['ward'] = self.user.ward
            cleaned['stage'] = Phase.Stage.WARD

        stage = cleaned.get('stage')
        ward = cleaned.get('ward')

        if stage == Phase.Stage.WARD and not ward:
            self.add_error('ward', "A Ward Phase must have a ward selected.")

        if self.tournament and stage:
            qs = Phase.objects.filter(tournament=self.tournament, stage=stage, ward=ward)
            if self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("A phase for this ward already exists in this tournament.")

        return cleaned


class SubcountyTeamForm(forms.ModelForm):
    class Meta:
        model = Team
        fields = ['name', 'sport', 'ward', 'home_ground', 'coach_name', 'coach_phone']

    def __init__(self, *args, sub_county=None, **kwargs):
        super().__init__(*args, **kwargs)
        if sub_county is not None:
            self.fields['ward'].queryset = Ward.objects.filter(sub_county=sub_county)
        self.fields['sport'].queryset = Sport.objects.filter(is_active=True)


class SubcountyTournamentForm(forms.ModelForm):
    sports = forms.ModelMultipleChoiceField(
        queryset=Sport.objects.filter(is_active=True),
        widget=forms.CheckboxSelectMultiple,
        required=True,
    )

    class Meta:
        model = Tournament
        fields = ['name', 'season', 'status', 'sports', 'ward', 'start_date', 'end_date']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        # 1. Pop 'user' so super().__init__ doesn't get unexpected arguments
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        if user and hasattr(user, 'sub_county') and user.sub_county:
            self.fields['ward'].queryset = Ward.objects.filter(sub_county=user.sub_county)
        else:
            self.fields['ward'].queryset = Ward.objects.none()

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get('start_date'), cleaned.get('end_date')
        if start and end and end < start:
            raise forms.ValidationError("End date can't be before the start date.")
        return cleaned


class SingleFixtureForm(forms.ModelForm):
    class Meta:
        model = Fixture
        fields = ['group', 'home_team', 'away_team', 'round_number', 'venue', 'kickoff_at', 'status']
        widgets = {'kickoff_at': forms.DateTimeInput(attrs={'type': 'datetime-local'})}

    def __init__(self, *args, phase_sport=None, **kwargs):
        super().__init__(*args, **kwargs)
        if phase_sport:
            self.fields['group'].queryset = phase_sport.groups.all()
            team_ids = PhaseEntry.objects.filter(
                phase=phase_sport.phase
            ).values_list('team_id', flat=True)
            teams = Team.objects.filter(id__in=team_ids)
            self.fields['home_team'].queryset = teams
            self.fields['away_team'].queryset = teams

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('home_team') and cleaned.get('home_team') == cleaned.get('away_team'):
            raise forms.ValidationError("A team cannot play itself.")
        return cleaned


class SubCountyPhaseForm(PhaseForm):
    """
    Used when a Sub-County Admin creates a Phase.
    Restricts sub_county to their own, and ward choices to
    wards that belong to that sub_county.
    """

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        if self.user and self.user.role == User.Role.SUB_COUNTY_ADMIN:
            sub_county = self.user.sub_county

            # Lock sub_county to the admin's own — no dropdown needed,
            # but keep it in the form so clean()/save() still work.
            self.fields['sub_county'].queryset = SubCounty.objects.filter(pk=sub_county.pk)
            self.fields['sub_county'].initial = sub_county
            self.fields['sub_county'].disabled = True

            # Only show wards within this sub-county.
            self.fields['ward'].queryset = Ward.objects.filter(sub_county=sub_county)

            # A sub-county admin only creates Sub-County or Ward phases,
            # never County/Final.
            self.fields['stage'].choices = [
                (val, label) for val, label in Phase.Stage.choices
                if val in (Phase.Stage.SUB_COUNTY, Phase.Stage.WARD)
            ]

    def clean(self):
        cleaned = super().clean()

        # Defense in depth: even if someone tampers with the payload,
        # force sub_county back to the admin's own scope.
        if self.user and self.user.role == User.Role.SUB_COUNTY_ADMIN:
            cleaned['sub_county'] = self.user.sub_county

            ward = cleaned.get('ward')
            if ward and ward.sub_county_id != self.user.sub_county_id:
                self.add_error('ward', "You can only select a ward within your own sub-county.")

        return cleaned
