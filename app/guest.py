"""Pages reached through an event's private guest link. No login: the link is the key.

Guests pick their name from the host's list (they can't add people), answer for everyone in
their group, set dietary needs, and sign up to bring dishes. They can't see the shopping list, timeline, kitchen or anything outside this event.
"""

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, session, url_for

from . import services as svc
from .models import COURSES, Dish, Event, Guest, Step, db

bp = Blueprint("guest", __name__, url_prefix="/g")


def _event(token):
    ev = Event.query.filter_by(guest_token=token).first()
    if ev is None or not ev.guest_link_enabled:
        abort(404)
    return ev


def _me(ev):
    gid = session.get(f"guest_{ev.id}")
    if gid is None:
        return None
    gst = db.session.get(Guest, gid)
    if gst is None or gst.event_id != ev.id:
        session.pop(f"guest_{ev.id}", None)
        return None
    return gst


def _int(v, default=None, lo=None, hi=None):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _back(ev):
    return redirect(url_for("guest.landing", token=ev.guest_token))


def _own_dish(ev, me, dish_id):
    d = db.session.get(Dish, dish_id)
    if d is None or d.event_id != ev.id or me is None or d.bringer != "guest" or d.bringer_guest_id != me.id:
        abort(403)
    return d


@bp.after_request
def _noindex(resp):
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@bp.get("/<token>")
def landing(token):
    ev = _event(token)
    me = _me(ev)
    all_flags = svc.flags(ev.household_id)
    group_flags = {f.id for m in me.party.members if m.rsvp != "no" for f in m.flags} if me else set()
    menu = [d for d in ev.dishes if d.bringer != "open"]
    return render_template(
        "guest.html",
        ev=ev,
        me=me,
        all_flags=all_flags,
        group_flags=group_flags,
        menu=menu,
        open_dishes=[d for d in ev.dishes if d.bringer == "open"],
        mine=[d for d in ev.dishes if me and d.bringer == "guest" and d.bringer_guest_id == me.id],
        status=svc.diet_status,
        courses=COURSES,
        active=svc.active_flags(ev),
        serve=svc.serve_datetime(ev),
        deg=svc.kitchen_settings(ev.household_id).deg,
        recipe_base=f"{current_app.config['MEALIE_PUBLIC_URL'].rstrip('/')}/g/{ev.group_slug or current_app.config['MEALIE_GROUP_SLUG']}/r/",
    )


@bp.post("/<token>/me")
def choose_me(token):
    ev = _event(token)
    gid = _int(request.form.get("guest_id"))
    gst = db.session.get(Guest, gid) if gid else None
    if gst is None or gst.event_id != ev.id:
        abort(404)
    session.permanent = True
    session[f"guest_{ev.id}"] = gst.id
    return _back(ev)


@bp.post("/<token>/forget")
def forget(token):
    ev = _event(token)
    session.pop(f"guest_{ev.id}", None)
    return _back(ev)


@bp.post("/<token>/rsvp")
def rsvp(token):
    """One form for the whole group: who's coming, and each person's dietary needs."""
    ev = _event(token)
    me = _me(ev)
    if me is None:
        abort(403)
    all_flags = svc.flags(ev.household_id)
    for member in me.party.members:
        answer = request.form.get(f"rsvp_{member.id}")
        if answer in ("yes", "no"):  # left blank = still unanswered
            member.rsvp = answer
        chosen = {_int(x) for x in request.form.getlist(f"flags_{member.id}")}
        member.flags = [f for f in all_flags if f.id in chosen]
    if "note" in request.form:
        me.note = request.form.get("note", "").strip()[:200]
    db.session.commit()
    flash("Thanks, saved.", "ok")
    return _back(ev)


@bp.post("/<token>/claim/<int:dish_id>")
def claim(token, dish_id):
    ev = _event(token)
    me = _me(ev)
    d = db.session.get(Dish, dish_id)
    if me is None or d is None or d.event_id != ev.id:
        abort(403)
    if d.bringer != "open":
        flash("Someone already has that one.", "warn")
        return _back(ev)
    d.bringer, d.bringer_guest_id = "guest", me.id
    db.session.commit()
    flash(f"You're bringing {d.name}. Thank you!", "ok")
    return _back(ev)


@bp.post("/<token>/bring")
def bring(token):
    ev = _event(token)
    me = _me(ev)
    if me is None:
        abort(403)
    name = request.form.get("name", "").strip()
    if not name:
        flash("What are you bringing?", "error")
        return _back(ev)
    d = Dish(
        event=ev,
        name=name[:120],
        course=request.form.get("course") if request.form.get("course") in COURSES else "side",
        bringer="guest",
        bringer_guest_id=me.id,
        guest_added=True,
        sort=len(ev.dishes),
    )
    db.session.add(d)
    _apply_guest_fields(ev, d, request.form)
    db.session.commit()
    flash(f"Added {d.name}. Thank you!", "ok")
    return _back(ev)


@bp.post("/<token>/dish/<int:dish_id>")
def edit_mine(token, dish_id):
    ev = _event(token)
    me = _me(ev)
    d = _own_dish(ev, me, dish_id)
    action = request.form.get("action")
    if action == "delete" and d.guest_added:
        db.session.delete(d)
    elif action in ("release", "delete"):
        d.bringer, d.bringer_guest_id = "open", None
    else:
        if request.form.get("name", "").strip() and not d.mealie_id:
            d.name = request.form["name"].strip()[:120]
        _apply_guest_fields(ev, d, request.form)
    db.session.commit()
    return _back(ev)


def _apply_guest_fields(ev, d, form):
    from datetime import time

    at = form.get("arrive_time", "")
    try:
        d.arrive_time = time.fromisoformat(at) if at else None
    except ValueError:
        d.arrive_time = None
    if not d.mealie_id:
        d.diet = {
            str(f.id): form.get(f"diet_{f.id}")
            for f in svc.flags(ev.household_id)
            if form.get(f"diet_{f.id}") in ("safe", "caution", "unsafe")
        }
    need = form.get("needs", "none")
    minutes = _int(form.get("need_minutes"), 0, 0, 240)
    temp = _int(form.get("need_temp"), None, 40, 600)
    # a guest dish's own kitchen time is one step the guest owns; host-added steps are kept
    d.steps = [s for s in d.steps if s.label not in ("Reheat", "Keep warm", "Finish")]
    if need in ("oven", "burner", "microwave", "slow_cooker") and minutes:
        label = {"oven": "Reheat", "burner": "Reheat", "microwave": "Reheat", "slow_cooker": "Keep warm"}[need]
        d.steps.append(Step(seq=len(d.steps), label=label, minutes=minutes, kind=need, temp=temp if need == "oven" else None))
    d.notes = form.get("notes", d.notes or "").strip()[:500]
