from datetime import date

from flask import current_app, g, session

from .models import APPLIANCE_KINDS
from .services import fmt_minutes, when


def _clock(dt):
    return dt.strftime("%-I:%M %p").replace(":00 ", " ") if dt else ""


def _qty(x):
    if x is None:
        return ""
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _relday(d):
    delta = (d - date.today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta > 1:
        return f"in {delta} days"
    if delta == -1:
        return "yesterday"
    return f"{-delta} days ago"


def register(app):
    app.jinja_env.filters["clock"] = _clock
    app.jinja_env.filters["qty"] = _qty
    app.jinja_env.filters["dur"] = fmt_minutes
    app.jinja_env.filters["relday"] = _relday
    app.jinja_env.globals["when_at"] = when  # globals reach imported macros; context processors don't
    app.jinja_env.filters["kindname"] = lambda k: APPLIANCE_KINDS.get(k, k or "Counter")

    @app.context_processor
    def _ctx():
        # Pages for a signed-in host use their kitchen's unit and Mealie group; guest pages pass both.
        from .services import kitchen_settings

        hid = getattr(g, "household_id", None)
        public = current_app.config["MEALIE_PUBLIC_URL"].rstrip("/")
        group = session.get("group_slug") or current_app.config["MEALIE_GROUP_SLUG"]
        return {
            "APPLIANCE_KINDS": APPLIANCE_KINDS,
            "mealie_public": public,
            "recipe_base": f"{public}/g/{group}/r/",
            "deg": kitchen_settings(hid).deg if hid else "°F",
        }
