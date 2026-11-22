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

    def household_members(self):
        """People in the signed-in user's Mealie household."""
        data = self._req("GET", "/api/households/members", params={"perPage": -1}) or {}
        return data.get("items", data) if isinstance(data, dict) else data

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
    ("pressure_cooker", re.compile(r"instant pot|pressure[- ]cook|(?:high|low) pressure|multi-?cooker", re.I)),
    ("oven", re.compile(r"(?<!dutch )\boven\b|\bbake\b|\broast\b|\bbroil", re.I)),
    ("burner", re.compile(r"\bsimmer|\bboil|saucepan|skillet|stovetop|stove|medium heat|low heat|high heat", re.I)),
]
_COOK_VERB = re.compile(
    r"\b(bake|roast|smoke(?!r)|cook(?!er)|simmer|broil|braise|boil|fry|grill|sear|melt|toast|saut)\w*\b"
    r"|\b(?:brown|warm)(?:ed|ing)?\s+(?:the|it|them|up)\b",
    re.I,
)
# Hands-on work. A step with these isn't "just waiting" unless it's long (softening butter for an hour
# while you dice herbs is mostly waiting; kneading in the softened butter is not).
_ACTIVE = re.compile(
    r"\b(untuck|pull|pat|cut|chop|dice|mince|peel|trim|slice|mix|whisk|stir|combine|knead|shape|roll(?:ed|ing)?\b|rub|season|"
    r"brush|spread|toss|arrange|portion|crush|flip|carve|fold|grease|zest|juice|grate|measure|pour|add|wrap|punch|"
    r"tuck|smear|scoop|spoon|drain|blend|beat|form|press|nick|snip|whip|paint|squirt|strain|skim)\w*\b",
    re.I,
)
PASSIVE_MIN_MINUTES = 30
# Pans and dishes named after a cooking method ("roasting pan", "baking dish") aren't cooking.
_EQUIPMENT = re.compile(r"\b(roasting|baking|frying|sheet|pie|cake|loaf|muffin|casserole)\s+(pan|dish|sheet|tray|plate|tin)s?\b", re.I)
# Sentences that only heat or ready something ("Preheat the oven to 410°F with the fat inside so it melts")
# don't cook the food; melting/toasting/browning in them is about the pan, not the dish.
# "the grill" / "pellet grill" is a noun, not grilling.
_COOK_FOOD = re.compile(
    r"\b(bake|roast|smoke(?!r)|cook(?!er)|simmer|broil|braise|boil|fry|(?<!pellet )(?<!the )(?<!a )(?<!your )grill|sear|saut)\w*\b",
    re.I,
)
_MOVE = re.compile(r"\b(transfer|add|place|put|pour|spoon|scrape|move)\w*\b.{0,60}\b(into|in|to|on)\b", re.I)
# Waiting, not working. "the rest" / "rest of" and "stand mixer" are not resting.
_REST = re.compile(
    r"\b(?:(?<!the )rest(?!\s+of\b)\w*|cool\w*|stand(?!\s+mixer)\w*|soften\w*|thaw\w*|room temperature|sit|sits)\b",
    re.I,
)
# An electric kettle boiling water isn't a burner booking (unless the step also names a pan or the stove).
_KETTLE = re.compile(r"\bkettle\b", re.I)
_STOVE_WORDS = re.compile(r"saucepan|skillet|stovetop|stove|\bpot\b|\bpan\b|over (?:low|medium|high) heat", re.I)


_LONG_WAIT = re.compile(r"\b(overnight|refrigerat\w*|chill\w*|marinat\w*|freez\w*|brin\w*)\b", re.I)
_RISE = re.compile(r"\b(rise|proof\w*|double[sd]? in size)\b", re.I)
_CHILL = re.compile(r"\b(chill\w*|refrigerat\w*)\b", re.I)
_VERB_LABEL = [
    ("Smoke", re.compile(r"\bsmok(?:e|ed|es|ing)\b", re.I)),
    ("Roast", re.compile(r"\broast", re.I)),
    ("Bake", re.compile(r"\bbak", re.I)),
    ("Broil", re.compile(r"\bbroil", re.I)),
    ("Simmer", re.compile(r"\bsimmer", re.I)),
    ("Boil", re.compile(r"\bboil", re.I)),
    ("Fry", re.compile(r"\bfry|\bsaut|\bsear", re.I)),
    ("Cook", re.compile(r"\bmelt|\btoast|\b(?:brown|warm)(?:ed|ing)?\s+(?:the|it|them|up)\b", re.I)),
    ("Braise", re.compile(r"\bbrais", re.I)),
    ("Brine", re.compile(r"\bbrin(?:e|ing)\b|\bdry[- ]brine", re.I)),
    ("Marinate", re.compile(r"\bmarinat", re.I)),
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
_SETS_HEAT = re.compile(
    r"\b(pre-?heat\w*|heat (?:the |your )?\w+ to\b|fire up\b[^.]{0,40}\bto\b|"
    r"(?:set|reduce|lower|increase|raise|turn down|turn up)\b[^.]{0,40}\bto\b)",
    re.I,
)
# The settings key (source_key) was always built with this older pattern and splitter; keep them so saved
# host settings keep matching their steps.
_SETS_HEAT_KEY = re.compile(
    r"\b(pre-?heat\w*|heat (?:the |your )?\w+ to|(?:set|reduce|lower|increase|raise|turn down|turn up)\b[^.]{0,40}\bto)", re.I
)
_KEY_SPLIT = re.compile(r"(?<=[.!?\n])\s+")
# Sentence ends, including ones closed by a quote or bracket ('...nice and poofy." Preheat the oven').
_SENTENCES = re.compile(r"(?<=[.!?\n])\s+|(?<=[.!?][\"”’)])\s+")
# Doneness temperatures of the food, not the appliance ("the breast reads 155°F, the leg close to 200°F").
_INTERNAL = re.compile(
    r"internal|reach\w*|register\w*|thermometer|until it|\breads?\b|thickest|breast|thigh|\blegs?\b|\bmeat\b|close to|probe",
    re.I,
)


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


def _step_label(n, text, kind, resting=None, preheat=False):
    if preheat:
        return f"Step {n} · Preheat"
    verb = next((name for name, pat in _VERB_LABEL if pat.search(text)), None)
    if verb is None and kind:
        verb = "Cook"
    if verb is None:
        if _RISE.search(text):
            verb = "Rise"
        elif _CHILL.search(text):
            verb = "Chill"
        elif re.search(r"\bfreez", text, re.I):
            verb = "Freeze"
        elif (resting if resting is not None else _REST.search(text) and not _COOK_VERB.search(text)):
            verb = "Rest"
        else:
            verb = "Cook" if kind else "Prep"
    return f"Step {n} · {verb}"


def _referenced_ids(recipe):
    """Ingredient referenceId -> id of the Mealie recipe it links to (a sub-recipe like a stock)."""
    out = {}
    for ing in recipe.get("recipeIngredient") or []:
        ref = ing.get("referencedRecipe") or {}
        if ref.get("id") and ing.get("referenceId"):
            out[ing["referenceId"]] = ref["id"]
    return out


def steps_from_recipe(recipe, overrides=None, unit="F"):
    """Kitchen steps from a Mealie recipe's explicit step durations.

    Only instructions with a duration in Mealie become steps, and their minutes always come from
    Mealie. Mealie has no field yet for which appliance a step uses or how hot, so those are
    suggested from the step's text unless the host has already set them (`overrides`, keyed by step
    number). Mealie regenerates instruction ids on every save, so ids can't be the key; an override
    only applies while the step's text still starts the same, so inserting or reordering steps in
    Mealie falls back to suggestions instead of moving a setting onto the wrong step.

    Long counter waits (chilling overnight, dry-brining for days) stay in the chain as waiting steps,
    so the work before them lands on the right earlier day. Each step also lists the sub-recipes its
    linked ingredients point to (`uses`), so a stock can be scheduled before the gravy step needing it.

    Returns (steps, make_ahead, untimed). make_ahead is always empty now; it's kept for callers.
    """
    overrides = overrides or {}
    refs = _referenced_ids(recipe)
    steps, untimed = [], 0
    heat = {}
    last_kind = None
    for n, item in enumerate(recipe.get("recipeInstructions") or [], start=1):
        text = item.get("text") or ""
        item_kind = _kind_of(text) or last_kind
        last_kind = item_kind
        for sentence in _SENTENCES.split(text):
            t = _cook_temp(sentence) if _SETS_HEAT.search(sentence) else None
            if t:
                heat[_kind_of(sentence) if _kind_of(sentence) in HEATED else (item_kind if item_kind in HEATED else "oven")] = t
        seconds = item.get("durationSeconds")
        if not seconds:
            untimed += 1
            continue
        minutes = max(1, -(-int(seconds) // 60))
        # The settings key is built exactly as before so hosts' saved settings keep matching.
        key_text = " ".join(x for x in _KEY_SPLIT.split(text) if not (_SETS_HEAT_KEY.search(x) and not _COOK_VERB.search(x)))
        key_text = _EQUIPMENT.sub(" ", key_text)
        # Judge the step by what it does: sentences that only heat or ready something (preheat, fire up
        # the grill) set the heat for later steps, and pans named after a method ("roasting pan") don't cook.
        sentences = _SENTENCES.split(text)
        kept = [x for x in sentences if not (_SETS_HEAT.search(x) and not _COOK_FOOD.search(x))]
        sets_heat = len(kept) < len(sentences)
        work = _EQUIPMENT.sub(" ", " ".join(kept))
        own_kind = _kind_of(work)
        if _COOK_VERB.search(work):
            kind = own_kind or item_kind or _verb_kind(work)
        elif own_kind and not _MOVE.search(work):
            kind = own_kind  # "Slow cooker: 6-8 hours on low" names the appliance and a time
        else:
            kind = None  # moving food into an appliance isn't cooking in it
        if kind == "burner" and _KETTLE.search(work) and not _STOVE_WORDS.search(work):
            kind = None
        if kind is None and not sets_heat and _cook_temp(work):
            kind = "oven"
        long_wait = bool(_LONG_WAIT.search(work) or _RISE.search(work))
        active = bool(_ACTIVE.search(work))
        resting = bool(_REST.search(work)) and not _COOK_FOOD.search(work) and (not active or minutes >= PASSIVE_MIN_MINUTES)
        # "Fire up the grill to 325°F": the grill's time, not yours. A rise or rest in the same step wins.
        preheat = sets_heat and kind is None and not active and not long_wait and not _REST.search(work)
        temp = convert_temp((_cook_temp(work) or heat.get(kind)) if kind in HEATED else None, unit)
        step = {
            "source_id": str(n),
            "source_text": source_key(key_text),
            # Mealie's own step summary ("Roast the bones") says more than a guessed verb.
            "label": f"Step {n} · {item['summary'].strip()}"[:80] if (item.get("summary") or "").strip() else _step_label(n, work, kind, resting, preheat),
            "minutes": minutes,
            "kind": kind,
            "temp": temp,
            "hands_on": (kind is None and not (resting or long_wait or preheat)) or (kind == "burner" and minutes <= 15),
            "uses": sorted({refs[r["referenceId"]] for r in item.get("ingredientReferences") or [] if r.get("referenceId") in refs}),
            # Chilling, brining, marinating, freezing can run longer than written (the time is a minimum), so
            # earlier steps may move earlier. Rises and rests can't: over-proofed dough and cold meat suffer.
            "stretch": kind is None and bool(_LONG_WAIT.search(work)) and not _RISE.search(work) and (not active or minutes >= PASSIVE_MIN_MINUTES),
        }
        o = overrides.get(str(n))
        if o and o.get("source_text") == step["source_text"]:
            step.update({k: o[k] for k in ("label", "kind", "appliance_id", "temp", "hands_on", "stretch") if k in o})
        steps.append(step)
    return steps, [], untimed


_POUNDS = re.compile(r"(\d+(?:\.\d+)?)\s*(?:(?:to|-|–)\s*(\d+(?:\.\d+)?)\s*)?(?:pounds?|lbs?)\b", re.I)
_NOT_A_BIRD = re.compile(r"stock|broth|giblet|neck|wing|tail|breast|thigh|drumstick|fat|ground|bacon|sausage|deli|gravy|leg", re.I)


def _turkey_pounds(recipe):
    """Weight of a whole turkey in the ingredients, for a thawing reminder. None if there isn't one."""
    for ing in recipe.get("recipeIngredient") or []:
        food = ((ing.get("food") or {}).get("name") or "").lower()
        if ing.get("referencedRecipe") or "turkey" not in food or _NOT_A_BIRD.search(food):
            continue
        unit = ((ing.get("unit") or {}).get("name") or "").lower()
        if unit in ("pound", "pounds", "lb", "lbs") and ing.get("quantity"):
            return float(ing["quantity"])
        for text in (ing.get("note"), ing.get("originalText"), ing.get("display")):
            m = _POUNDS.search(text or "")
            if m:
                return max(float(x) for x in m.groups() if x)
    return None


def _components(recipe):
    """Sub-recipes this recipe uses, with how much of each per batch."""
    out = []
    for ing in recipe.get("recipeIngredient") or []:
        ref = ing.get("referencedRecipe") or {}
        if ref.get("id"):
            out.append({"recipe_id": ref["id"], "qty": ing.get("quantity") or 1, "unit": (ing.get("unit") or {}).get("name")})
    return out


def recipe_summary(recipe):
    return {
        "id": recipe["id"],
        "slug": recipe["slug"],
        "name": recipe["name"],
        "servings": recipe.get("recipeServings") or None,
        "tags": [t["name"] for t in recipe.get("tags") or []],
        "tools": [t["name"] for t in recipe.get("tools") or []],
        "yield_qty": recipe.get("recipeYieldQuantity") or None,
        "yield_unit": (recipe.get("recipeYield") or "").strip()[:60] or None,
        "components": _components(recipe),
        "thaw_lbs": _turkey_pounds(recipe),
    }
