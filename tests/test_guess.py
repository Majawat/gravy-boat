from app.mealie import steps_from_recipe


def run(*items, overrides=None):
    recipe = {"recipeInstructions": [{"id": f"s{i}", "text": t, "durationSeconds": d} for i, (t, d) in enumerate(items, 1)]}
    steps, ahead, untimed = steps_from_recipe(recipe, overrides)
    return [(s["label"], s["minutes"], s["kind"], s["temp"]) for s in steps], ahead, untimed


def test_minutes_come_only_from_mealie_durations():
    steps, _, untimed = run(("Preheat oven to 350°F.", None), ("Simmer for 30 minutes.", 1500), ("Bake for 50 minutes.", 3000))
    # the text says 30 but Mealie says 25: Mealie wins; the preheat step has no time and is skipped
    assert steps == [("Step 2 · Simmer", 25, "burner", None), ("Step 3 · Bake", 50, "oven", 350)]
    assert untimed == 1


def test_partial_minutes_round_up():
    steps, _, _ = run(("Whisk the eggs.", 61),)
    assert steps[0][1] == 2


def test_appliance_and_temperature_are_suggested_from_the_step_text():
    steps, _, _ = run(
        ("Preheat your smoker to 275F.", None),
        ("Smoke until an internal thermometer reads 157 F.", 16200),
        ("Heat oil in a dutch oven over medium heat and cook the onions.", 420),
        ("Reduce the oven to 325°F and bake.", 6300),
    )
    assert steps == [
        ("Step 2 · Smoke", 270, "smoker", 275),
        ("Step 3 · Cook", 7, "burner", None),
        ("Step 4 · Bake", 105, "oven", 325),
    ]


def test_appliance_carries_over_between_instructions():
    steps, _, _ = run(("Put everything in the crockpot.", None), ("Cook on low.", 28800))
    assert steps == [("Step 2 · Cook", 480, "slow_cooker", None)]


def test_host_settings_override_suggestions():
    o = {"1": {"source_text": "bake.", "label": "Bake rolls", "kind": "oven", "temp": 400, "hands_on": False}}
    steps, _, _ = run(("Bake.", 600), overrides=o)
    assert steps == [("Bake rolls", 10, "oven", 400)]


def test_settings_survive_regenerated_ids_but_not_reordered_steps():
    o = {"2": {"source_text": "simmer the sauce.", "label": "Simmer sauce", "kind": "burner", "temp": None, "hands_on": False}}
    # same text at the same step number (Mealie gave it a new id): applied
    steps, _, _ = run(("Chop.", 300), ("Simmer the sauce.", 1200), overrides=o)
    assert steps[1][0] == "Simmer sauce"
    # a step was inserted before it, so step 2 is now different text: ignored, suggestion used
    steps, _, _ = run(("Chop.", 300), ("Brown the meat.", 600), ("Simmer the sauce.", 1200), overrides=o)
    assert [s[0] for s in steps] == ["Step 1 · Prep", "Step 2 · Cook", "Step 3 · Simmer"]


def test_long_counter_waits_become_make_ahead():
    steps, ahead, _ = run(("Mix the dough.", 600), ("Cover and refrigerate overnight.", 43200), ("Bake at 400°F.", 720))
    assert ahead == ["Step 2: Cover and refrigerate overnight."]
    assert [s[0] for s in steps] == ["Step 1 · Prep", "Step 3 · Bake"]


def test_moving_food_into_an_appliance_is_not_cooking():
    steps, _, _ = run(("Transfer the mixture into a slow cooker and stir until combined.", 300), ("Cook on low.", 28800))
    assert steps[0] == ("Step 1 · Prep", 5, None, None)
    assert steps[1] == ("Step 2 · Cook", 480, "slow_cooker", None)


def test_step_that_names_an_appliance_uses_it():
    steps, _, _ = run(("Slow cooker: 6-8 hours on low or 3-4 hours on high.", 28800), ("Grill the burgers.", 480))
    assert steps == [("Step 1 · Cook", 480, "slow_cooker", None), ("Step 2 · Grill", 8, "grill", None)]


def test_celsius_recipes_and_celsius_kitchens():
    recipe = {"recipeInstructions": [{"id": "a", "text": "Bake at 180°C.", "durationSeconds": 1200}]}
    steps, _, _ = steps_from_recipe(recipe, unit="C")
    assert steps[0]["temp"] == 180
    steps, _, _ = steps_from_recipe(recipe, unit="F")
    assert steps[0]["temp"] == 356


def test_pans_named_after_a_method_are_not_cooking():
    steps, _, _ = run(("Set the ham in a roasting pan and cover with foil.", 600), ("Spread in a buttered baking dish.", 480))
    assert [(s[2]) for s in steps] == [None, None]


def test_preheat_sentence_does_not_put_a_rise_in_the_oven():
    steps, _, _ = run(("Let rise again for 30 minutes. Preheat the oven to 375°F.", 1800), ("Bake for 18 minutes.", 1080))
    assert steps == [("Step 1 · Rise", 30, None, None), ("Step 2 · Bake", 18, "oven", 375)]


def test_browning_butter_is_stovetop_cooking():
    steps, _, _ = run(("Brown the butter in a small saucepan. Let it cool slightly.", 480),)
    assert steps == [("Step 1 · Cook", 8, "burner", None)]
