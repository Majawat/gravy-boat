# Development

Flask + SQLAlchemy + SQLite, server-rendered Jinja templates, a little vanilla JS. No frontend build.

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
GRAVYBOAT_SECRET_KEY=dev GRAVYBOAT_INSECURE_COOKIES=1 MEALIE_URL=http://localhost:9000 \
  flask --app "app:create_app()" db upgrade
GRAVYBOAT_SECRET_KEY=dev GRAVYBOAT_INSECURE_COOKIES=1 MEALIE_URL=http://localhost:9000 \
  flask --app "app:create_app()" run --port 8794
pytest
```

## Layout
| Path | What |
|---|---|
| `app/scheduler.py` | Backward kitchen scheduler. Pure functions, no Flask. |
| `app/mealie.py` | Mealie API client, shopping-list diffing, building kitchen steps from a recipe. |
| `app/services.py` | Glue between the database, Mealie and the scheduler. |
| `app/host.py` | Pages for signed-in hosts. |
| `app/guest.py` | Pages behind an event's guest link (no login). |
| `app/auth.py` | Sign-in through Mealie (password or API token), failed-attempt limit. |
| `app/models.py` | SQLAlchemy models. |
| `migrations/` | Alembic migrations (batch mode for SQLite). |

## How it works

**Accounts.** There are none. Hosts sign in with Mealie (`POST /api/auth/token`, or a Mealie API
token for single-sign-on accounts). The Mealie token is kept in the signed, HttpOnly session cookie and
every Mealie call runs as that person. Everything is scoped to their Mealie household.

**Guests** use `/g/<token>`, a random per-event link the host can turn off or replace. They pick their
name from the host's list (they can't add people) and answer for everyone in their group: coming or not,
and dietary flags per person. They can claim "needs someone" dishes and add their own. They can't see the
timeline, shopping list or kitchen.

**Step times come from Mealie.** Mealie v3.30+ stores a duration per recipe step. Each timed step becomes
one kitchen step; untimed steps are left off and reported. Mealie has no per-step appliance or temperature,
so those are suggested from the step text and whatever the host sets is remembered per recipe, keyed by
step number plus the start of the step's text. (Mealie regenerates step ids on every save, so ids can't be
used as keys.) Long counter waits ("refrigerate overnight") become prep tasks.

**Scheduler.** Each dish is a chain of steps ending when the dish is ready (relative to serving time).
Dishes are placed longest-first; each may finish early by up to its "can wait" minutes to fit around
others. Heated appliances can share only within a temperature tolerance, need time to change temperature
between uses, and have a capacity. Past the allowed wait (+60 min) a dish is force-placed and reported as
a clash, merged per appliance and time window. More hands-on tasks than cooks at once is a warning.

**Dietary flags** map to Mealie tags (safe / caution / unsafe lists per flag). Tags shaped like
`X-Safe`, `X-Modifiable`, `X-Unsafe` are offered as one-click flags.

**Shopping.** Host dishes linked to Mealie recipes go onto a Mealie shopping list, scaled to headcount
(children count half). Syncing diffs the list's recipe references, so items added by hand are untouched.

**Temperatures** are stored in the kitchen's unit (°F or °C). Switching converts everything saved.

## Changing the schema
Edit `app/models.py`, then:
```
flask --app "app:create_app()" db migrate -m "what changed"
```
Review the generated file (add a `server_default` for new non-null columns), commit it.
`tests/test_migrations.py` fails if models and migrations disagree, and the test app is built from the
migrations. The container runs `flask db upgrade` before starting.

## Tests
- `pytest`: scheduler unit tests and app tests against a fake Mealie.
- Live tests: `GRAVYBOAT_LIVE_TOKEN=<token> MEALIE_URL=... pytest tests/test_live_mealie.py`
  runs against a real Mealie and creates (then deletes) one shopping list. Skipped otherwise.
