import math
from datetime import datetime, timedelta

from . import scheduler as sch
from .mealie import list_recipe_quantities, recipe_summary, shopping_diff
from .models import (
    DEFAULT_GENERALWARE,
    Appliance,
    ChecklistItem,
    DietaryFlag,
    Dish,
    Event,
    Guest,
    KitchenSettings,
    Party,
    PrepTask,
    RecipeTiming,
    Step,
    db,
    new_token,
    utcnow,
)

STATUS_ORDER = {"unsafe": 0, "unknown": 1, "caution": 2, "safe": 3}


# --- kitchen -------------------------------------------------------------------


def kitchen_settings(household_id):
    ks = db.session.get(KitchenSettings, household_id)
    if ks is None:
        ks = KitchenSettings(household_id=household_id)
        db.session.add(ks)
        db.session.flush()
    return ks


def appliances(household_id):
    return Appliance.query.filter_by(household_id=household_id).order_by(Appliance.sort, Appliance.id).all()


def flags(household_id):
    return DietaryFlag.query.filter_by(household_id=household_id).order_by(DietaryFlag.sort, DietaryFlag.id).all()


def suggest_flags(tag_names, existing):
    """Mealie tags shaped like 'X-Safe' / 'X-Modifiable' / 'X-Unsafe' become flag suggestions."""
    used = {t for f in existing for t in (f.safe_tags + f.caution_tags + f.unsafe_tags)}
    out = []
    for name in sorted(tag_names):
        low = name.lower()
        if not low.endswith("-safe") or name in used:
            continue
        base = name[: -len("-safe")]
        sibling = {t.lower(): t for t in tag_names}
        out.append(
            {
                "name": base,
                "safe": [name],
                "caution": [sibling[f"{base.lower()}-modifiable"]] if f"{base.lower()}-modifiable" in sibling else [],
                "unsafe": [sibling[f"{base.lower()}-unsafe"]] if f"{base.lower()}-unsafe" in sibling else [],
            }
        )
    return out


# --- dishes --------------------------------------------------------------------


def auto_scale(dish, headcount):
    if not dish.recipe_servings or headcount <= 0:
        return 1.0
    return max(0.5, math.ceil(headcount / dish.recipe_servings * 2) / 2)


def effective_scale(dish, headcount):
    return dish.scale if dish.scale is not None else auto_scale(dish, headcount)


def diet_status(dish, flag):
    if dish.from_mealie:
        tags = {t.lower() for t in dish.mealie_tags or []}
        if any(t.lower() in tags for t in flag.unsafe_tags):
            return "unsafe"
        if any(t.lower() in tags for t in flag.safe_tags):
            return "safe"
        if any(t.lower() in tags for t in flag.caution_tags):
            return "caution"
        return "unknown"
    return (dish.diet or {}).get(str(flag.id), "unknown")


def active_flags(event):
    """Flags carried by guests who are coming, with who carries them."""
    seen = {}
    for g in event.attending:
        for f in g.flags:
            seen.setdefault(f.id, (f, []))[1].append(g)
    return list(seen.values())


def _overrides(household_id, mealie_id, dish=None):
    """Host settings per Mealie instruction id: this dish's current steps win over remembered ones."""
    out = {}
    remembered = db.session.get(RecipeTiming, (household_id, mealie_id))
    if remembered:
        out.update({s["source_id"]: s for s in remembered.steps if s.get("source_id")})
    if dish is not None:
        out.update({s.source_id: s.as_dict() for s in dish.steps if s.source_id})
    return out, remembered


def _set_steps(dish, recipe, overrides):
    from .mealie import steps_from_recipe

    unit = kitchen_settings(dish.event.household_id).temp_unit
    steps, ahead, untimed = steps_from_recipe(recipe, overrides, unit)
    keys = ("source_id", "source_text", "label", "minutes", "kind", "appliance_id", "temp", "hands_on")
    dish.steps = [Step(seq=i, **{k: st.get(k) for k in keys}) for i, st in enumerate(steps)]
    return ahead, untimed


def apply_recipe(dish, recipe, household_id, default_hold):
    """Link a dish to a Mealie recipe. Returns how many of its steps have no time in Mealie."""
    info = recipe_summary(recipe)
    dish.mealie_id = info["id"]
    dish.mealie_slug = info["slug"]
    dish.recipe_servings = info["servings"]
    dish.mealie_tags = info["tags"]
    dish.mealie_tools = info["tools"]
    dish.synced_at = utcnow()
    overrides, remembered = _overrides(household_id, info["id"])
    if remembered:
        dish.ready_at = remembered.ready_at
        dish.max_hold = remembered.max_hold
    ahead, untimed = _set_steps(dish, recipe, overrides)
    if not remembered:
        for text in ahead:
            dish.event.prep_tasks.append(PrepTask(label=f"{info['name']}: {text[:180]}", days_before=1, dish=dish))
    if dish.max_hold is None:
        dish.max_hold = default_hold
    return untimed


def refresh_from_mealie(dish, client):
    """Pick up changes made in Mealie: tags, tools, servings and step times."""
    recipe = client.recipe(dish.mealie_id)
    info = recipe_summary(recipe)
    dish.mealie_slug = info["slug"]
    dish.recipe_servings = info["servings"]
    dish.mealie_tags = info["tags"]
    dish.mealie_tools = info["tools"]
    dish.synced_at = utcnow()
    if dish.steps and not any(s.source_id for s in dish.steps):
        return  # hand-entered timing (e.g. imported from an old sheet) isn't Mealie's to replace
    overrides, _ = _overrides(dish.event.household_id, dish.mealie_id, dish)
    _set_steps(dish, recipe, overrides)


def remember_timing(dish, household_id):
    if not dish.mealie_id:
        return
    rt = db.session.get(RecipeTiming, (household_id, dish.mealie_id))
    if rt is None:
        rt = RecipeTiming(household_id=household_id, mealie_id=dish.mealie_id)
        db.session.add(rt)
    rt.steps = [{k: v for k, v in s.as_dict().items() if k != "minutes"} for s in dish.steps if s.source_id]
    rt.ready_at = dish.ready_at
    rt.max_hold = dish.max_hold


# --- events --------------------------------------------------------------------


def serve_datetime(event):
    return datetime.combine(event.date, event.serve_time)


def clock(event, minutes):
    return serve_datetime(event) + timedelta(minutes=minutes)


def when(event, minutes):
    """'2 PM' on the day itself, 'Wed 9 PM' for anything on another day (overnight cooks)."""
    dt = clock(event, minutes)
    t = dt.strftime("%-I:%M %p").replace(":00 ", " ")
    return t if dt.date() == event.date else f"{dt:%a} {t}"


def new_event(household_id, name, date, serve_time, created_by, copy_from=None, group_slug=None):
    ev = Event(household_id=household_id, name=name, date=date, serve_time=serve_time, created_by=created_by, group_slug=group_slug)
    db.session.add(ev)
    if copy_from is None:
        for i, (item, qty, per) in enumerate(DEFAULT_GENERALWARE):
            ev.checklist.append(ChecklistItem(name=item, qty=qty, per_guest=per, sort=i))
        return ev

    src = copy_from
    ev.location = src.location
    ev.notes = src.notes
    guest_map = {}
    for p in src.parties:
        np = Party(event=ev, sort=p.sort)
        db.session.add(np)  # SQLAlchemy 2 doesn't cascade through backrefs
        for g in p.members:
            ng = Guest(event=ev, party=np, name=g.name, age=g.age, note=g.note, flags=list(g.flags))
            db.session.add(ng)
            guest_map[g.id] = ng
    dish_map = {}
    for d in src.dishes:
        nd = Dish(
            name=d.name,
            course=d.course,
            bringer=d.bringer,
            bringer_guest=guest_map.get(d.bringer_guest_id),
            mealie_id=d.mealie_id,
            mealie_slug=d.mealie_slug,
            recipe_servings=d.recipe_servings,
            scale=d.scale,
            mealie_tags=list(d.mealie_tags or []),
            mealie_tools=list(d.mealie_tools or []),
            diet=dict(d.diet or {}),
            serveware=d.serveware,
            notes=d.notes,
            ready_at=d.ready_at,
            max_hold=d.max_hold,
            arrive_time=d.arrive_time,
            guest_added=d.guest_added,
            sort=d.sort,
            steps=[
                Step(seq=s.seq, source_id=s.source_id, source_text=s.source_text, label=s.label, minutes=s.minutes, kind=s.kind, appliance_id=s.appliance_id, temp=s.temp, hands_on=s.hands_on)
                for s in d.steps
            ],
        )
        if nd.bringer == "guest" and nd.bringer_guest is None:
            nd.bringer = "open"
        ev.dishes.append(nd)
        dish_map[d.id] = nd
    for t in src.prep_tasks:
        ev.prep_tasks.append(PrepTask(label=t.label, days_before=t.days_before, dish=dish_map.get(t.dish_id)))
    for c in src.checklist:
        ev.checklist.append(ChecklistItem(name=c.name, qty=c.qty, per_guest=c.per_guest, sort=c.sort))
    return ev


def rotate_guest_token(event):
    event.guest_token = new_token()


# --- schedule ------------------------------------------------------------------


def _earliest_start(event, dish):
    if dish.bringer == "host" or dish.arrive_time is None:
        return None
    arrive = datetime.combine(event.date, dish.arrive_time)
    return int((arrive - serve_datetime(event)).total_seconds() // 60)


def build_event_schedule(event):
    ks = kitchen_settings(event.household_id)
    apps = appliances(event.household_id)
    sapps = [sch.Appliance(a.id, a.name, a.kind, a.capacity) for a in apps]
    sdishes = []
    for d in event.dishes:
        steps = [sch.Step(s.id, s.label, s.minutes, s.kind, s.appliance_id, s.temp, s.hands_on) for s in d.steps if s.minutes > 0]
        if not steps:
            continue
        sdishes.append(
            sch.Dish(
                d.id,
                d.name,
                steps,
                ready_at=d.ready_at,
                max_hold=d.max_hold if d.max_hold is not None else ks.default_hold,
                earliest_start=_earliest_start(event, d),
            )
        )
    settings = sch.Settings(ks.temp_tolerance, ks.minutes_per_10f, ks.preheat_minutes, ks.cooks)
    return sch.build_schedule(sdishes, sapps, settings), {a.id: a for a in apps}


def run_of_show(event, schedule, apps_by_id):
    """Chronological list of things the cook does, for the day-of view."""
    dishes = {d.id: d for d in event.dishes}
    deg = kitchen_settings(event.household_id).deg
    items = []
    for h in schedule.preheats:
        a = apps_by_id.get(h.appliance_id)
        verb = "Set" if h.change else "Heat"
        items.append({"at": h.at, "kind": "preheat", "text": f"{verb} {a.name if a else 'oven'} to {h.temp}{deg}", "dish": None})
    for p in schedule.placements:
        d = dishes.get(p.dish_id)
        a = apps_by_id.get(p.appliance_id) if p.appliance_id else None
        where = f" — {a.name}" + (f" at {p.temp}{deg}" if p.temp else "") if a else ""
        items.append(
            {
                "at": p.start,
                "end": p.end,
                "kind": "hands" if p.hands_on else ("appliance" if a else "wait"),
                "text": f"{d.name}: {p.label[:1].lower() + p.label[1:]} ({fmt_minutes(p.end - p.start)}){where}",
                "dish": d.id,
            }
        )
        stays = any(q.dish_id == p.dish_id and q.appliance_id == p.appliance_id and q.start == p.end for q in schedule.placements)
        if a and not stays:
            items.append({"at": p.end, "kind": "out", "text": f"{d.name}: out of the {a.name.lower()}", "dish": d.id})
    for r in schedule.dishes.values():
        d = dishes.get(r.dish_id)
        if r.finish is not None:
            msg = f"{d.name} ready" + (f" (hold {r.hold} min)" if r.hold else "")
            items.append({"at": r.finish, "kind": "ready", "text": msg, "dish": d.id})
    items.append({"at": 0, "kind": "serve", "text": "Serve", "dish": None})
    order = {"preheat": 0, "out": 1, "ready": 2, "appliance": 3, "hands": 3, "wait": 4, "serve": 5}
    items.sort(key=lambda i: (i["at"], order[i["kind"]]))
    for i in items:
        i["clock"] = clock(event, i["at"])
        i["when"] = when(event, i["at"])
    return items


def fmt_minutes(m):
    m = int(m)
    h, mm = divmod(abs(m), 60)
    if h and mm:
        return f"{h} h {mm} min"
    if h:
        return f"{h} h"
    return f"{mm} min"


# --- prep ----------------------------------------------------------------------

PREP_BUCKETS = [
    (14, "Two weeks out"),
    (7, "The week before"),
    (2, "Days before"),
    (1, "Day before"),
    (0, "Day of"),
]


def bucket(days):
    for floor, name in PREP_BUCKETS:
        if days >= floor:
            return name
    return "Day of"


def change_temp_unit(household_id, unit):
    """Switch a kitchen between °F and °C, converting every stored temperature so nothing changes meaning."""
    from .mealie import convert_temp

    ks = kitchen_settings(household_id)
    if unit not in ("F", "C") or unit == ks.temp_unit:
        return
    to_c = unit == "C"

    def conv(t):
        if t is None:
            return None
        return convert_temp(t, "C") if to_c else round(t * 9 / 5 + 32)

    for step in Step.query.join(Dish).join(Event).filter(Event.household_id == household_id):
        step.temp = conv(step.temp)
    for rt in RecipeTiming.query.filter_by(household_id=household_id):
        rt.steps = [{**s, "temp": conv(s.get("temp"))} for s in rt.steps]
    ks.temp_tolerance = round(ks.temp_tolerance * 5 / 9) if to_c else round(ks.temp_tolerance * 9 / 5)
    ks.minutes_per_10f = round(ks.minutes_per_10f * 9 / 5, 2) if to_c else round(ks.minutes_per_10f * 5 / 9, 2)
    ks.temp_unit = unit


def thaw_days(pounds):
    """Refrigerator thawing: about one day per 4 pounds (USDA)."""
    return max(1, math.ceil(pounds / 4))


# --- shopping ------------------------------------------------------------------


def desired_shopping(event):
    headcount = event.headcount
    out = {}
    for d in event.dishes:
        if d.bringer == "host" and d.mealie_id:
            out[d.mealie_id] = out.get(d.mealie_id, 0) + effective_scale(d, headcount)
    return out


def sync_shopping(event, client):
    if event.shopping_list_id:
        try:
            current_list = client.get_list(event.shopping_list_id)
        except Exception:
            current_list = None
        if current_list is None:
            event.shopping_list_id = None
    if not event.shopping_list_id:
        created = client.create_list(f"{event.name} ({event.date:%b %-d})")
        event.shopping_list_id = created["id"]
        current_list = created
    adds, removes = shopping_diff(desired_shopping(event), list_recipe_quantities(current_list))
    if adds:
        client.add_recipes(event.shopping_list_id, adds)
    for rid, q in removes:
        client.remove_recipe(event.shopping_list_id, rid, q)
    return adds, removes


def cookware(event):
    """Mealie tools across host dishes, with which dishes need them."""
    out = {}
    for d in event.dishes:
        if d.bringer != "host":
            continue
        for t in d.mealie_tools or []:
            out.setdefault(t, []).append(d.name)
    return dict(sorted(out.items()))
