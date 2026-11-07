import threading
import time
from collections import defaultdict, deque
from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from .mealie import MIN_STEP_TIMES_VERSION, Mealie, MealieAuthError, MealieError

bp = Blueprint("auth", __name__)

# Failed sign-ins per client address. Gravy Boat passes credentials straight to Mealie, so this
# keeps it from being a faster way to guess Mealie passwords. In memory, per worker process.
FAIL_LIMIT = 10
FAIL_WINDOW = 15 * 60
_failures = defaultdict(deque)
_lock = threading.Lock()


def _too_many_failures(addr):
    now = time.monotonic()
    with _lock:
        q = _failures[addr]
        while q and now - q[0] > FAIL_WINDOW:
            q.popleft()
        return len(q) >= FAIL_LIMIT


def _record_failure(addr):
    with _lock:
        _failures[addr].append(time.monotonic())


def mealie_client():
    return Mealie(current_app.config["MEALIE_URL"], session.get("mealie_token"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("mealie_token") or not session.get("household_id"):
            return redirect(url_for("auth.login", next=request.path))
        g.household_id = session["household_id"]
        g.user_name = session.get("user_name", "")
        g.mealie = mealie_client()
        return view(*args, **kwargs)

    return wrapped


def _safe_next(target):
    if not target:
        return url_for("host.events")
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return url_for("host.events")
    return target


@bp.route("/login", methods=["GET", "POST"])
def login():
    nxt = request.values.get("next", "")
    needs_https = current_app.config["SESSION_COOKIE_SECURE"] and not request.is_secure
    if needs_https:
        flash("Sign-in only works over https://. Open Gravy Boat through its https address.", "warn")
    if request.method == "POST":
        addr = request.remote_addr or "?"
        username = request.form.get("username", "").strip()
        if _too_many_failures(addr):
            flash("Too many failed sign-ins. Wait 15 minutes and try again.", "error")
            return render_template("login.html", next=nxt, username=username), 429
        client = mealie_client()
        client.token = None
        try:
            if request.form.get("api_token", "").strip():
                # For Mealie accounts that sign in with OIDC and have no password.
                client.token = request.form["api_token"].strip()
            else:
                client.login(username, request.form.get("password", ""))
            me = client.me()
            version = client.version()
        except MealieAuthError:
            _record_failure(addr)
            flash("Mealie didn't accept that sign-in.", "error")
            return render_template("login.html", next=nxt, username=username), 401
        except MealieError as e:
            flash(str(e), "error")
            return render_template("login.html", next=nxt, username=username), 502
        guest_ids = {k: v for k, v in session.items() if k.startswith("guest_")}
        session.clear()
        session.update(guest_ids)
        session.permanent = True
        session["mealie_token"] = client.token
        session["household_id"] = me["householdId"]
        session["household_name"] = me.get("household") or ""
        session["group_slug"] = me.get("groupSlug") or current_app.config["MEALIE_GROUP_SLUG"]
        session["user_name"] = me.get("fullName") or me.get("username") or username
        if version is not None and version < MIN_STEP_TIMES_VERSION:
            flash(
                "Your Mealie is older than v3.30, which added step times. Recipes will show no timing until Mealie is updated.",
                "warn",
            )
        return redirect(_safe_next(nxt))
    if session.get("mealie_token"):
        return redirect(_safe_next(nxt))
    return render_template("login.html", next=nxt, username="")


@bp.post("/logout")
def logout():
    guest_ids = {k: v for k, v in session.items() if k.startswith("guest_")}
    session.clear()
    session.update(guest_ids)
    return redirect(url_for("auth.login"))
