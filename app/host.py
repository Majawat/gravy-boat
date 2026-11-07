from datetime import date, time, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from . import services as svc
from .auth import login_required
from .models import (
    APPLIANCE_KINDS,
    COURSES,
    HEATED_KINDS,
    RSVPS,
    Appliance,
    ChecklistItem,
    DietaryFlag,
    Dish,
    Event,
    Guest,
    Party,
    PrepTask,
    Step,
    ToolCheck,
    db,
    utcnow,
)

bp = Blueprint("host", __name__)

APPLIANCE_PRESETS = {
    "oven": [("Oven", "oven", 2)],
    "double_oven": [("Upper oven", "oven", 2), ("Lower oven", "oven", 2)],
    "burners4": [(f"{pos} burner", "burner", 1) for pos in ("Back left", "Back right", "Front left", "Front right")],
    "burners5": [
        (f"{pos} burner", "burner", 1) for pos in ("Back left", "Back right", "Center", "Front left", "Front right")
    ],
    "burners6": [
        (f"{pos} burner", "burner", 1)
        for pos in ("Back left", "Back center", "Back right", "Front left", "Front center", "Front right")
    ],
    "smoker": [("Pellet smoker", "smoker", 2)],
    "grill": [("Grill", "grill", 1)],
    "slow_cooker": [("Slow cooker", "slow_cooker", 1)],
    "air_fryer": [("Air fryer", "air_fryer", 1)],
    "pressure_cooker": [("Instant Pot", "pressure_cooker", 1)],
    "microwave": [("Microwave", "microwave", 1)],
}


# --- helpers -------------------------------------------------------------------


def _event(event_id):
    ev = db.session.get(Event, event_id)
    if ev is None or ev.household_id != g.household_id:
        abort(404)
    return ev


def _dish(event, dish_id):
    d = db.session.get(Dish, dish_id)
    if d is None or d.event_id != event.id:
        abort(404)
    return d


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


def _float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _date(v):
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def _time(v):
    try:
        return time.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def _back(ev, page):
    return redirect(url_for(f"host.{page}", event_id=ev.id))


def _common(ev, active):
    return {"ev": ev, "active": active, "flags_active": svc.active_flags(ev)}


# --- events --------------------------------------------------------------------


@bp.get("/")
@login_required
def events():
    evs = Event.query.filter_by(household_id=g.household_id).order_by(Event.date.desc()).all()
    today = date.today()
    upcoming = sorted([e for e in evs if e.date >= today], key=lambda e: e.date)
    past = [e for e in evs if e.date < today]
    return render_template(
        "events.html",
        upcoming=upcoming,
        past=past,
        no_kitchen=not svc.appliances(g.household_id),
    )


@bp.route("/events/new", methods=["GET", "POST"])
@login_required
def new_event():
    evs = Event.query.filter_by(household_id=g.household_id).order_by(Event.date.desc()).all()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        d = _date(request.form.get("date"))
        t = _time(request.form.get("serve_time"))
        if not name or not d or not t:
            flash("Name, date and serve time are all needed.", "error")
            return render_template("event_new.html", evs=evs, form=request.form), 400
        src = None
        if request.form.get("copy_from"):
            src = db.session.get(Event, _int(request.form["copy_from"]))
            if src is None or src.household_id != g.household_id:
                abort(404)
        ev = svc.new_event(g.household_id, name, d, t, g.user_name, src, session.get("group_slug"))
        db.session.commit()
        return redirect(url_for("host.overview", event_id=ev.id))
    return render_template("event_new.html", evs=evs, form={"copy_from": request.args.get("copy", "")})


@bp.get("/events/<int:event_id>")
@login_required
def overview(event_id):
    ev = _event(event_id)
    schedule, apps = svc.build_event_schedule(ev)
    first = svc.when(ev, schedule.start) if schedule.placements else None
    open_dishes = [d for d in ev.dishes if d.bringer == "open"]
    prep_open = [(t, ev.date - timedelta(days=t.days_before)) for t in ev.prep_tasks if not t.done]
    return render_template(
        "overview.html",
        **_common(ev, "overview"),
        schedule=schedule,
        first=first,
        open_dishes=open_dishes,
        prep_open=prep_open,
        guest_url=url_for("guest.landing", token=ev.guest_token, _external=True),
        no_kitchen=not apps,
        dishes_by_id={d.id: d for d in ev.dishes},
        flag_summary=[
            (f, who, [(d, svc.diet_status(d, f)) for d in ev.dishes]) for f, who in svc.active_flags(ev)
        ],
    )


@bp.post("/events/<int:event_id>/edit")
@login_required
def edit_event(event_id):
    ev = _event(event_id)
    name = request.form.get("name", "").strip()
    d = _date(request.form.get("date"))
    t = _time(request.form.get("serve_time"))
    if name:
        ev.name = name
    if d:
        ev.date = d
    if t:
        ev.serve_time = t
    ev.location = request.form.get("location", ev.location).strip()
    ev.notes = request.form.get("notes", ev.notes).strip()
    db.session.commit()
    flash("Saved.", "ok")
    return _back(ev, "overview")


@bp.post("/events/<int:event_id>/delete")
@login_required
def delete_event(event_id):
    ev = _event(event_id)
    if request.form.get("confirm") != ev.name:
        flash("Type the event name exactly to delete it.", "error")
        return _back(ev, "overview")
    db.session.delete(ev)
    db.session.commit()
    flash(f"Deleted {ev.name}.", "ok")
    return redirect(url_for("host.events"))


@bp.post("/events/<int:event_id>/link")
@login_required
def guest_link(event_id):
    ev = _event(event_id)
    action = request.form.get("action")
    if action == "rotate":
        svc.rotate_guest_token(ev)
        ev.guest_link_enabled = True
        flash("New guest link made. The old one no longer works.", "ok")
    elif action == "disable":
        ev.guest_link_enabled = False
        flash("Guest link turned off.", "ok")
    elif action == "enable":
        ev.guest_link_enabled = True
        flash("Guest link turned on.", "ok")
    db.session.commit()
    return _back(ev, "overview")


@bp.post("/events/<int:event_id>/shift")
@login_required
def shift_serve(event_id):
    ev = _event(event_id)
    mins = _int(request.form.get("minutes"), 0, -240, 240)
    ev.serve_time = (svc.serve_datetime(ev) + timedelta(minutes=mins)).time()
    db.session.commit()
    flash(f"Serve time is now {svc.serve_datetime(ev):%-I:%M %p}.", "ok")
    from .auth import _safe_next

    back = request.form.get("back")
    return redirect(_safe_next(back) if back else url_for("host.today", event_id=ev.id))


# --- guests --------------------------------------------------------------------


@bp.get("/events/<int:event_id>/guests")
@login_required
def guests(event_id):
    ev = _event(event_id)
    return render_template("guests.html", **_common(ev, "guests"), all_flags=svc.flags(g.household_id), rsvps=RSVPS)


def _guest_from_form(gst, form, all_flags, ev):
    name = form.get("name", "").strip()
    if name:
        gst.name = name[:80]
    if form.get("age") in ("adult", "child"):
        gst.age = form["age"]
    if form.get("rsvp") in RSVPS:
        gst.rsvp = form["rsvp"]
    gst.note = form.get("note", gst.note or "").strip()[:200]
    chosen = {_int(x) for x in form.getlist("flags")}
    gst.flags = [f for f in all_flags if f.id in chosen]
    target = form.get("party", "")
    old = gst.party
    if target == "new":
        gst.party = Party(event=ev, sort=len(ev.parties))
    elif target:
        p = db.session.get(Party, _int(target))
        if p is not None and p.event_id == ev.id:
            gst.party = p
    if old is not None and old is not gst.party and not [m for m in old.members if m is not gst]:
        db.session.delete(old)


@bp.post("/events/<int:event_id>/guests")
@login_required
def add_guest(event_id):
    """Names separated by commas or new lines. Together = one group that RSVPs together."""
    ev = _event(event_id)
    names = [n.strip()[:80] for n in request.form.get("name", "").replace("\n", ",").split(",") if n.strip()]
    if not names:
        flash("Enter a name.", "error")
        return _back(ev, "guests")
    all_flags = svc.flags(g.household_id)
    chosen = {_int(x) for x in request.form.getlist("flags")}
    age = request.form.get("age") if request.form.get("age") in ("adult", "child") else "adult"
    rsvp = request.form.get("rsvp") if request.form.get("rsvp") in RSVPS else "invited"
    together = request.form.get("together") == "1"
    existing = db.session.get(Party, _int(request.form.get("party"))) if request.form.get("party") else None
    if existing is not None and existing.event_id != ev.id:
        abort(404)
    party = existing
    for n in names:
        if party is None or not (together or existing):
            party = Party(event=ev, sort=len(ev.parties))
        db.session.add(
            Guest(event=ev, party=party, name=n, age=age, rsvp=rsvp, flags=[f for f in all_flags if f.id in chosen])
        )
    db.session.commit()
    return _back(ev, "guests")


@bp.post("/events/<int:event_id>/guests/<int:guest_id>")
@login_required
def update_guest(event_id, guest_id):
    ev = _event(event_id)
    gst = db.session.get(Guest, guest_id)
    if gst is None or gst.event_id != ev.id:
        abort(404)
    if request.form.get("action") == "delete":
        for d in ev.dishes:
            if d.bringer_guest_id == gst.id:
                d.bringer, d.bringer_guest_id = "open", None
        party = gst.party
        db.session.delete(gst)
        db.session.flush()
        if party is not None and not party.members:
            db.session.delete(party)
    else:
        _guest_from_form(gst, request.form, svc.flags(g.household_id), ev)
    db.session.commit()
    return _back(ev, "guests")


# --- menu ----------------------------------------------------------------------


def _refresh_stale(ev):
    stale = utcnow() - timedelta(hours=1)
    changed = False
    for d in ev.dishes:
        if d.mealie_id and (d.synced_at is None or d.synced_at < stale):
            try:
                svc.refresh_from_mealie(d, g.mealie)
                changed = True
            except Exception as e:  # recipe deleted in Mealie, or Mealie down: keep the snapshot
                from .mealie import MealieAuthError

                if isinstance(e, MealieAuthError):
                    raise
    if changed:
        db.session.commit()


@bp.get("/events/<int:event_id>/menu")
@login_required
def menu(event_id):
    ev = _event(event_id)
    _refresh_stale(ev)
    groups = [
        ("Hosting", [d for d in ev.dishes if d.bringer == "host"]),
        ("Guests are bringing", [d for d in ev.dishes if d.bringer == "guest"]),
        ("Still needs someone", [d for d in ev.dishes if d.bringer == "open"]),
    ]
    return render_template(
        "menu.html",
        **_common(ev, "menu"),
        groups=groups,
        courses=COURSES,
        headcount=ev.headcount,
        scale=lambda d: svc.effective_scale(d, ev.headcount),
        status=svc.diet_status,
    )


@bp.get("/events/<int:event_id>/menu/add")
@login_required
def recipe_search(event_id):
    ev = _event(event_id)
    q = request.args.get("q", "").strip()
    results = g.mealie.search(q, per_page=30)
    on_menu = {d.mealie_id for d in ev.dishes if d.mealie_id}
    return render_template("recipe_search.html", **_common(ev, "menu"), q=q, results=results, on_menu=on_menu, courses=COURSES)


def _course_guess(tags, categories):
    names = {t.lower() for t in tags} | {c.lower() for c in categories}
    for key, course in (
        ("dessert", "dessert"),
        ("appetizer", "appetizer"),
        ("side dish", "side"),
        ("main course", "main"),
        ("dinner", "main"),
        ("alcoholic drinks", "drink"),
        ("non-alcoholic drinks", "drink"),
        ("bread", "bread"),
    ):
        if key in names:
            return course
    return "side"


@bp.post("/events/<int:event_id>/menu/recipe")
@login_required
def add_recipe(event_id):
    ev = _event(event_id)
    slug = request.form.get("slug", "")
    recipe = g.mealie.recipe(slug)
    ks = svc.kitchen_settings(g.household_id)
    d = Dish(
        event=ev,
        name=recipe["name"],
        course=request.form.get("course")
        if request.form.get("course") in COURSES
        else _course_guess([t["name"] for t in recipe.get("tags") or []], [c["name"] for c in recipe.get("recipeCategory") or []]),
        bringer=request.form.get("bringer") if request.form.get("bringer") in ("host", "open") else "host",
        sort=len(ev.dishes),
    )
    db.session.add(d)
    untimed = svc.apply_recipe(d, recipe, g.household_id, ks.default_hold)
    db.session.commit()
    if not d.steps:
        flash(f"Added {d.name}, but none of its steps have a time in Mealie yet. Add step times there, then Refresh.", "warn")
        return _back(ev, "menu")
    if untimed:
        flash(f"Added {d.name}. {untimed} of its steps have no time in Mealie, so they're left off the timeline.", "warn")
    else:
        flash(f"Added {d.name} with its step times from Mealie.", "ok")
    return redirect(url_for("host.dish", event_id=ev.id, dish_id=d.id))


@bp.post("/events/<int:event_id>/menu/dish")
@login_required
def add_dish(event_id):
    ev = _event(event_id)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Give the dish a name.", "error")
        return _back(ev, "menu")
    bringer = request.form.get("bringer", "host")
    gid = None
    if bringer.startswith("guest:"):
        gid = _int(bringer.split(":", 1)[1])
        if not any(x.id == gid for x in ev.guests):
            abort(400)
        bringer = "guest"
    elif bringer not in ("host", "open"):
        bringer = "host"
    course = request.form.get("course") if request.form.get("course") in COURSES else "side"
    d = Dish(
        event=ev,
        name=name[:120],
        course=course,
        bringer=bringer,
        bringer_guest_id=gid,
        sort=len(ev.dishes),
        max_hold=svc.kitchen_settings(g.household_id).default_hold,
    )
    db.session.add(d)
    db.session.commit()
    return _back(ev, "menu")


@bp.post("/events/<int:event_id>/menu/refresh")
@login_required
def refresh_menu(event_id):
    ev = _event(event_id)
    for d in ev.dishes:
        if d.mealie_id:
            svc.refresh_from_mealie(d, g.mealie)
    db.session.commit()
    flash("Tags, tools and servings refreshed from Mealie.", "ok")
    return _back(ev, "menu")


@bp.route("/events/<int:event_id>/dishes/<int:dish_id>", methods=["GET", "POST"])
@login_required
def dish(event_id, dish_id):
    ev = _event(event_id)
    d = _dish(ev, dish_id)
    apps = svc.appliances(g.household_id)
    all_flags = svc.flags(g.household_id)
    if request.method == "POST":
        if request.form.get("action") == "delete":
            db.session.delete(d)
            db.session.commit()
            flash(f"Removed {d.name}.", "ok")
            return _back(ev, "menu")
        _save_dish(ev, d, request.form, apps, all_flags)
        db.session.commit()
        flash("Saved." + (" Timing remembered for next time." if d.mealie_id else ""), "ok")
        if request.form.get("then") == "timeline":
            return _back(ev, "timeline")
        return redirect(url_for("host.dish", event_id=ev.id, dish_id=d.id))
    return render_template(
        "dish.html",
        **_common(ev, "menu"),
        d=d,
        apps=apps,
        courses=COURSES,
        all_flags=all_flags,
        auto_scale=svc.auto_scale(d, ev.headcount),
        status=svc.diet_status,
        heated=HEATED_KINDS,
    )


def _save_dish(ev, d, form, apps, all_flags):
    name = form.get("name", "").strip()
    if name and not d.mealie_id:
        d.name = name[:120]
    if form.get("course") in COURSES:
        d.course = form["course"]
    bringer = form.get("bringer", d.bringer)
    if bringer.startswith("guest:"):
        gid = _int(bringer.split(":", 1)[1])
        if any(x.id == gid for x in ev.guests):
            d.bringer, d.bringer_guest_id = "guest", gid
    elif bringer in ("host", "open"):
        d.bringer, d.bringer_guest_id = bringer, None
    d.scale = _float(form.get("scale")) if form.get("scale", "").strip() else None
    if d.scale is not None and d.scale <= 0:
        d.scale = None
    d.serveware = form.get("serveware", "").strip()[:200]
    d.notes = form.get("notes", "").strip()
    ready = _int(form.get("ready_at"), 0, -600, 600)
    d.ready_at = ready
    d.max_hold = _int(form.get("max_hold"), d.max_hold, 0, 600)
    d.arrive_time = _time(form.get("arrive_time")) if form.get("arrive_time") else None
    if not d.mealie_id:
        d.diet = {str(f.id): form.get(f"diet_{f.id}") for f in all_flags if form.get(f"diet_{f.id}") in ("safe", "caution", "unsafe")}

    app_ids = {a.id: a for a in apps}
    from_mealie = {s.source_id: s for s in d.steps if s.source_id}
    new_steps = []
    for i in range(_int(form.get("step_count"), 0, 0, 40)):
        source = form.get(f"s{i}_source") or None
        if d.mealie_id and source not in from_mealie:
            continue  # a Mealie dish's steps are exactly its timed Mealie steps
        label = form.get(f"s{i}_label", "").strip()
        minutes = from_mealie[source].minutes if source else _int(form.get(f"s{i}_minutes"), 0, 0, 60 * 72)
        if source and not label:
            label = from_mealie[source].label
        if not label or not minutes:
            continue
        where = form.get(f"s{i}_where", "")
        kind, app_id = None, None
        if where.startswith("app:"):
            a = app_ids.get(_int(where[4:]))
            if a:
                kind, app_id = a.kind, a.id
        elif where in APPLIANCE_KINDS:
            kind = where
        temp = _int(form.get(f"s{i}_temp"), None, 40, 1000) if kind in HEATED_KINDS else None
        new_steps.append(
            Step(seq=_int(form.get(f"s{i}_seq"), i), source_id=source, source_text=from_mealie[source].source_text if source else None, label=label[:80], minutes=minutes, kind=kind, appliance_id=app_id, temp=temp, hands_on=form.get(f"s{i}_hands") == "1")
        )
    new_steps.sort(key=lambda s: s.seq)
    for i, s in enumerate(new_steps):
        s.seq = i
    d.steps = new_steps
    svc.remember_timing(d, ev.household_id)


# --- timeline ------------------------------------------------------------------


def _timeline_ctx(ev):
    schedule, apps = svc.build_event_schedule(ev)
    show = svc.run_of_show(ev, schedule, apps)
    start = (schedule.start // 60) * 60 if schedule.placements else -60
    end = -(-schedule.end // 60) * 60 if schedule.placements else 0
    end = max(end, start + 60)
    hours_span = (end - start) / 60
    every = 60 if hours_span <= 12 else 120 if hours_span <= 24 else 240
    hours = []
    for i, m in enumerate(range(start, end + 1, every)):
        dt = svc.clock(ev, m)
        label = dt.strftime("%-I %p")
        if dt.date() != ev.date and (i == 0 or dt.hour < every // 60):
            label = f"{dt:%a} {label}"  # name the day where it starts and at midnight
        hours.append((m, label))
    dish_rows = []
    for d in ev.dishes:
        r = schedule.dishes.get(d.id)
        if r and r.placements:
            dish_rows.append((d, r))
    dish_rows.sort(key=lambda x: (x[1].start, x[0].id))
    app_rows = []
    for a in apps.values():
        ps = [p for p in schedule.placements if p.appliance_id == a.id]
        heats = [h for h in schedule.preheats if h.appliance_id == a.id]
        if ps or heats:
            app_rows.append((a, ps, heats))
    return {
        "schedule": schedule,
        "show": show,
        "span": (start, end),
        "hours": hours,
        "dish_rows": dish_rows,
        "app_rows": app_rows,
        "apps": apps,
        "dishes_by_id": {d.id: d for d in ev.dishes},
        "untimed": [d for d in ev.dishes if not any(s.minutes for s in d.steps)],
    }


@bp.get("/events/<int:event_id>/timeline")
@login_required
def timeline(event_id):
    ev = _event(event_id)
    return render_template("timeline.html", **_common(ev, "timeline"), **_timeline_ctx(ev))


@bp.get("/events/<int:event_id>/today")
@login_required
def today(event_id):
    ev = _event(event_id)
    ctx = _timeline_ctx(ev)
    return render_template("today.html", ev=ev, **ctx, serve=svc.serve_datetime(ev))


@bp.get("/events/<int:event_id>/print")
@login_required
def print_view(event_id):
    ev = _event(event_id)
    ctx = _timeline_ctx(ev)
    return render_template(
        "print.html",
        ev=ev,
        **ctx,
        cookware=svc.cookware(ev),
        buckets=_prep_buckets(ev),
        scale=lambda d: svc.effective_scale(d, ev.headcount),
    )


# --- prep ----------------------------------------------------------------------


def _prep_buckets(ev):
    out = {}
    for t in sorted(ev.prep_tasks, key=lambda t: (-t.days_before, t.id)):
        out.setdefault(svc.bucket(t.days_before), []).append((t, ev.date - timedelta(days=t.days_before)))
    return [(name, out[name]) for _, name in svc.PREP_BUCKETS if name in out]


@bp.get("/events/<int:event_id>/prep")
@login_required
def prep(event_id):
    ev = _event(event_id)
    return render_template("prep.html", **_common(ev, "prep"), buckets=_prep_buckets(ev))


@bp.post("/events/<int:event_id>/prep")
@login_required
def prep_post(event_id):
    ev = _event(event_id)
    action = request.form.get("action")
    if action == "add":
        label = request.form.get("label", "").strip()
        when = request.form.get("when", "")
        days = _int(request.form.get("days_before"), None, 0, 120)
        on = _date(when)
        if on:
            days = max(0, (ev.date - on).days)
        if label and days is not None:
            dish_id = _int(request.form.get("dish_id"))
            if dish_id and not any(d.id == dish_id for d in ev.dishes):
                dish_id = None
            db.session.add(PrepTask(event=ev, label=label[:200], days_before=days, dish_id=dish_id))
        else:
            flash("A task needs a description and a day.", "error")
    elif action == "thaw":
        lbs = _float(request.form.get("pounds"))
        if lbs and lbs > 0:
            days = svc.thaw_days(lbs)
            db.session.add(PrepTask(event=ev, label=f"Move the {lbs:g} lb turkey to the fridge to thaw ({days} days)", days_before=days + _int(request.form.get("extra"), 0, 0, 7)))
    elif action in ("toggle", "delete"):
        t = db.session.get(PrepTask, _int(request.form.get("task_id")))
        if t is None or t.event_id != ev.id:
            abort(404)
        if action == "toggle":
            t.done = not t.done
        else:
            db.session.delete(t)
    db.session.commit()
    return _back(ev, "prep")


# --- shopping ------------------------------------------------------------------


@bp.get("/events/<int:event_id>/shopping")
@login_required
def shopping(event_id):
    ev = _event(event_id)
    hc = ev.headcount
    host_dishes = [d for d in ev.dishes if d.bringer == "host"]
    return render_template(
        "shopping.html",
        **_common(ev, "shopping"),
        host_dishes=host_dishes,
        scale=lambda d: svc.effective_scale(d, hc),
        headcount=hc,
    )


@bp.post("/events/<int:event_id>/shopping")
@login_required
def shopping_sync(event_id):
    ev = _event(event_id)
    adds, removes = svc.sync_shopping(ev, g.mealie)
    db.session.commit()
    if not adds and not removes:
        flash("The Mealie list already matches the menu.", "ok")
    else:
        flash(f"Mealie list updated: {len(adds)} recipe change(s) added, {len(removes)} reduced or removed.", "ok")
    return _back(ev, "shopping")


# --- checklist -----------------------------------------------------------------


@bp.get("/events/<int:event_id>/checklist")
@login_required
def checklist(event_id):
    ev = _event(event_id)
    checked = {t.name for t in ev.tool_checks}
    return render_template(
        "checklist.html",
        **_common(ev, "checklist"),
        cookware=svc.cookware(ev),
        checked=checked,
        serveware=[d for d in ev.dishes if d.serveware],
    )


@bp.post("/events/<int:event_id>/checklist")
@login_required
def checklist_post(event_id):
    ev = _event(event_id)
    action = request.form.get("action")
    if action == "add":
        name = request.form.get("name", "").strip()
        if name:
            mode = request.form.get("mode")
            n = _float(request.form.get("count"))
            item = ChecklistItem(event=ev, name=name[:120], sort=len(ev.checklist))
            if mode == "per_guest" and n is not None:
                item.per_guest = n
            elif n is not None:
                item.qty = int(n)
            db.session.add(item)
    elif action in ("toggle", "delete"):
        item = db.session.get(ChecklistItem, _int(request.form.get("item_id")))
        if item is None or item.event_id != ev.id:
            abort(404)
        if action == "toggle":
            item.done = not item.done
        else:
            db.session.delete(item)
    elif action == "tool":
        name = request.form.get("name", "")
        existing = db.session.get(ToolCheck, (ev.id, name))
        if existing:
            db.session.delete(existing)
        elif name:
            db.session.add(ToolCheck(event_id=ev.id, name=name[:120]))
    db.session.commit()
    return _back(ev, "checklist")


# --- kitchen & dietary flags -----------------------------------------------------


@bp.get("/kitchen")
@login_required
def kitchen():
    existing = svc.flags(g.household_id)
    try:
        tag_names = [t["name"] for t in g.mealie.tags()]
    except Exception as e:
        from .mealie import MealieAuthError

        if isinstance(e, MealieAuthError):
            raise
        tag_names = []
    return render_template(
        "kitchen.html",
        apps=svc.appliances(g.household_id),
        ks=svc.kitchen_settings(g.household_id),
        flags=existing,
        tag_names=sorted(tag_names, key=str.lower),
        suggestions=svc.suggest_flags(tag_names, existing),
        presets=APPLIANCE_PRESETS,
    )


@bp.post("/kitchen")
@login_required
def kitchen_post():
    hid = g.household_id
    action = request.form.get("action")
    apps = svc.appliances(hid)
    if action == "preset":
        for name, kind, cap in APPLIANCE_PRESETS.get(request.form.get("preset"), []):
            db.session.add(Appliance(household_id=hid, name=name, kind=kind, capacity=cap, sort=len(apps)))
            apps.append(None)
    elif action == "add_appliance":
        name = request.form.get("name", "").strip()
        kind = request.form.get("kind")
        if name and kind in APPLIANCE_KINDS:
            db.session.add(Appliance(household_id=hid, name=name[:80], kind=kind, capacity=_int(request.form.get("capacity"), 1, 1, 20), sort=len(apps)))
    elif action in ("update_appliance", "delete_appliance"):
        a = db.session.get(Appliance, _int(request.form.get("id")))
        if a is None or a.household_id != hid:
            abort(404)
        if action == "delete_appliance":
            db.session.delete(a)
        else:
            a.name = request.form.get("name", a.name).strip()[:80] or a.name
            if request.form.get("kind") in APPLIANCE_KINDS:
                a.kind = request.form["kind"]
            a.capacity = _int(request.form.get("capacity"), a.capacity, 1, 20)
    elif action == "settings":
        ks = svc.kitchen_settings(hid)
        ks.temp_tolerance = _int(request.form.get("temp_tolerance"), ks.temp_tolerance, 0, 200)
        ks.minutes_per_10f = _float(request.form.get("minutes_per_10f"), ks.minutes_per_10f)
        ks.preheat_minutes = _int(request.form.get("preheat_minutes"), ks.preheat_minutes, 0, 120)
        ks.default_hold = _int(request.form.get("default_hold"), ks.default_hold, 0, 600)
        ks.cooks = _int(request.form.get("cooks"), ks.cooks, 1, 20)
        if request.form.get("temp_unit") in ("F", "C"):
            svc.change_temp_unit(hid, request.form["temp_unit"])
    elif action == "add_flag":
        name = request.form.get("name", "").strip()
        if name:
            db.session.add(
                DietaryFlag(
                    household_id=hid,
                    name=name[:80],
                    safe_tags=request.form.getlist("safe"),
                    caution_tags=request.form.getlist("caution"),
                    unsafe_tags=request.form.getlist("unsafe"),
                    sort=len(svc.flags(hid)),
                )
            )
    elif action in ("update_flag", "delete_flag"):
        f = db.session.get(DietaryFlag, _int(request.form.get("id")))
        if f is None or f.household_id != hid:
            abort(404)
        if action == "delete_flag":
            db.session.delete(f)
        else:
            f.name = request.form.get("name", f.name).strip()[:80] or f.name
            f.safe_tags = request.form.getlist("safe")
            f.caution_tags = request.form.getlist("caution")
            f.unsafe_tags = request.form.getlist("unsafe")
    db.session.commit()
    return redirect(url_for("host.kitchen") + (request.form.get("anchor") or ""))
