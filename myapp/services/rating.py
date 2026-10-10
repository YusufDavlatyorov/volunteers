"""The public volunteer rating list — one source for every page that shows it.

The Rating page (``rating_view``) and the About page's "Our volunteers" block
(``services/landing.py``) read the same list, in the same order: every active
volunteer, by name — no places, no points (owner decision 2026-10-03: helping
is not a contest; reconfirmed 2026-10-09 and for the About page 2026-10-10).
"""

from django.core.cache import cache

from accounts.models import RATING_CACHE_KEY, Profile

RATING_TTL = 300  # seconds; Profile.add_points() / review stats clear it at once
RATING_MAX_ROWS = 2000  # response-size bound, like the map caps

# Russian + Tajik Cyrillic in alphabet order (ё after е, ғ after г, ӣ after и,
# қ after к, ӯ after у, ҳ after х, ҷ after ч). Code-point order would put ё and
# every Tajik letter after я.
_CYRILLIC_ALPHABET = {ch: i for i, ch in enumerate("абвгғдеёжзиӣйкқлмнопрстуӯфхҳчҷшщъыьэюя")}


def name_sort_key(name):
    # Latin (and digits) first in code-point order, then Cyrillic by alphabet.
    return tuple(
        (1, _CYRILLIC_ALPHABET[ch]) if ch in _CYRILLIC_ALPHABET else (0, ord(ch))
        for ch in name.casefold()
    )


def rating_profiles():
    """Every active volunteer in one shared list (no grouping by region — the
    region is shown as small text on each row), by name. Names are sorted in
    Python: SQLite's lower() is ASCII-only, so a DB-side sort would misplace
    Cyrillic / Tajik names."""
    profiles = list(
        Profile.objects.filter(user__is_volunteer=True, user__is_active=True)
        .select_related("user")[:RATING_MAX_ROWS]
    )
    profiles.sort(key=lambda p: (name_sort_key(p.full_name or p.user.username), p.user_id))
    return profiles


def cached_rating_profiles():
    """``rating_profiles()`` through the shared cache (Redis in production)."""
    return cache.get_or_set(RATING_CACHE_KEY, rating_profiles, RATING_TTL)
