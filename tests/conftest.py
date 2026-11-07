import re

import flask_migrate
import pytest

from app import create_app
from app.mealie import MealieAuthError
from app.models import db

HOUSEHOLD = "hh-1"

RECIPES = {
    "southern-corn-casserole": {
        "id": "r-corn",
        "slug": "southern-corn-casserole",
        "name": "Southern Corn Casserole",
        "recipeServings": 8,
        "tags": [{"name": "Tree Nut-Safe"}, {"name": "Gluten-Unsafe"}],
        "tools": [{"name": "9x13-inch baking dish"}, {"name": "mixing bowl (large)"}],
        "recipeCategory": [{"name": "Side Dish"}],
        "recipeInstructions": [
            {"id": "c1", "text": "Preheat oven to 350°F."},
            {"id": "c2", "text": "Bake for 45 minutes until set.", "durationSeconds": 2700},
        ],
    },
    "dinner-rolls": {
        "id": "r-rolls",
        "slug": "dinner-rolls",
        "name": "Dinner Rolls",
        "recipeServings": 24,
        "tags": [{"name": "Tree Nut-Safe"}, {"name": "Gluten-Modifiable"}],
        "tools": [{"name": "baking sheet"}, {"name": "mixing bowl (large)"}],
        "recipeCategory": [],
        "recipeInstructions": [{"id": "r1", "text": "Bake at 375°F for 15 minutes.", "durationSeconds": 900}],
    },
    "pecan-pie": {
        "id": "r-pie",
        "slug": "pecan-pie",
        "name": "Pecan Pie",
        "recipeServings": 8,
        "tags": [{"name": "Gluten-Unsafe"}],
        "tools": [],
        "recipeCategory": [{"name": "Dessert"}],
        "recipeInstructions": [
            {"id": "p1", "text": "Bake at 350°F for 60 minutes.", "durationSeconds": 3600},
            {"id": "p2", "text": "Let cool 2 hours.", "durationSeconds": 7200},
        ],
    },
}


class FakeMealie:
    """Stands in for the Mealie API. Shared state lives on the class so every client sees it."""

    users = {"taylor": ("pw", {"username": "taylor", "fullName": "Taylor", "householdId": HOUSEHOLD, "household": "Test Household", "groupSlug": "home"})}
    lists = {}
    calls = []

    def __init__(self, base_url, token=None, timeout=15):
        self.token = token

    @classmethod
    def reset(cls):
        cls.lists = {}
        cls.calls = []
        cls.version_tuple = (3, 30, 0)

    def _check(self):
        if self.token != "tok-taylor":
            raise MealieAuthError("bad token")

    def login(self, username, password):
        u = self.users.get(username)
        if not u or u[0] != password:
            raise MealieAuthError("nope")
        self.token = f"tok-{username}"
        return self.token

    def me(self):
        self._check()
        return self.users["taylor"][1]

    version_tuple = (3, 30, 0)

    def version(self):
        self._check()
        return self.version_tuple

    def search(self, query, per_page=24):
        self._check()
        q = (query or "").lower()
        return [r for r in RECIPES.values() if q in r["name"].lower()]

    def recipe(self, slug_or_id):
        self._check()
        for r in RECIPES.values():
            if slug_or_id in (r["slug"], r["id"]):
                return r
        raise KeyError(slug_or_id)

    def tags(self):
        self._check()
        return [{"name": n} for n in ["Tree Nut-Safe", "Gluten-Safe", "Gluten-Modifiable", "Gluten-Unsafe", "holiday"]]

    def create_list(self, name):
        self._check()
        lid = f"list-{len(self.lists) + 1}"
        self.lists[lid] = {"id": lid, "name": name, "recipeReferences": []}
        self.calls.append(("create", name))
        return self.lists[lid]

    def get_list(self, list_id):
        self._check()
        return self.lists[list_id]

    def add_recipes(self, list_id, increments):
        self._check()
        refs = self.lists[list_id]["recipeReferences"]
        for rid, q in increments:
            self.calls.append(("add", rid, q))
            ref = next((r for r in refs if r["recipeId"] == rid), None)
            if ref:
                ref["recipeQuantity"] += q
            else:
                refs.append({"recipeId": rid, "recipeQuantity": q})

    def remove_recipe(self, list_id, recipe_id, quantity):
        self._check()
        self.calls.append(("remove", recipe_id, quantity))
        refs = self.lists[list_id]["recipeReferences"]
        for r in list(refs):
            if r["recipeId"] == recipe_id:
                r["recipeQuantity"] -= quantity
                if r["recipeQuantity"] <= 0:
                    refs.remove(r)


@pytest.fixture
def app(monkeypatch, tmp_path):
    import app.auth as auth

    FakeMealie.reset()
    auth._failures.clear()
    monkeypatch.setattr(auth, "Mealie", FakeMealie)
    a = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/t.db",
            "WTF_CSRF_ENABLED": False,
            "SESSION_COOKIE_SECURE": False,
            "MEALIE_PUBLIC_URL": "https://recipes.example",
        }
    )
    with a.app_context():
        flask_migrate.upgrade()  # build the schema the way production does
    yield a
    with a.app_context():
        db.session.remove()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def host(client):
    r = client.post("/login", data={"username": "taylor", "password": "pw"})
    assert r.status_code == 302
    return client


def event_id_from(resp):
    m = re.search(r"/events/(\d+)", resp.headers["Location"])
    return int(m.group(1))
