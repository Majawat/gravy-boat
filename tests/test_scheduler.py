from app.scheduler import Appliance, Dish, Settings, Step, build_schedule

OVEN = Appliance(1, "Oven", "oven", capacity=2)
OVEN_SMALL = Appliance(1, "Oven", "oven", capacity=1)
BURNERS = [Appliance(10 + i, f"Burner {i + 1}", "burner") for i in range(2)]


def bake(id, minutes, temp, kind="oven", **kw):
    return Step(id, "Bake", minutes, kind=kind, temp=temp, **kw)


def test_single_dish_ends_at_serve_and_chains_backward():
    d = Dish(1, "Ham", [Step(1, "Prep", 15, hands_on=True), bake(2, 225, 325), Step(3, "Rest", 20)])
    s = build_schedule([d], [OVEN])
    r = s.dishes[1]
    assert r.finish == 0 and r.hold == 0
    assert [(p.label, p.start, p.end) for p in r.placements] == [
        ("Prep", -260, -245),
        ("Bake", -245, -20),
        ("Rest", -20, 0),
    ]
    assert r.placements[1].appliance_id == 1
    assert not s.conflicts


def test_same_temperature_dishes_share_oven_up_to_capacity():
    a = Dish(1, "Corn casserole", [bake(1, 60, 350)])
    b = Dish(2, "Rolls", [bake(2, 20, 350)])
    s = build_schedule([a, b], [OVEN])
    assert s.dishes[1].finish == 0 and s.dishes[2].finish == 0
    assert not s.conflicts


def test_capacity_one_pushes_second_dish_earlier_within_hold():
    a = Dish(1, "Corn", [bake(1, 60, 350)], max_hold=30)
    b = Dish(2, "Rolls", [bake(2, 20, 350)], max_hold=30)
    s = build_schedule([a, b], [OVEN_SMALL])
    finishes = sorted([s.dishes[1].finish, s.dishes[2].finish])
    assert finishes[1] == 0
    # the shorter one goes second in priority, so it moves: rolls finish 60 min early is too far,
    # corn (longest) is placed first at serve, rolls must fit before corn starts
    assert s.dishes[1].finish == 0
    assert s.dishes[2].finish <= -60
    assert any(c.kind == "over_hold" and c.dish_ids == [2] for c in s.conflicts)


def test_different_temperatures_cannot_overlap_and_need_change_time():
    hot = Dish(1, "Potatoes", [bake(1, 60, 425)], max_hold=120)
    cool = Dish(2, "Ham", [bake(2, 120, 325)], max_hold=120)
    s = build_schedule([hot, cool], [OVEN], Settings(temp_tolerance=25, minutes_per_10f=1.0))
    ham, pot = s.dishes[2], s.dishes[1]
    assert ham.finish == 0  # longest dish placed first
    # potatoes must finish before ham starts, plus 10 min to change 100F
    assert pot.finish <= ham.start - 10
    assert not [c for c in s.conflicts if c.kind == "clash"]


def test_within_tolerance_temperatures_share():
    a = Dish(1, "A", [bake(1, 60, 350)])
    b = Dish(2, "B", [bake(2, 60, 375)])
    s = build_schedule([a, b], [OVEN], Settings(temp_tolerance=25))
    assert s.dishes[1].finish == 0 and s.dishes[2].finish == 0


def test_two_ovens_run_different_temperatures_side_by_side():
    ovens = [Appliance(1, "Upper oven", "oven"), Appliance(2, "Lower oven", "oven")]
    a = Dish(1, "A", [bake(1, 60, 425)])
    b = Dish(2, "B", [bake(2, 60, 325)])
    s = build_schedule([a, b], ovens)
    assert s.dishes[1].finish == 0 and s.dishes[2].finish == 0
    assert {p.appliance_id for p in s.placements} == {1, 2}


def test_impossible_fit_is_reported_and_still_laid_out():
    a = Dish(1, "A", [bake(1, 600, 350)], max_hold=0)
    b = Dish(2, "B", [bake(2, 600, 450)], max_hold=0)
    s = build_schedule([a, b], [OVEN_SMALL])
    # B fits only by finishing >= 600+10 early, beyond max_hold + overflow window -> forced clash
    assert s.dishes[2].placements
    assert any(c.kind == "clash" for c in s.conflicts)


def test_missing_appliance_kind_is_a_conflict():
    d = Dish(1, "Brisket", [Step(1, "Smoke", 600, kind="smoker", temp=250)])
    s = build_schedule([d], [OVEN])
    assert [c.kind for c in s.conflicts] == ["no_appliance"]
    assert s.dishes[1].placements[0].appliance_id is None


def test_pinned_appliance_is_respected():
    ovens = [Appliance(1, "Upper", "oven"), Appliance(2, "Lower", "oven")]
    d = Dish(1, "Turkey", [Step(1, "Roast", 180, appliance_id=2, kind="oven", temp=325)])
    s = build_schedule([d], ovens)
    assert s.placements[0].appliance_id == 2


def test_guest_arrival_too_late():
    d = Dish(1, "Guest's green beans", [bake(1, 30, 350)], earliest_start=-15)
    s = build_schedule([d], [OVEN])
    assert any(c.kind == "late_arrival" for c in s.conflicts)


def test_guest_arrival_in_time_no_conflict():
    d = Dish(1, "Reheat", [bake(1, 20, 350)], earliest_start=-60)
    s = build_schedule([d], [OVEN])
    assert not s.conflicts


def test_dessert_ready_after_serve():
    d = Dish(1, "Pie", [bake(1, 45, 375)], ready_at=60)
    s = build_schedule([d], [OVEN])
    assert s.dishes[1].finish == 60 and s.end == 60


def test_one_cook_does_hands_on_dishes_one_after_another():
    a = Dish(1, "Gravy", [Step(1, "Whisk", 20, kind="burner", hands_on=True)])
    b = Dish(2, "Mash", [Step(2, "Mash", 20, kind="burner", hands_on=True)])
    s = build_schedule([a, b], BURNERS, Settings(cooks=1))
    assert not [c for c in s.conflicts if c.kind == "hands"]
    assert sorted([s.dishes[1].finish, s.dishes[2].finish]) == [-20, 0]  # one waits its 20 minutes
    s2 = build_schedule([a, b], BURNERS, Settings(cooks=2))
    assert s2.dishes[1].finish == s2.dishes[2].finish == 0


def test_hands_on_overload_is_flagged_when_nothing_can_move():
    from app.scheduler import Placement, _hands_on_overload

    ps = [Placement(1, 1, "Whisk", -20, 0, "burner", 10, None, True), Placement(2, 2, "Mash", -20, 0, "burner", 11, None, True)]
    hands = _hands_on_overload(ps, Settings(cooks=1))
    assert len(hands) == 1 and hands[0].start == -20 and hands[0].end == 0


def test_preheat_before_first_use_and_on_temperature_change():
    hot = Dish(1, "Potatoes", [bake(1, 60, 425)], max_hold=200)
    cool = Dish(2, "Ham", [bake(2, 120, 325)], max_hold=200)
    s = build_schedule([hot, cool], [OVEN_SMALL], Settings(preheat_minutes=15))
    temps = [(h.temp, h.at) for h in s.preheats]
    assert temps[0][0] == 425 and temps[0][1] == s.dishes[1].start - 15
    assert any(t == 325 for t, _ in temps)


def test_failed_attempt_does_not_leave_bookings():
    # dish with two oven steps where only the later one fits at first try must not leak bookings
    blocker = Dish(1, "Blocker", [bake(1, 60, 450)], max_hold=0)
    two = Dish(2, "Two step", [bake(2, 30, 300), bake(3, 30, 300)], max_hold=120)
    s = build_schedule([blocker, two], [OVEN_SMALL])
    on_oven = sorted((p.start, p.end) for p in s.placements if p.appliance_id == 1)
    for (s1, e1), (s2, e2) in zip(on_oven, on_oven[1:]):
        assert e1 <= s2


def test_overbooked_oven_reports_one_merged_window():
    dishes = [Dish(i, f"D{i}", [bake(i, 60, t)], max_hold=0) for i, t in enumerate([325, 350, 410, 450], start=1)]
    s = build_schedule(dishes, [OVEN_SMALL])
    clashes = [c for c in s.conflicts if c.kind == "clash"]
    assert len(clashes) == 1
    assert clashes[0].dish_ids == [1, 3, 4]  # D2 fits by finishing an hour early


def test_consecutive_steps_stay_on_the_same_appliance():
    ovens = [Appliance(1, "Upper", "oven"), Appliance(2, "Lower", "oven")]
    pie = Dish(1, "Pie", [bake(1, 15, 425), bake(2, 45, 350)])
    s = build_schedule([pie], ovens)
    assert len({p.appliance_id for p in s.placements}) == 1


def test_dish_stays_in_one_oven_even_when_the_first_choice_is_busy():
    ovens = [Appliance(1, "Upper", "oven", capacity=2), Appliance(2, "Lower", "oven", capacity=2)]
    ham = Dish(1, "Ham", [bake(1, 180, 325)])  # holds the upper oven at 325 all afternoon
    pie = Dish(2, "Pie", [bake(2, 15, 425), bake(3, 45, 350)], max_hold=0)
    s = build_schedule([ham, pie], ovens)
    assert len({p.appliance_id for p in s.dishes[2].placements}) == 1


def test_turning_the_oven_down_mid_dish_happens_when_the_next_step_starts():
    pie = Dish(1, "Pie", [bake(1, 15, 425), bake(2, 45, 350)])
    s = build_schedule([pie], [OVEN_SMALL])
    change = [h for h in s.preheats if h.change]
    assert len(change) == 1 and change[0].at == -45 and change[0].temp == 350


def test_component_is_ready_before_the_step_that_uses_it():
    turkey = Dish(1, "Turkey", [Step(11, "Butter it", 10, hands_on=True), Step(12, "Smoke", 120, kind="smoker", temp=325)])
    butter = Dish(2, "Herb butter", [Step(21, "Whip", 10, hands_on=True)])
    smoker = Appliance(5, "Smoker", "smoker")
    s = build_schedule([butter, turkey], [smoker], deps={2: [(1, 11)]})
    butter_step = s.dishes[1].placements[0]
    assert s.dishes[2].finish <= butter_step.start  # butter done before the turkey gets buttered
    assert s.dishes[2].finish == -130  # and no earlier than it has to be
    assert not [c for c in s.conflicts if c.kind == "over_hold"]


def test_cooks_are_a_resource_so_a_dish_that_can_wait_moves_aside():
    turkey = Dish(1, "Turkey", [Step(1, "Smoke", 120, kind="smoker"), Step(3, "Carve", 20, hands_on=True)], max_hold=0)
    gravy = Dish(2, "Gravy", [Step(2, "Whisk", 15, kind="burner", hands_on=True)], max_hold=30)
    s = build_schedule([turkey, gravy], BURNERS + [Appliance(5, "Smoker", "smoker")], Settings(cooks=1))
    assert s.dishes[1].finish == 0
    assert s.dishes[2].finish <= -20  # made before the carving instead of at the same time
    assert not [c for c in s.conflicts if c.kind == "hands"]


def test_missing_appliance_warns_but_the_rest_still_books_properly():
    stock = Dish(1, "Stock", [Step(1, "Pressure cook", 90, kind="pressure_cooker"), Step(2, "Reduce", 60, kind="burner")])
    gravy = Dish(2, "Gravy", [Step(3, "Simmer", 60, kind="burner")])
    one_burner = [Appliance(10, "Burner", "burner")]
    s = build_schedule([stock, gravy], one_burner)
    kinds = [c.kind for c in s.conflicts]
    assert "no_appliance" in kinds and "clash" not in kinds  # warned, but nothing double-booked
    reduce = s.dishes[1].placements[1]
    simmer = s.dishes[2].placements[0]
    assert reduce.end <= simmer.start or simmer.end <= reduce.start


def test_back_to_back_steps_of_one_dish_take_one_rack():
    sprouts = Dish(1, "Sprouts", [bake(1, 15, 410), bake(2, 10, 410)])
    potatoes = Dish(2, "Potatoes", [bake(3, 25, 410)])
    s = build_schedule([sprouts, potatoes], [OVEN])  # holds 2
    assert s.dishes[1].finish == 0 and s.dishes[2].finish == 0
    assert not s.conflicts


def test_one_cooks_back_to_back_steps_count_once():
    gravy = Dish(1, "Gravy", [Step(1, "Roux", 5, kind="burner", hands_on=True), Step(2, "Whisk", 5, kind="burner", hands_on=True)])
    sprouts = Dish(2, "Sprouts", [Step(3, "Trim", 10, hands_on=True)])
    s = build_schedule([gravy, sprouts], BURNERS, Settings(cooks=2))
    assert s.dishes[1].finish == 0 and s.dishes[2].finish == 0  # two cooks, two tasks at a time
