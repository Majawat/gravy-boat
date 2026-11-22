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


def test_long_counter_waits_stay_in_the_chain_as_waiting():
    recipe = {"recipeInstructions": [
        {"text": "Mix the dough.", "durationSeconds": 600},
        {"text": "Cover and refrigerate overnight.", "durationSeconds": 43200},
        {"text": "Bake at 400°F.", "durationSeconds": 720},
    ]}
    steps, ahead, _ = steps_from_recipe(recipe)
    assert ahead == []
    assert [(s["label"], s["minutes"], s["kind"], s["hands_on"]) for s in steps] == [
        ("Step 1 · Prep", 10, None, True),
        ("Step 2 · Chill", 720, None, False),
        ("Step 3 · Bake", 12, "oven", False),
    ]


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


def test_stand_mixer_and_the_rest_are_not_resting():
    steps, _, _ = run(
        ("In the stand mixer bowl, combine the milk, eggs and sugar.", 180),
        ("Whisk in a little stock first until smooth, then add the rest.", 180),
    )
    assert [s[0] for s in steps] == ["Step 1 · Prep", "Step 2 · Prep"]


def test_softening_butter_is_waiting_not_hands_on():
    recipe = {"recipeInstructions": [{"text": "Set the butter out until fully softened. Finely dice the herbs.", "durationSeconds": 3600}]}
    steps, _, _ = steps_from_recipe(recipe)
    assert steps[0]["hands_on"] is False and steps[0]["kind"] is None


def test_firing_up_a_pellet_grill_is_a_preheat_not_cooking():
    steps, _, _ = run(
        ("Fire up the pellet grill to 300–325 °F. Use about 250 °F instead if you want a smokier bird.", 900),
        ("Put the turkey on the grill and let it smoke.", 6600),
    )
    assert steps[0][2:] == (None, None)  # no oven at 250 from the "instead" sentence
    assert steps[1] == ("Step 2 · Smoke", 110, "smoker", 325)


def test_preheating_fat_in_the_oven_is_not_a_cooking_step():
    steps, _, _ = run(
        ("Preheat the oven to 410 °F with the goose fat on a baking sheet inside so it melts. Meanwhile, peel the potatoes.", 900),
        ("Bake 25 minutes.", 1500),
    )
    assert steps[0] == ("Step 1 · Prep", 15, None, None)
    assert steps[1] == ("Step 2 · Bake", 25, "oven", 410)


def test_doneness_temperatures_do_not_book_an_oven():
    steps, _, _ = run(("Pull it when the thickest part of the breast reads about 155 °F and the leg meat is close to 200 °F. Rest at least 30 minutes.", 1800),)
    assert steps == [("Step 1 · Rest", 30, None, None)]


def test_high_pressure_is_a_pressure_cooker_and_loading_it_is_not():
    steps, _, _ = run(
        ("Move the bones into the pressure cooker. Add the carrots and celery.", 600),
        ("Top off with boiling water. Cook on high pressure for 1 ½ hours.", 6300),
    )
    assert steps[0][2] is None
    assert steps[1][2] == "pressure_cooker"


def test_a_kettle_is_not_a_burner():
    steps, _, _ = run(("Put a kettle of water on to boil for later.", 300),)
    assert steps[0][2] is None


def test_steps_list_the_sub_recipes_their_ingredients_link_to():
    recipe = {
        "recipeIngredient": [
            {"referenceId": "i1", "food": {"name": "butter"}},
            {"referenceId": "i2", "referencedRecipe": {"id": "r-stock", "name": "Stock"}, "quantity": 1, "unit": {"name": "quart"}},
        ],
        "recipeInstructions": [
            {"text": "Make a roux.", "durationSeconds": 180, "ingredientReferences": [{"referenceId": "i1"}]},
            {"text": "Whisk in the stock.", "durationSeconds": 180, "ingredientReferences": [{"referenceId": "i2"}]},
        ],
    }
    steps, _, _ = steps_from_recipe(recipe)
    assert [s["uses"] for s in steps] == [[], ["r-stock"]]


def test_recipe_summary_reads_components_yield_and_turkey_weight():
    from app.mealie import recipe_summary

    r = {
        "id": "r", "slug": "s", "name": "Turkey", "recipeYieldQuantity": 6, "recipeYield": "pounds carved meat",
        "recipeIngredient": [
            {"food": {"name": "whole turkey"}, "quantity": 1, "note": "12 to 13 pounds"},
            {"food": {"name": "turkey stock"}, "quantity": 2, "unit": {"name": "cup"}},
            {"referencedRecipe": {"id": "r-butter"}, "quantity": 1},
        ],
    }
    s = recipe_summary(r)
    assert s["thaw_lbs"] == 13
    assert s["components"] == [{"recipe_id": "r-butter", "qty": 1, "unit": None}]
    assert (s["yield_qty"], s["yield_unit"]) == (6, "pounds carved meat")


def test_waiting_words_only_count_without_hands_on_work_or_when_long():
    recipe = {"recipeInstructions": [
        {"text": "Thaw the turkey completely. Untuck the legs and pull out the neck.", "durationSeconds": 900},
        {"text": "Add the softened butter and salt. Knead with the dough hook 6 to 10 minutes.", "durationSeconds": 600},
        {"text": "Soften the butter. Grease a mixing bowl and a baking dish.", "durationSeconds": 1800},
        {"text": "Warm the turkey stock.", "durationSeconds": 300},
    ]}
    steps, _, _ = steps_from_recipe(recipe)
    assert [(s["label"], s["kind"], s["hands_on"]) for s in steps] == [
        ("Step 1 · Prep", None, True),
        ("Step 2 · Prep", None, True),
        ("Step 3 · Rest", None, False),
        ("Step 4 · Cook", "burner", True),
    ]


def test_preheat_only_steps_are_labelled_and_not_hands_on():
    recipe = {"recipeInstructions": [
        {"text": "Fire up the pellet grill to 300–325 °F. Use about 250 °F instead if you want a smokier bird.", "durationSeconds": 900},
        {"text": "Roast on the pellet grill until the bones look positively smoky.", "durationSeconds": 1800},
    ]}
    steps, _, _ = steps_from_recipe(recipe)
    assert [(s["label"], s["kind"], s["hands_on"]) for s in steps] == [("Step 1 · Preheat", None, False), ("Step 2 · Roast", "smoker", False)]


def test_set_aside_tip_quoted_sentence_and_rolls_noun():
    recipe = {"recipeInstructions": [
        {"text": "Pro tip: if you want butter for the rolls, set it aside now, before your hands touch raw turkey.", "durationSeconds": 60},
        {"text": 'Cover and let rise until doubled and "nice and poofy." Preheat the oven to 375 °F during the rise.', "durationSeconds": 3600},
        {"text": "Get the rolls out of the pan so the bottoms don't steam. Cool on a rack.", "durationSeconds": 900},
        {"text": "Bake 15 to 20 minutes.", "durationSeconds": 1200},
    ]}
    steps, _, _ = steps_from_recipe(recipe)
    assert [(s["label"], s["hands_on"]) for s in steps] == [
        ("Step 1 · Prep", True), ("Step 2 · Rise", False), ("Step 3 · Rest", False), ("Step 4 · Bake", False),
    ]
    assert steps[3]["temp"] == 375


def test_settings_key_is_unchanged_for_saved_settings():
    # Keys saved before this version dropped heat-setting sentences and split only on bare . ! ?
    recipe = {"recipeInstructions": [
        {"text": "Preheat the oven to 350°F. Bake the casserole.", "durationSeconds": 1800},
        {"text": 'Let rise until "poofy." Set it aside to touch later.', "durationSeconds": 600},
    ]}
    steps, _, _ = steps_from_recipe(recipe)
    assert [s["source_text"] for s in steps] == ["bake the casserole.", ""]  # exactly what older versions stored


def test_mealie_step_summary_becomes_the_label():
    recipe = {"recipeInstructions": [{"text": "Spread the bones on the pellet grill. Roast about 1 hour.", "summary": "Roast the bones", "durationSeconds": 3600}]}
    steps, _, _ = steps_from_recipe(recipe)
    assert (steps[0]["label"], steps[0]["kind"]) == ("Step 1 · Roast the bones", "smoker")
