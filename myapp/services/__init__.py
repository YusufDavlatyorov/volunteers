"""Service layer for myapp.

Business logic that needs to be unit-testable without the ORM or the network —
or that would otherwise bloat views.py — lives here, one module per concern:

    geo            pure (lat, lng) math + the OSRM routing client (never raises)
    maps           provider-agnostic facade over tiles / routing / geocoding
    matching       deterministic, explainable volunteer<->task scoring
    analytics      read-only CRM aggregate queries
    overdue        detection + alerting for tasks active past the 3h threshold
    emergency      volunteer SOS reports: dedup, location, notification fan-out
    telegram_link  redemption of one-time Telegram account-link codes

Conventions:
  - Functions take and return plain data (numbers, dicts, model instances), not
    request/response objects. Views do permission checks and HTTP.
  - I/O helpers degrade gracefully instead of raising for an expected failure
    (a geocode miss returns None, a routing failure returns a fallback dict).
  - No notifications or writes as a side effect of a "read"/"recommend" call —
    those stay explicit in the view.
"""
