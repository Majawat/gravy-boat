"""Thin Mealie API client. Every call runs as the signed-in person, with their own token."""

import re

import requests


class MealieError(Exception):
    pass


class MealieAuthError(MealieError):
    """Bad credentials, or the stored token has expired."""


class Mealie:
    def __init__(self, base_url, token=None, timeout=15):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method, path, **kw):
        headers = kw.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            r = requests.request(method, self.base + path, headers=headers, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise MealieError(f"Can't reach Mealie: {e.__class__.__name__}") from e
        if r.status_code in (401, 403):
            raise MealieAuthError("Mealie rejected the login")
        if r.status_code >= 400:
            raise MealieError(f"Mealie returned {r.status_code} for {path}")
        return r.json() if r.content else None

    # --- auth -------------------------------------------------------------------
    def login(self, username, password):
        data = self._req(
            "POST",
            "/api/auth/token",
            data={"username": username, "password": password, "remember_me": "true"},
        )
        self.token = data["access_token"]
        return self.token

    def me(self):
        return self._req("GET", "/api/users/self")

    def version(self):
        """(major, minor, patch) of the Mealie server, or None if it doesn't say."""
        v = (self._req("GET", "/api/app/about") or {}).get("version", "")
        m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", v or "")
        return tuple(int(x) for x in m.groups()) if m else None

    # --- recipes ----------------------------------------------------------------
    def search(self, query, per_page=24):
        params = {"perPage": per_page, "orderBy": "name", "orderDirection": "asc"}
        if query:
            params["search"] = query
        return self._req("GET", "/api/recipes", params=params)["items"]

    def recipe(self, slug_or_id):
        return self._req("GET", f"/api/recipes/{slug_or_id}")

    def tags(self):
        return self._req("GET", "/api/organizers/tags", params={"perPage": -1})["items"]

    # --- shopping lists ---------------------------------------------------------
    def create_list(self, name):
        return self._req("POST", "/api/households/shopping/lists", json={"name": name})

    def get_list(self, list_id):
        return self._req("GET", f"/api/households/shopping/lists/{list_id}")

    def add_recipes(self, list_id, increments):
        """increments: [(recipe_id, quantity)]"""
        body = [{"recipeId": rid, "recipeIncrementQuantity": q} for rid, q in increments]
        return self._req("POST", f"/api/households/shopping/lists/{list_id}/recipe", json=body)

    def remove_recipe(self, list_id, recipe_id, quantity):
        return self._req(
            "POST",
            f"/api/households/shopping/lists/{list_id}/recipe/{recipe_id}/delete",
            json={"recipeDecrementQuantity": quantity},
        )

    def delete_list(self, list_id):
        return self._req("DELETE", f"/api/households/shopping/lists/{list_id}")


def list_recipe_quantities(shopping_list):
    """{recipe_id: quantity} currently on a Mealie shopping list."""
    out = {}
    for ref in shopping_list.get("recipeReferences") or []:
        rid = ref.get("recipeId")
        if rid:
            out[rid] = out.get(rid, 0) + float(ref.get("recipeQuantity") or 0)
    return out


def shopping_diff(desired, current):
    """Changes that turn `current` {recipe_id: qty} into `desired`. Returns (adds, removes)."""
    adds, removes = [], []
    for rid in sorted(set(desired) | set(current)):
        delta = round(desired.get(rid, 0) - current.get(rid, 0), 3)
        if delta > 0:
            adds.append((rid, delta))
        elif delta < 0:
            removes.append((rid, -delta))
    return adds, removes


# --- kitchen steps from a recipe ------------------------------------------------

_TEMP = re.compile(r"(\d{2,3})\s*°?\s*(?:degrees\s*)?([FC])\b", re.I)
MIN_STEP_TIMES_VERSION = (3, 30, 0)  # first Mealie with per-step durations


def _to_f(value, unit):
    return value if unit.upper() == "F" else round(value * 9 / 5 + 32)


def convert_temp(value_f, unit):
    """A Fahrenheit value in the kitchen's unit, rounded to 5 degrees for Celsius."""
    if value_f is None or unit == "F":
        return value_f
    return int(5 * round((value_f - 32) * 5 / 9 / 5))
_KIND_WORDS = [
    ("smoker", re.compile(r"\bsmok(?:er|e|ing)\b|pellet|traeger", re.I)),
    ("grill", re.compile(r"\bgrill\b", re.I)),
    ("air_fryer", re.compile(r"air[- ]?fry", re.I)),
    ("slow_cooker", re.compile(r"slow cooker|crock ?pot", re.I)),
    ("pressure_cooker", re.compile(r"instant pot|pressure cook", re.I)),
    ("oven", re.compile(r"(?<!dutch )\boven\b|\bbake\b|\broast\b|\bbroil", re.I)),
    ("burner", re.compile(r"\bsimmer|\bboil|saucepan|skillet|stovetop|stove|medium heat|low heat|high heat", re.I)),
]
_COOK_VERB = re.compile(
    r"\b(bake|roast|smoke(?!r)|cook(?!er)|simmer|broil|braise|boil|fry|grill|sear|melt|toast|saut)\w*\b"
    r"|\bbrown(?:ed|ing)?\s+(?:the|it|them)\b",
    re.I,
)
# Pans and dishes named after a cooking method ("roasting pan", "baking dish") aren't cooking.
_EQUIPMENT = re.compile(r"\b(roasting|baking|frying|sheet|pie|cake|loaf|muffin|casserole)\s+(pan|dish|sheet|tray|plate|tin)s?\b", re.I)
_MOVE = re.compile(r"\b(transfer|add|place|put|pour|spoon|scrape)\w*\b.{0,60}\b(into|in|to|on)\b", re.I)
_REST = re.compile(r"\b(rest|cool|stand)\w*\b", re.I)


_LONG_WAIT = re.compile(r"\b(overnight|refrigerat\w*|chill\w*|marinat\w*|freez\w*|brin\w*)\b", re.I)
_RISE = re.compile(r"\b(rise|proof\w*|double[sd]? in size)\b", re.I)
_CHILL = re.compile(r"\b(chill\w*|refrigerat\w*)\b", re.I)
_VERB_LABEL = [
    ("Smoke", re.compile(r"\bsmok", re.I)),
    ("Roast", re.compile(r"\broast", re.I)),
    ("Bake", re.compile(r"\bbak", re.I)),
    ("Broil", re.compile(r"\bbroil", re.I)),
    ("Simmer", re.compile(r"\bsimmer", re.I)),
    ("Boil", re.compile(r"\bboil", re.I)),
    ("Fry", re.compile(r"\bfry|\bsaut|\bsear", re.I)),
    ("Cook", re.compile(r"\bmelt|\btoast|\bbrown(?:ed|ing)?\s+(?:the|it|them)\b", re.I)),
    ("Braise", re.compile(r"\bbrais", re.I)),
    ("Grill", re.compile(r"\bgrill", re.I)),
]
HEATED = ("oven", "smoker", "grill", "air_fryer")


def _verb_kind(text):
    """Appliance implied by the cooking verb when the step doesn't name one."""
    if re.search(r"\b(bake|roast|broil)", text, re.I):
        return "oven"
    if re.search(r"\bsmok", text, re.I):
        return "smoker"
    if re.search(r"\bgrill", text, re.I):
        return "grill"
    return "burner"
LONG_WAIT_MINUTES = 240
_SETS_HEAT = re.compile(
    r"\b(pre-?heat\w*|heat (?:the |your )?\w+ to|(?:set|reduce|lower|increase|raise|turn down|turn up)\b[^.]{0,40}\bto)", re.I
)
_INTERNAL = re.compile(r"internal|reach\w*|register\w*|thermometer|until it", re.I)


def _cook_temp(text):
    """First appliance temperature in text, in °F, skipping internal/doneness temperatures."""
    for m in _TEMP.finditer(text):
        before = text[max(0, m.start() - 40) : m.start()]
        f = _to_f(int(m.group(1)), m.group(2))
        if f >= 200 and not _INTERNAL.search(before):
            return f
    return None


def _kind_of(text):
    for k, pat in _KIND_WORDS:
        if pat.search(text):
            return k
    return None


def source_key(text):
    """Start of a step's text, whitespace-folded: identifies the step across Mealie saves."""
    return " ".join((text or "").split())[:60].lower()


def _step_label(n, text, kind):
    verb = next((name for name, pat in _VERB_LABEL if pat.search(text)), None)
    if verb is None and kind:
        verb = "Cook"
    if verb is None:
        if _RISE.search(text):
            verb = "Rise"
        elif _CHILL.search(text):
            verb = "Chill"
        elif _REST.search(text) and not _COOK_VERB.search(text):
            verb = "Rest"
        else:
            verb = "Cook" if kind else "Prep"
    return f"Step {n} · {verb}"


def steps_from_recipe(recipe, overrides=None, unit="F"):
    """Kitchen steps from a Mealie recipe's explicit step durations.

    Only instructions with a duration in Mealie become steps, and their minutes always come from
    Mealie. Mealie has no field yet for which appliance a step uses or how hot, so those are
    suggested from the step's text unless the host has already set them (`overrides`, keyed by step
    number). Mealie regenerates instruction ids on every save, so ids can't be the key; an override
    only applies while the step's text still starts the same, so inserting or reordering steps in
    Mealie falls back to suggestions instead of moving a setting onto the wrong step. Long counter waits (chilling, rising, freezing for 4+ hours or overnight)
    become make-ahead tasks instead of timeline steps.

    Returns (steps, make_ahead, untimed): untimed counts instructions with no duration.
    """
    overrides = overrides or {}
    steps, ahead, untimed = [], [], 0
    heat = {}
    last_kind = None
    for n, item in enumerate(recipe.get("recipeInstructions") or [], start=1):
        text = item.get("text") or ""
        item_kind = _kind_of(text) or last_kind
        last_kind = item_kind
        for sentence in re.split(r"(?<=[.!?\n])\s+", text):
            t = _cook_temp(sentence) if _SETS_HEAT.search(sentence) else None
            if t:
                heat[_kind_of(sentence) if _kind_of(sentence) in HEATED else (item_kind if item_kind in HEATED else "oven")] = t
        seconds = item.get("durationSeconds")
        if not seconds:
            untimed += 1
            continue
        minutes = max(1, -(-int(seconds) // 60))
        # Judge the step by what it does: preheat sentences only set the heat for later steps,
        # and pans named after a method ("roasting pan") don't mean the step cooks.
        text = " ".join(
            x for x in re.split(r"(?<=[.!?\n])\s+", text) if not (_SETS_HEAT.search(x) and not _COOK_VERB.search(x))
        )
        text = _EQUIPMENT.sub(" ", text)
        own_kind = _kind_of(text)
        if _COOK_VERB.search(text):
            kind = own_kind or item_kind or _verb_kind(text)
        elif own_kind and not _MOVE.search(text):
            kind = own_kind  # "Slow cooker: 6-8 hours on low" names the appliance and a time
        else:
            kind = None  # moving food into an appliance isn't cooking in it
        if kind is None and _cook_temp(text):
            kind = "oven"
        long_wait = _LONG_WAIT.search(text) or _RISE.search(text)
        if kind is None and long_wait and (minutes >= LONG_WAIT_MINUTES or "overnight" in text.lower()):
            ahead.append(f"Step {n}: {text.strip()[:160]}")
            continue
        temp = convert_temp((_cook_temp(text) or heat.get(kind)) if kind in HEATED else None, unit)
        step = {
            "source_id": str(n),
            "source_text": source_key(text),
            "label": _step_label(n, text, kind),
            "minutes": minutes,
            "kind": kind,
            "temp": temp,
            "hands_on": kind is None and not (_REST.search(text) or long_wait) or (kind == "burner" and minutes <= 15),
        }
        o = overrides.get(str(n))
        if o and o.get("source_text") == step["source_text"]:
            step.update({k: o[k] for k in ("label", "kind", "appliance_id", "temp", "hands_on") if k in o})
        steps.append(step)
    return steps, ahead, untimed


def recipe_summary(recipe):
    return {
        "id": recipe["id"],
        "slug": recipe["slug"],
        "name": recipe["name"],
        "servings": recipe.get("recipeServings") or None,
        "tags": [t["name"] for t in recipe.get("tags") or []],
        "tools": [t["name"] for t in recipe.get("tools") or []],
    }
