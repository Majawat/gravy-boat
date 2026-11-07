"""Runs against a real Mealie when GRAVYBOAT_LIVE_TOKEN and MEALIE_URL are set.

Needs at least two recipes in that Mealie. Creates one throwaway shopping list and deletes it.
"""

import os

import pytest

from app.mealie import Mealie, list_recipe_quantities, recipe_summary, shopping_diff, steps_from_recipe

TOKEN = os.environ.get("GRAVYBOAT_LIVE_TOKEN")
URL = os.environ.get("MEALIE_URL")
pytestmark = pytest.mark.skipif(not (TOKEN and URL), reason="set GRAVYBOAT_LIVE_TOKEN and MEALIE_URL")


@pytest.fixture
def m():
    return Mealie(URL, TOKEN)


@pytest.fixture
def two_recipes(m):
    hits = m.search("")
    assert len(hits) >= 2, "the live Mealie needs at least two recipes"
    return [m.recipe(h["slug"]) for h in hits[:2]]


def test_me_version_and_recipe_reading(m, two_recipes):
    assert m.me()["householdId"]
    assert m.version() is not None
    for r in two_recipes:
        info = recipe_summary(r)
        assert info["id"] and info["slug"]
        steps, ahead, untimed = steps_from_recipe(r)
        assert len(steps) + len(ahead) + untimed == len(r["recipeInstructions"])  # every step accounted for
    assert isinstance(m.tags(), list)


def test_shopping_list_round_trip(m, two_recipes):
    a, b = (r["id"] for r in two_recipes)
    lst = m.create_list("Gravy Boat live test (safe to delete)")
    try:
        m.add_recipes(lst["id"], [(a, 1.5), (b, 1)])
        cur = list_recipe_quantities(m.get_list(lst["id"]))
        assert cur == {a: 1.5, b: 1.0}
        adds, removes = shopping_diff({a: 1.0}, cur)
        assert adds == [] and sorted(removes) == sorted([(a, 0.5), (b, 1.0)])
        for rid, q in removes:
            m.remove_recipe(lst["id"], rid, q)
        assert list_recipe_quantities(m.get_list(lst["id"])) == {a: 1.0}
    finally:
        m.delete_list(lst["id"])
