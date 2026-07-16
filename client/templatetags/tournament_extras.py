# tournaments/templatetags/tournament_extras.py
from django import template
from itertools import groupby

register = template.Library()

@register.filter
def knockout_only(fixtures):
    """Fixtures with no group ? these are the bracket rounds (post-group or pure knockout)."""
    return [f for f in fixtures if f.group_id is None]

@register.filter
def group_by_round(fixtures):
    """[(round_number, [fixtures]), ...] preserving order. Assumes fixtures pre-sorted by round."""
    return [(rn, list(items)) for rn, items in groupby(fixtures, key=lambda f: f.round_number)]