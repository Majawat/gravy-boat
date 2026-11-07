"""Backward kitchen scheduler.

Pure functions, no Flask or database. All times are integer minutes relative to the
serve time (negative = before serving). Every dish is a chain of steps that run back
to back; the last step ends when the dish is ready. A finished dish may sit (hold)
for up to its max_hold minutes, which is the slack the scheduler uses to fit dishes
around each other on shared appliances.
"""

from dataclasses import dataclass, field

HEAT_KINDS = {"oven", "smoker", "grill", "air_fryer"}
SEARCH_STEP = 5
OVERFLOW_HOLD = 60  # past max_hold + this, a clash is more useful than a dish held for hours


@dataclass
class Appliance:
    id: int
    name: str
    kind: str
    capacity: int = 1


@dataclass
class Step:
    id: int
    label: str
    minutes: int
    kind: str | None = None  # None = counter work / resting, needs no appliance
    appliance_id: int | None = None  # pin to one specific appliance
    temp: int | None = None
    hands_on: bool = False


@dataclass
class Dish:
    id: int
    name: str
    steps: list[Step]
    ready_at: int = 0  # minutes relative to serve (desserts may be positive)
    max_hold: int = 20
    earliest_start: int | None = None  # e.g. a guest's arrival time


@dataclass
class Settings:
    temp_tolerance: int = 25  # dishes within this many degrees can share an oven
    minutes_per_10f: float = 1.0  # time to move an oven 10 degrees between uses
    preheat_minutes: int = 15
    cooks: int = 1


@dataclass
class Placement:
    dish_id: int
    step_id: int
    label: str
    start: int
    end: int
    kind: str | None
    appliance_id: int | None
    temp: int | None
    hands_on: bool


@dataclass
class DishResult:
    dish_id: int
    start: int | None = None
    finish: int | None = None
    hold: int = 0
    placements: list[Placement] = field(default_factory=list)


@dataclass
class Conflict:
    kind: str  # no_appliance | over_hold | clash | late_arrival | hands
    message: str
    dish_ids: list[int] = field(default_factory=list)
    start: int | None = None
    end: int | None = None


@dataclass
class Preheat:
    appliance_id: int
    at: int
    temp: int
    ready_by: int
    change: bool = False  # already hot, just set to a new temperature


@dataclass
class Schedule:
    dishes: dict[int, DishResult]
    placements: list[Placement]
    conflicts: list[Conflict]
    preheats: list[Preheat]
    start: int
    end: int


def _gap_needed(t1, t2, settings):
    if t1 is None or t2 is None or abs(t1 - t2) <= settings.temp_tolerance:
        return 0
    return int(round(abs(t1 - t2) / 10 * settings.minutes_per_10f))


def _fits(appliance, start, end, temp, booked, settings, dish_id=None):
    """Can a step occupy `appliance` from start to end at `temp`, given what is booked?"""
    overlapping = 0
    for p in booked.get(appliance.id, []):
        # a dish's own consecutive steps can change temperature in place (turn the oven down with it inside)
        same_dish = p.dish_id == dish_id and (p.start == end or p.end == start)
        gap = _gap_needed(temp, p.temp, settings) if appliance.kind in HEAT_KINDS and not same_dish else 0
        if start < p.end + gap and p.start < end + gap:
            if gap:
                return False  # different temperature too close in time
            if start < p.end and p.start < end:
                overlapping += 1
    return overlapping < appliance.capacity


def _candidates(step, appliances):
    if step.appliance_id is not None:
        return [a for a in appliances if a.id == step.appliance_id]
    return [a for a in appliances if a.kind == step.kind]


def _try_place(dish, finish, appliances, booked, settings, prefer=None):
    """Place the chain so the last step ends at `finish`. Returns placements or None."""
    placements = []
    end = finish
    later_app = prefer  # appliance of the step that follows this one, preferred so food isn't moved between ovens
    for step in reversed(dish.steps):
        start = end - step.minutes
        appliance_id = None
        if step.kind or step.appliance_id is not None:
            options = sorted(_candidates(step, appliances), key=lambda a: a.id != later_app)
            chosen = next(
                (a for a in options if _fits(a, start, end, step.temp, booked, settings, dish.id)),
                None,
            )
            if chosen is None:
                _unbook(placements, booked)
                return None
            appliance_id = chosen.id
            later_app = chosen.id
            # book tentatively so later (earlier-in-time) steps of this dish see it
            booked.setdefault(chosen.id, []).append(
                Placement(dish.id, step.id, step.label, start, end, step.kind, chosen.id, step.temp, step.hands_on)
            )
        placements.append(
            Placement(dish.id, step.id, step.label, start, end, step.kind, appliance_id, step.temp, step.hands_on)
        )
        end = start
    return list(reversed(placements))


def _place_preferring_one_appliance(dish, finish, appliances, booked, settings):
    """Like _try_place, but if a dish has several steps of one kind, try each appliance for them so
    the food can stay put (a pie isn't moved between ovens halfway through)."""
    kinds = [s.kind for s in dish.steps if s.kind and s.appliance_id is None]
    shared = {k for k in kinds if kinds.count(k) > 1}
    if not shared:
        return _try_place(dish, finish, appliances, booked, settings)
    fallback = None
    for a in [a for a in appliances if a.kind in shared]:
        placed = _try_place(dish, finish, appliances, booked, settings, prefer=a.id)
        if placed is None:
            continue
        _unbook(placed, booked)  # trial only; the chosen option is booked again below
        if len({p.appliance_id for p in placed if p.kind in shared}) == 1:
            return _try_place(dish, finish, appliances, booked, settings, prefer=a.id)
        if fallback is None:
            fallback = a.id
    if fallback is None:
        return None
    return _try_place(dish, finish, appliances, booked, settings, prefer=fallback)


def _unbook(placements, booked):
    for p in placements:
        if p.appliance_id is None:
            continue
        lst = booked.get(p.appliance_id, [])
        for i, b in enumerate(lst):
            if b.dish_id == p.dish_id and b.step_id == p.step_id and b.start == p.start:
                del lst[i]
                break


def _force_place(dish, finish, appliances):
    """Last resort: lay the chain out ignoring capacity so the cook still sees a plan."""
    placements = []
    end = finish
    for step in reversed(dish.steps):
        start = end - step.minutes
        options = _candidates(step, appliances) if (step.kind or step.appliance_id is not None) else []
        appliance_id = options[0].id if options else None
        placements.append(
            Placement(dish.id, step.id, step.label, start, end, step.kind, appliance_id, step.temp, step.hands_on)
        )
        end = start
    return list(reversed(placements))


def _priority(dish):
    busy = sum(s.minutes for s in dish.steps if s.kind or s.appliance_id is not None)
    pinned = any(s.appliance_id is not None for s in dish.steps)
    return (not pinned, -busy, dish.ready_at, dish.id)


def build_schedule(dishes, appliances, settings=None):
    settings = settings or Settings()
    booked: dict[int, list[Placement]] = {}
    results: dict[int, DishResult] = {}
    conflicts: list[Conflict] = []
    by_id = {a.id: a for a in appliances}

    for dish in sorted(dishes, key=_priority):
        result = DishResult(dish.id)
        results[dish.id] = result
        if not dish.steps:
            continue

        missing = [
            s for s in dish.steps if (s.kind or s.appliance_id is not None) and not _candidates(s, appliances)
        ]
        if missing:
            s = missing[0]
            what = s.kind.replace("_", " ") if s.kind else "the chosen appliance"
            conflicts.append(Conflict("no_appliance", f"{dish.name}: your kitchen has no {what}.", [dish.id]))

        placed = None
        hold = 0
        limit = dish.max_hold + OVERFLOW_HOLD
        while hold <= limit and not missing:
            placed = _place_preferring_one_appliance(dish, dish.ready_at - hold, appliances, booked, settings)
            if placed is not None:
                if dish.earliest_start is not None and placed[0].start < dish.earliest_start:
                    _unbook(placed, booked)
                    placed = None
                    break
                break
            hold += SEARCH_STEP

        if placed is None:
            placed = _force_place(dish, dish.ready_at, appliances)
            hold = 0
            if dish.earliest_start is not None and placed[0].start < dish.earliest_start:
                conflicts.append(
                    Conflict(
                        "late_arrival",
                        f"{dish.name} needs to start {dish.earliest_start - placed[0].start} min before it arrives.",
                        [dish.id],
                        placed[0].start,
                        dish.earliest_start,
                    )
                )
            elif not missing:
                conflicts.append(
                    Conflict(
                        "clash",
                        f"{dish.name} doesn't fit around the other dishes; it overlaps on shared appliances.",
                        [dish.id],
                        placed[0].start,
                        placed[-1].end,
                    )
                )
        elif hold > dish.max_hold:
            conflicts.append(
                Conflict(
                    "over_hold",
                    f"{dish.name} finishes {hold} min early and has to be held (limit {dish.max_hold}).",
                    [dish.id],
                    placed[-1].end,
                    dish.ready_at,
                )
            )

        result.placements = placed
        result.start = placed[0].start
        result.finish = placed[-1].end
        result.hold = hold

    placements = sorted(
        (p for r in results.values() for p in r.placements), key=lambda p: (p.start, p.end, p.dish_id)
    )
    shared = _shared_appliance_clashes(placements, by_id, settings)
    explained = {d for c in shared for d in c.dish_ids}
    conflicts = [c for c in conflicts if not (c.kind == "clash" and set(c.dish_ids) <= explained)]
    conflicts.extend(shared)
    conflicts.extend(_hands_on_overload(placements, settings))
    preheats = _preheats(placements, by_id, settings)

    times = [p.start for p in placements] + [p.at for p in preheats] + [p.end for p in placements] + [0]
    return Schedule(results, placements, conflicts, preheats, min(times), max(times))


def _shared_appliance_clashes(placements, by_id, settings):
    """Report overlaps left over from forced placements, one entry per appliance and time window."""
    windows = []  # (appliance_id, start, end, dish_ids, temp_clash)
    for a in placements:
        appliance = by_id.get(a.appliance_id) if a.appliance_id is not None else None
        if appliance is None:
            continue
        overlapping = [
            b for b in placements
            if b is not a and b.appliance_id == a.appliance_id and b.start < a.end and a.start < b.end
        ]
        over_capacity = len(overlapping) + 1 > appliance.capacity
        temp_clash = appliance.kind in HEAT_KINDS and any(_gap_needed(a.temp, b.temp, settings) for b in overlapping)
        if over_capacity or temp_clash:

            windows.append((a.appliance_id, a.start, a.end, {a.dish_id} | {b.dish_id for b in overlapping}, temp_clash))

    merged = []
    for app_id, start, end, ids, temp in sorted(windows, key=lambda w: (w[0], w[1])):
        last = merged[-1] if merged else None
        if last and last[0] == app_id and start <= last[2]:
            last[2] = max(last[2], end)
            last[3] |= ids
            last[4] = last[4] or temp
        else:
            merged.append([app_id, start, end, set(ids), temp])
    return [
        Conflict(
            "clash",
            f"{by_id[app_id].name} is double-booked ({'different temperatures' if temp else 'more dishes than it holds'}).",
            sorted(ids),
            start,
            end,
        )
        for app_id, start, end, ids, temp in merged
    ]


def _hands_on_overload(placements, settings):
    hands = [p for p in placements if p.hands_on]
    edges = sorted({p.start for p in hands} | {p.end for p in hands})
    out = []
    window = None
    for t0, t1 in zip(edges, edges[1:]):
        active = [p for p in hands if p.start < t1 and t0 < p.end]
        if len(active) > settings.cooks:
            ids = sorted({p.dish_id for p in active})
            if window and window[1] == t0 and window[2] == ids:
                window[1] = t1
            else:
                if window:
                    out.append(window)
                window = [t0, t1, ids, len(active)]
        elif window:
            out.append(window)
            window = None
    if window:
        out.append(window)
    return [
        Conflict(
            "hands",
            f"{n} hands-on tasks at once but {settings.cooks} cook{'s' if settings.cooks != 1 else ''}.",
            ids,
            t0,
            t1,
        )
        for t0, t1, ids, n in out
    ]


def _preheats(placements, by_id, settings):
    """One preheat for each run of use on a heated appliance that starts from cold."""
    out = []
    by_app: dict[int, list[Placement]] = {}
    for p in placements:
        if p.appliance_id is not None and p.temp and by_id.get(p.appliance_id) and by_id[p.appliance_id].kind in HEAT_KINDS:
            by_app.setdefault(p.appliance_id, []).append(p)
    for app_id, items in by_app.items():
        items.sort(key=lambda p: p.start)
        run_end = None
        run_temp = None
        prev = None
        for p in items:
            cold = run_end is None or p.start > run_end + 30
            changed = run_temp is not None and _gap_needed(p.temp, run_temp, settings)
            if cold:
                out.append(Preheat(app_id, p.start - settings.preheat_minutes, p.temp, p.start))
            elif changed:
                # a dish continuing in place changes temperature when its next step starts
                continuing = prev is not None and prev.dish_id == p.dish_id and prev.end == p.start
                gap = 0 if continuing else _gap_needed(p.temp, run_temp, settings)
                out.append(Preheat(app_id, p.start - gap, p.temp, p.start, change=True))
            prev = p
            run_end = p.end if run_end is None or cold else max(run_end, p.end)
            run_temp = p.temp
    return sorted(out, key=lambda h: h.at)
