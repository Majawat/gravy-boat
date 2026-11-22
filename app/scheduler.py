"""Backward kitchen scheduler.

Pure functions, no Flask or database. All times are integer minutes relative to the
serve time (negative = before serving). Every dish is a chain of steps that run back
to back; the last step ends when the dish is ready. A finished dish may sit (hold)
for up to its max_hold minutes, which is the slack the scheduler uses to fit dishes
around each other on shared appliances.

People are a resource too. Each hands-on step is given to a cook who is home and not
busy (the dish's own cook if it has one), and steps on a grill, oven or stovetop need
someone home. Waits marked `stretch` (chilling, brining) may run longer than written,
so the steps before them can move earlier, e.g. to an evening when someone's home.
"""

from dataclasses import dataclass, field, replace

HEAT_KINDS = {"oven", "smoker", "grill", "air_fryer"}
ATTENDED_KINDS = HEAT_KINDS | {"burner"}  # someone has to be home; fridges, slow and pressure cookers don't care
OVERFLOW_HOLD = 60  # past max_hold + this, a clash is more useful than a dish held for hours
STRETCH_LIMIT = 3 * 24 * 60  # a stretchable wait grows by at most this much
PLACE_BUDGET = 12000  # step placements tried per dish before laying it out anyway


def _hold_offsets(limit):
    """How much earlier than wanted to try finishing: fine steps first, coarser further out."""
    t = 0
    while t <= limit:
        yield t
        t += 5 if t < 120 else 15


def _stretch_offsets():
    t = 0
    while t <= STRETCH_LIMIT:
        yield t
        t += 5 if t < 60 else 15 if t < 360 else 30


@dataclass
class Appliance:
    id: int
    name: str
    kind: str
    capacity: int = 1


@dataclass
class Cook:
    id: int
    name: str = ""
    away: list = field(default_factory=list)  # [(start, end)]: not home
    quiet: list = field(default_factory=list)  # [(start, end)]: home, but no hands-on work (asleep)


@dataclass
class Step:
    id: int
    label: str
    minutes: int
    kind: str | None = None  # None = counter work / resting, needs no appliance
    appliance_id: int | None = None  # pin to one specific appliance
    temp: int | None = None
    hands_on: bool = False
    stretch: bool = False  # a wait that may run longer than `minutes`


@dataclass
class Dish:
    id: int
    name: str
    steps: list[Step]
    ready_at: int = 0  # minutes relative to serve (desserts may be positive)
    max_hold: int = 20
    earliest_start: int | None = None  # e.g. a guest's arrival time
    cook_id: int | None = None  # this cook does every hands-on step; None = whoever's free


@dataclass
class Settings:
    temp_tolerance: int = 25  # dishes within this many degrees can share an oven
    minutes_per_10f: float = 1.0  # time to move an oven 10 degrees between uses
    preheat_minutes: int = 15
    cooks: int = 1  # used when no cooks are named


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
    cook_id: int | None = None


@dataclass
class DishResult:
    dish_id: int
    start: int | None = None
    finish: int | None = None
    hold: int = 0
    placements: list[Placement] = field(default_factory=list)


@dataclass
class Conflict:
    kind: str  # no_appliance | over_hold | clash | late_arrival | hands | nobody_home | thaw
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
    nobody_home: list = field(default_factory=list)  # [(start, end)]


def _gap_needed(t1, t2, settings):
    if t1 is None or t2 is None or abs(t1 - t2) <= settings.temp_tolerance:
        return 0
    return int(round(abs(t1 - t2) / 10 * settings.minutes_per_10f))


def _most_at_once(others, start, end):
    """The most of `others` in use at the same moment between start and end. A dish's back-to-back steps
    (roast 15 min, then roast 10 more) are one dish in the oven, not two."""
    points = [start] + [p.start for p in others if start < p.start < end]
    return max((sum(1 for p in others if p.start <= t < p.end) for t in points), default=0)


def _overlaps(intervals, start, end):
    return any(s < end and start < e for s, e in intervals)


def _nobody_home(cooks):
    """Times when every cook is away (being asleep at home doesn't count)."""
    if not cooks:
        return []
    common = sorted(cooks[0].away)
    for c in cooks[1:]:
        common = [(max(a, b), min(e, f)) for a, e in common for b, f in c.away if max(a, b) < min(e, f)]
    return sorted(common)


def _fits(appliance, start, end, temp, booked, settings, dish_id=None):
    """Can a step occupy `appliance` from start to end at `temp`, given what is booked?"""
    overlapping = []
    for p in booked.get(appliance.id, []):
        # a dish's own consecutive steps can change temperature in place (turn the oven down with it inside)
        same_dish = p.dish_id == dish_id and (p.start == end or p.end == start)
        gap = _gap_needed(temp, p.temp, settings) if appliance.kind in HEAT_KINDS and not same_dish else 0
        if start < p.end + gap and p.start < end + gap:
            if gap:
                return False  # different temperature too close in time
            if start < p.end and p.start < end:
                overlapping.append(p)
    return _most_at_once(overlapping, start, end) < appliance.capacity


def _candidates(step, appliances):
    if step.appliance_id is not None:
        return [a for a in appliances if a.id == step.appliance_id]
    return [a for a in appliances if a.kind == step.kind]


def _cook_free(cook, start, end, booked):
    if _overlaps(cook.away, start, end) or _overlaps(cook.quiet, start, end):
        return False
    return not any(p.start < end and start < p.end for p in booked.get(("cook", cook.id), []))


class _Ctx:
    """One attempt at placing one dish."""

    def __init__(self, dish, appliances, booked, settings, cooks, nobody_home, prefer, budget):
        self.dish, self.appliances, self.booked, self.settings = dish, appliances, booked, settings
        self.cooks, self.nobody_home = cooks, nobody_home
        self.later_app = prefer  # the next step's appliance, preferred so food isn't moved between ovens
        self.later_cook = None  # and the next hands-on step's cook, so one person sees a dish through
        self.budget = budget


def _place_step(ctx, step, start, end):
    """Book one step from start to end, or None. Books its appliance and its cook."""
    ctx.budget[0] -= 1
    if ctx.budget[0] < 0:
        return None
    dish = ctx.dish
    if step.kind in ATTENDED_KINDS and _overlaps(ctx.nobody_home, start, end):
        return None
    appliance_id = None
    if step.kind or step.appliance_id is not None:
        options = sorted(_candidates(step, ctx.appliances), key=lambda a: a.id != ctx.later_app)
        chosen = next((a for a in options if _fits(a, start, end, step.temp, ctx.booked, ctx.settings, dish.id)), None)
        if chosen is None:
            return None
        appliance_id = chosen.id
    cook_id = None
    if step.hands_on and ctx.cooks:
        pool = [c for c in ctx.cooks if c.id == dish.cook_id] if dish.cook_id is not None else ctx.cooks
        pool = sorted(pool, key=lambda c: c.id != ctx.later_cook)
        cook = next((c for c in pool if _cook_free(c, start, end, ctx.booked)), None)
        if cook is None:
            return None
        cook_id = cook.id
    p = Placement(dish.id, step.id, step.label, start, end, step.kind, appliance_id, step.temp, step.hands_on, cook_id)
    if appliance_id is not None:
        ctx.booked.setdefault(appliance_id, []).append(p)
        ctx.later_app = appliance_id
    if cook_id is not None:
        ctx.booked.setdefault(("cook", cook_id), []).append(p)
        ctx.later_cook = cook_id
    return p


def _place_from(ctx, i, end):
    """Place steps[0..i] so step i ends at `end`. Returns placements in order (booked), or None (nothing booked)."""
    steps = ctx.dish.steps
    step = steps[i]
    start = end - step.minutes
    saved = (ctx.later_app, ctx.later_cook)
    p = _place_step(ctx, step, start, end)
    if p is None:
        return None
    if i == 0:
        return [p]
    stretchy = step.stretch and not step.hands_on and p.appliance_id is None
    for extra in _stretch_offsets() if stretchy else (0,):
        rest = _place_from(ctx, i - 1, start - extra)
        if rest is not None:
            return rest + [replace(p, start=start - extra) if extra else p]
        if ctx.budget[0] < 0:
            break
    _unbook([p], ctx.booked)
    ctx.later_app, ctx.later_cook = saved
    return None


def _try_place(dish, finish, appliances, booked, settings, cooks=(), nobody_home=(), prefer=None, budget=None):
    """Place the chain so the last step ends at `finish`. Returns placements or None."""
    if not dish.steps:
        return []
    ctx = _Ctx(dish, appliances, booked, settings, list(cooks), list(nobody_home), prefer, budget or [PLACE_BUDGET])
    return _place_from(ctx, len(dish.steps) - 1, finish)


def _place_preferring_one_appliance(dish, finish, appliances, booked, settings, cooks=(), nobody_home=(), budget=None):
    """Like _try_place, but if a dish has several steps of one kind, try each appliance for them so
    the food can stay put (a pie isn't moved between ovens halfway through)."""
    kinds = [s.kind for s in dish.steps if s.kind and s.appliance_id is None]
    shared = {k for k in kinds if kinds.count(k) > 1}
    args = (appliances, booked, settings, cooks, nobody_home)
    if not shared:
        return _try_place(dish, finish, *args, budget=budget)
    fallback = None
    for a in [a for a in appliances if a.kind in shared]:
        placed = _try_place(dish, finish, *args, prefer=a.id, budget=budget)
        if placed is None:
            continue
        _unbook(placed, booked)  # trial only; the chosen option is booked again below
        if len({p.appliance_id for p in placed if p.kind in shared}) == 1:
            return _try_place(dish, finish, *args, prefer=a.id, budget=budget)
        if fallback is None:
            fallback = a.id
    if fallback is None:
        return None
    return _try_place(dish, finish, *args, prefer=fallback, budget=budget)


def _unbook(placements, booked):
    for p in placements:
        keys = ([p.appliance_id] if p.appliance_id is not None else []) + ([("cook", p.cook_id)] if p.cook_id is not None else [])
        for key in keys:
            lst = booked.get(key, [])
            for i, b in enumerate(lst):
                if b.dish_id == p.dish_id and b.step_id == p.step_id and b.end == p.end:
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
            Placement(dish.id, step.id, step.label, start, end, step.kind, appliance_id, step.temp, step.hands_on, dish.cook_id if step.hands_on else None)
        )
        end = start
    return list(reversed(placements))


def _priority(dish):
    busy = sum(s.minutes for s in dish.steps if s.kind or s.appliance_id is not None)
    pinned = any(s.appliance_id is not None for s in dish.steps)
    return (not pinned, dish.cook_id is None, -busy, dish.ready_at, dish.id)


def _ordered(dishes, deps):
    """Priority order, except a component (a stock, a compound butter) waits until every dish that
    needs it is placed, so it can be timed to be ready before the step that uses it."""
    pending = sorted(dishes, key=_priority)
    ids = {d.id for d in dishes}
    done: set[int] = set()
    out = []
    while pending:
        pick = next(
            (d for d in pending if all(pid in done or pid not in ids for pid, _ in deps.get(d.id, []))),
            pending[0],  # a cycle: just go in priority order
        )
        pending.remove(pick)
        done.add(pick.id)
        out.append(pick)
    return out


def build_schedule(dishes, appliances, settings=None, deps=None, cooks=None):
    """deps: {component dish id: [(dish id, step id that uses it)]}. cooks: [Cook]; when None,
    `settings.cooks` anonymous cooks who are always home."""
    settings = settings or Settings()
    deps = deps or {}
    if cooks is None:
        cooks = [Cook(-(i + 1)) for i in range(max(1, settings.cooks))]
    nobody = _nobody_home(cooks)
    booked: dict = {}
    results: dict[int, DishResult] = {}
    conflicts: list[Conflict] = []
    by_id = {a.id: a for a in appliances}
    known_cooks = {c.id for c in cooks}

    for dish in _ordered(dishes, deps):
        needed = [
            p.start
            for pid, sid in deps.get(dish.id, [])
            for p in (results[pid].placements if pid in results else [])
            if p.step_id == sid
        ]
        if needed and min(needed) < dish.ready_at:
            dish = replace(dish, ready_at=min(needed))
        if dish.cook_id is not None and dish.cook_id not in known_cooks:
            dish = replace(dish, cook_id=None)
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
            # Warn, but still plan the dish properly: the step without an appliance just takes its time.
            gone = {m.id for m in missing}
            dish = replace(dish, steps=[replace(s, kind=None, appliance_id=None) if s.id in gone else s for s in dish.steps])

        placed = None
        hold = 0
        budget = [PLACE_BUDGET]
        for hold in _hold_offsets(dish.max_hold + OVERFLOW_HOLD):
            placed = _place_preferring_one_appliance(dish, dish.ready_at - hold, appliances, booked, settings, cooks, nobody, budget)
            if placed is not None:
                if dish.earliest_start is not None and placed[0].start < dish.earliest_start:
                    _unbook(placed, booked)
                    placed = None
                break
            if budget[0] < 0:
                break

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
            else:
                conflicts.append(
                    Conflict(
                        "clash",
                        f"{dish.name} doesn't fit around the other dishes and the cooks' time; it overlaps.",
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
    conflicts.extend(_hands_on_overload(placements, cooks))
    conflicts.extend(_unattended(placements, nobody, {d.id: d.name for d in dishes}, by_id))
    preheats = _preheats(placements, by_id, settings)

    times = [p.start for p in placements] + [p.at for p in preheats] + [p.end for p in placements] + [0]
    return Schedule(results, placements, conflicts, preheats, min(times), max(times), nobody)


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
        over_capacity = _most_at_once(overlapping, a.start, a.end) + 1 > appliance.capacity
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


def _hands_on_overload(placements, cooks):
    """Hands-on work nobody was free for (only left after a dish had to be laid out regardless)."""
    hands = [p for p in placements if p.hands_on]
    edges = sorted({p.start for p in hands} | {p.end for p in hands})
    out = []
    window = None
    for t0, t1 in zip(edges, edges[1:]):
        active = [p for p in hands if p.start < t1 and t0 < p.end]
        free = sum(1 for c in cooks if not _overlaps(c.away, t0, t1) and not _overlaps(c.quiet, t0, t1))
        if len(active) > free:
            ids = sorted({p.dish_id for p in active})
            if window and window[1] == t0 and window[2] == ids and window[4] == free:
                window[1] = t1
            else:
                if window:
                    out.append(window)
                window = [t0, t1, ids, len(active), free]
        elif window:
            out.append(window)
            window = None
    if window:
        out.append(window)
    return [
        Conflict(
            "hands",
            f"{n} hands-on task{'s' if n != 1 else ''} at once but {free} cook{'s' if free != 1 else ''} free.",
            ids,
            t0,
            t1,
        )
        for t0, t1, ids, n, free in out
    ]


def _unattended(placements, nobody, names, by_id):
    """A grill, oven or stovetop step left running while nobody's home."""
    out = []
    for p in placements:
        if p.kind in ATTENDED_KINDS and _overlaps(nobody, p.start, p.end):
            where = by_id[p.appliance_id].name.lower() if p.appliance_id in by_id else (p.kind or "").replace("_", " ")
            out.append(Conflict("nobody_home", f"Nobody's home while {names.get(p.dish_id, 'a dish')} is on the {where}.", [p.dish_id], p.start, p.end))
    return out


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
