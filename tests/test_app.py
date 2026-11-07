import re

from app.models import Appliance, DietaryFlag, Dish, Event, Guest, PrepTask, RecipeTiming, db

from .conftest import HOUSEHOLD, FakeMealie, event_id_from


def make_event(host, name="Christmas 2026", copy_from=""):
    r = host.post("/events/new", data={"name": name, "date": "2026-12-25", "serve_time": "14:00", "copy_from": copy_from})
    assert r.status_code == 302
    return event_id_from(r)


def setup_kitchen(host):
    host.post("/kitchen", data={"action": "preset", "preset": "oven"})
    host.post("/kitchen", data={"action": "preset", "preset": "burners4"})
    host.post("/kitchen", data={"action": "preset", "preset": "smoker"})


def test_login_required_and_bad_password(client):
    r = client.get("/")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    r = client.post("/login", data={"username": "taylor", "password": "wrong"})
    assert r.status_code == 401
    assert b"didn&#39;t accept" in r.data or b"didn't accept" in r.data


def test_login_next_is_not_an_open_redirect(client):
    r = client.post("/login", data={"username": "taylor", "password": "pw", "next": "https://evil.example/x"})
    assert r.headers["Location"] == "/"
    r = client.post("/login", data={"username": "taylor", "password": "pw", "next": "//evil.example"})
    assert r.headers["Location"] == "/"


def test_expired_mealie_token_sends_back_to_login(host, app):
    eid = make_event(host)
    with host.session_transaction() as s:
        s["mealie_token"] = "expired"
    r = host.get(f"/events/{eid}/menu/add?q=corn")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_new_event_seeds_generalware_and_all_pages_render(host, app):
    setup_kitchen(host)
    eid = make_event(host)
    with app.app_context():
        ev = db.session.get(Event, eid)
        names = [c.name for c in ev.checklist]
        assert "china" in names and "tables" in names
    for page in ["", "/guests", "/menu", "/menu/add?q=", "/timeline", "/today", "/prep", "/shopping", "/checklist", "/print"]:
        r = host.get(f"/events/{eid}{page}")
        assert r.status_code == 200, page
    assert host.get("/kitchen").status_code == 200
    assert host.get("/").status_code == 200


def test_other_household_cannot_see_event(host, app):
    eid = make_event(host)
    with app.app_context():
        ev = db.session.get(Event, eid)
        ev.household_id = "someone-else"
        db.session.commit()
    assert host.get(f"/events/{eid}").status_code == 404
    assert host.post(f"/events/{eid}/guests", data={"name": "X"}).status_code == 404


def test_guests_headcount_and_flags(host, app):
    eid = make_event(host)
    host.post("/kitchen", data={"action": "add_flag", "name": "Tree nuts", "safe": ["Tree Nut-Safe"]})
    with app.app_context():
        fid = DietaryFlag.query.first().id
    host.post(f"/events/{eid}/guests", data={"name": "Taylor, Morgan\nJordan", "rsvp": "yes"})
    host.post(f"/events/{eid}/guests", data={"name": "Gavin", "age": "child", "rsvp": "yes"})
    with app.app_context():
        guest = Guest.query.filter_by(name="Jordan").one()
        aid = guest.id
    host.post(f"/events/{eid}/guests/{aid}", data={"name": "Jordan", "age": "adult", "rsvp": "yes", "flags": [str(fid)]})
    with app.app_context():
        ev = db.session.get(Event, eid)
        assert ev.people == 4
        assert ev.headcount == 3.5
        assert [f.name for f in db.session.get(Guest, aid).flags] == ["Tree nuts"]


def test_recipe_times_come_from_mealie_and_host_settings_are_remembered(host, app, monkeypatch):
    from .conftest import RECIPES

    setup_kitchen(host)
    eid = make_event(host)
    r = host.post(f"/events/{eid}/menu/recipe", data={"slug": "southern-corn-casserole"})
    did = int(re.search(r"/dishes/(\d+)", r.headers["Location"]).group(1))
    with app.app_context():
        d = db.session.get(Dish, did)
        assert d.course == "side"
        # only the timed Mealie step becomes a step; the untimed preheat step is left off
        assert [(s.source_id, s.label, s.minutes, s.kind, s.temp) for s in d.steps] == [("2", "Step 2 · Bake", 45, "oven", 350)]
    # host sets what Mealie can't hold; a posted minutes value is ignored for Mealie steps
    host.post(
        f"/events/{eid}/dishes/{did}",
        data={"course": "side", "bringer": "host", "ready_at": "0", "max_hold": "30", "step_count": "1",
              "s0_source": "2", "s0_seq": "0", "s0_label": "Bake covered", "s0_minutes": "999", "s0_where": "oven", "s0_temp": "325"},
    )
    with app.app_context():
        d = db.session.get(Dish, did)
        assert [(s.label, s.minutes, s.temp) for s in d.steps] == [("Bake covered", 45, 325)]
        rt = db.session.get(RecipeTiming, (HOUSEHOLD, "r-corn"))
        assert rt.steps == [{"source_id": "2", "source_text": "bake for 45 minutes until set.", "label": "Bake covered", "kind": "oven", "appliance_id": None, "temp": 325, "hands_on": False}]
        assert rt.max_hold == 30
    # Mealie's time changes (and, like every Mealie save, the step ids are regenerated);
    # a refresh picks up the time and keeps the host's settings
    monkeypatch.setitem(RECIPES["southern-corn-casserole"]["recipeInstructions"][1], "durationSeconds", 3300)
    monkeypatch.setitem(RECIPES["southern-corn-casserole"]["recipeInstructions"][1], "id", "regenerated")
    host.post(f"/events/{eid}/menu/refresh")
    with app.app_context():
        assert [(s.label, s.minutes, s.temp) for s in db.session.get(Dish, did).steps] == [("Bake covered", 55, 325)]
    # another event gets the remembered settings with Mealie's current time
    eid2 = make_event(host, "Easter")
    host.post(f"/events/{eid2}/menu/recipe", data={"slug": "southern-corn-casserole"})
    with app.app_context():
        d2 = Dish.query.filter_by(event_id=eid2).one()
        assert [(s.label, s.minutes, s.temp) for s in d2.steps] == [("Bake covered", 55, 325)] and d2.max_hold == 30


def test_recipe_without_step_times_is_left_off_the_timeline(host, app, monkeypatch):
    from .conftest import RECIPES

    monkeypatch.setitem(RECIPES, "notimes", {
        "id": "r-nt", "slug": "notimes", "name": "No Times", "recipeServings": 4, "tags": [], "tools": [],
        "recipeCategory": [], "recipeInstructions": [{"id": "n1", "text": "Bake for 30 minutes at 350°F."}],
    })
    eid = make_event(host)
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "notimes"})
    with app.app_context():
        assert Dish.query.filter_by(name="No Times").one().steps == []
    assert "No Times" in host.get(f"/events/{eid}/timeline").data.decode()  # listed under "no timing yet"


def test_timeline_shows_conflicts_and_steps(host, app):
    setup_kitchen(host)
    eid = make_event(host)
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "southern-corn-casserole"})
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "dinner-rolls"})
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "pecan-pie"})
    r = host.get(f"/events/{eid}/timeline")
    html = r.data.decode()
    assert "Southern Corn Casserole" in html and "Heat Oven to" in html
    assert "Serve" in html


def test_missing_appliance_is_reported(host, app):
    eid = make_event(host)  # no kitchen at all
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "southern-corn-casserole"})
    html = host.get(f"/events/{eid}/timeline").data.decode()
    assert "your kitchen has no oven" in html


def test_shopping_sync_creates_list_and_applies_deltas(host, app):
    eid = make_event(host)
    host.post(f"/events/{eid}/guests", data={"name": ", ".join(f"G{i}" for i in range(11)), "rsvp": "yes"})
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "southern-corn-casserole"})  # 8 servings -> x1.5 for 11
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "pecan-pie", "bringer": "open"})  # not the host's
    host.post(f"/events/{eid}/shopping")
    lst = next(iter(FakeMealie.lists.values()))
    assert lst["recipeReferences"] == [{"recipeId": "r-corn", "recipeQuantity": 1.5}]
    # second sync is a no-op
    FakeMealie.calls.clear()
    host.post(f"/events/{eid}/shopping")
    assert FakeMealie.calls == []
    # headcount drops -> list shrinks
    with app.app_context():
        for gst in Guest.query.filter_by(event_id=eid).limit(5):
            gst.rsvp = "no"
        db.session.commit()
    host.post(f"/events/{eid}/shopping")
    assert lst["recipeReferences"] == [{"recipeId": "r-corn", "recipeQuantity": 1.0}]
    # dish removed -> recipe taken off
    with app.app_context():
        did = Dish.query.filter_by(event_id=eid, mealie_id="r-corn").one().id
    host.post(f"/events/{eid}/dishes/{did}", data={"action": "delete"})
    host.post(f"/events/{eid}/shopping")
    assert lst["recipeReferences"] == []


def test_prep_tasks_and_thaw_helper(host, app):
    eid = make_event(host)
    host.post(f"/events/{eid}/prep", data={"action": "add", "label": "Make brine", "days_before": "2"})
    host.post(f"/events/{eid}/prep", data={"action": "add", "label": "Set table", "when": "2026-12-24"})
    host.post(f"/events/{eid}/prep", data={"action": "thaw", "pounds": "14", "extra": "2"})
    with app.app_context():
        tasks = {t.label: t.days_before for t in PrepTask.query.filter_by(event_id=eid)}
    assert tasks["Make brine"] == 2 and tasks["Set table"] == 1
    thaw = [v for k, v in tasks.items() if k.startswith("Move the 14 lb")]
    assert thaw == [6]  # 14 lb -> 4 days in the fridge + 2 days before
    html = host.get(f"/events/{eid}/prep").data.decode()
    assert "Days before" in html and "Day before" in html


def test_checklist_counts_scale_with_people(host, app):
    eid = make_event(host)
    host.post(f"/events/{eid}/guests", data={"name": ", ".join(f"G{i}" for i in range(11)), "rsvp": "yes"})
    html = host.get(f"/events/{eid}/checklist").data.decode()
    assert "china <b>(22)</b>" in html
    assert "tables <b>(3)</b>" in html


def test_copy_event_resets_state(host, app):
    setup_kitchen(host)
    eid = make_event(host, "Christmas 2025")
    host.post(f"/events/{eid}/guests", data={"name": "Pat", "rsvp": "yes"})
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "dinner-rolls"})
    host.post(f"/events/{eid}/prep", data={"action": "add", "label": "Brine", "days_before": "2"})
    with app.app_context():
        for t in PrepTask.query.filter_by(event_id=eid):
            t.done = True
        db.session.commit()
    eid2 = make_event(host, "Christmas 2026", copy_from=str(eid))
    with app.app_context():
        ev2 = db.session.get(Event, eid2)
        assert [g.name for g in ev2.guests] == ["Pat"] and ev2.guests[0].rsvp == "invited"
        assert len(ev2.parties) == 1 and ev2.guests[0].party is ev2.parties[0]
        assert [d.name for d in ev2.dishes] == ["Dinner Rolls"] and ev2.dishes[0].steps
        assert [(t.label, t.done) for t in ev2.prep_tasks] == [("Brine", False)]
        assert ev2.guest_token != db.session.get(Event, eid).guest_token
        assert ev2.shopping_list_id is None


def test_dietary_status_from_mealie_tags(host, app):
    eid = make_event(host)
    host.post("/kitchen", data={"action": "add_flag", "name": "Tree nuts", "safe": ["Tree Nut-Safe"]})
    host.post("/kitchen", data={"action": "add_flag", "name": "Riley", "safe": ["Gluten-Safe"], "caution": ["Gluten-Modifiable"], "unsafe": ["Gluten-Unsafe"]})
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "dinner-rolls"})
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "pecan-pie"})
    from app.services import diet_status

    with app.app_context():
        nuts, gluten = DietaryFlag.query.order_by(DietaryFlag.id).all()
        rolls = Dish.query.filter_by(mealie_id="r-rolls").one()
        pie = Dish.query.filter_by(mealie_id="r-pie").one()
        assert diet_status(rolls, nuts) == "safe" and diet_status(pie, nuts) == "unknown"
        assert diet_status(rolls, gluten) == "caution" and diet_status(pie, gluten) == "unsafe"


def test_flag_suggestions_from_tags(host):
    html = host.get("/kitchen").data.decode()
    assert "Found in your Mealie tags" in html
    assert "Gluten-Safe / Gluten-Modifiable / Gluten-Unsafe" in html


# --- guest link ----------------------------------------------------------------


def guest_token(app, eid):
    with app.app_context():
        return db.session.get(Event, eid).guest_token


def test_guest_flow_group_rsvp_flags_claim_and_bring(host, client, app):
    setup_kitchen(host)
    eid = make_event(host)
    host.post("/kitchen", data={"action": "add_flag", "name": "Gluten-free", "safe": ["Gluten-Safe"]})
    host.post(f"/events/{eid}/guests", data={"name": "Alex, Sam, Riley", "together": "1"})
    host.post(f"/events/{eid}/guests", data={"name": "Jordan"})
    host.post(f"/events/{eid}/menu/dish", data={"name": "Green bean casserole", "course": "side", "bringer": "open"})
    tok = guest_token(app, eid)
    with app.app_context():
        j, d, b = [Guest.query.filter_by(name=n).one() for n in ("Alex", "Sam", "Riley")]
        assert j.party_id == d.party_id == b.party_id
        assert Guest.query.filter_by(name="Jordan").one().party_id != j.party_id
        jid, did, bid = j.id, d.id, b.id
        fid = DietaryFlag.query.one().id
        gb = Dish.query.filter_by(name="Green bean casserole").one().id

    g = app.test_client()
    page = g.get(f"/g/{tok}").data.decode()
    assert "Who are you?" in page and "Ask the host to add you" in page
    g.post(f"/g/{tok}/me", data={"guest_id": jid})
    page = g.get(f"/g/{tok}").data.decode()
    assert "Who&#39;s coming?" in page or "Who's coming?" in page
    # Alex answers for the group: Sam can't make it, Riley can and is gluten-free
    g.post(f"/g/{tok}/rsvp", data={f"rsvp_{jid}": "yes", f"rsvp_{did}": "no", f"rsvp_{bid}": "yes", f"flags_{bid}": [str(fid)], "note": "running late"})
    g.post(f"/g/{tok}/claim/{gb}")
    g.post(f"/g/{tok}/bring", data={"name": "Pumpkin pie", "course": "dessert", "arrive_time": "13:00", "needs": "oven", "need_minutes": "20", "need_temp": "325", f"diet_{fid}": "safe"})
    with app.app_context():
        assert [db.session.get(Guest, x).rsvp for x in (jid, did, bid)] == ["yes", "no", "yes"]
        assert [f.id for f in db.session.get(Guest, bid).flags] == [fid]
        assert db.session.get(Guest, jid).note == "running late"
        assert db.session.get(Dish, gb).bringer_guest_id == jid
        pie = Dish.query.filter_by(name="Pumpkin pie").one()
        assert pie.guest_added and pie.diet == {str(fid): "safe"}
        assert [(s.label, s.kind, s.minutes, s.temp) for s in pie.steps] == [("Reheat", "oven", 20, 325)]
        pie_id = pie.id
    # releasing a claimed host dish hands it back; deleting a guest-added one removes it
    g.post(f"/g/{tok}/dish/{gb}", data={"action": "delete"})
    g.post(f"/g/{tok}/dish/{pie_id}", data={"action": "delete"})
    with app.app_context():
        assert db.session.get(Dish, gb).bringer == "open"
        assert db.session.get(Dish, pie_id) is None


def test_guests_cannot_add_people_or_answer_for_other_groups(host, app):
    eid = make_event(host)
    host.post(f"/events/{eid}/guests", data={"name": "Alex, Sam", "together": "1"})
    host.post(f"/events/{eid}/guests", data={"name": "Pat"})
    tok = guest_token(app, eid)
    with app.app_context():
        jid = Guest.query.filter_by(name="Alex").one().id
        mom = Guest.query.filter_by(name="Pat").one().id
    g = app.test_client()
    assert g.post(f"/g/{tok}/me", data={"name": "Stranger"}).status_code == 404
    assert g.post(f"/g/{tok}/party", data={"name": "Plus one"}).status_code in (404, 405)
    g.post(f"/g/{tok}/me", data={"guest_id": jid})
    g.post(f"/g/{tok}/rsvp", data={f"rsvp_{mom}": "no"})  # Pat isn't in Alex's group: ignored
    with app.app_context():
        assert Guest.query.count() == 3
        assert db.session.get(Guest, mom).rsvp == "invited"
        assert db.session.get(Guest, jid).rsvp == "invited"  # saving without answering leaves it unanswered


def test_host_moves_guest_between_groups(host, app):
    eid = make_event(host)
    host.post(f"/events/{eid}/guests", data={"name": "Alex, Sam", "together": "1"})
    host.post(f"/events/{eid}/guests", data={"name": "Riley"})
    with app.app_context():
        b = Guest.query.filter_by(name="Riley").one()
        old_party = b.party_id
        target = Guest.query.filter_by(name="Alex").one().party_id
        bid = b.id
    host.post(f"/events/{eid}/guests/{bid}", data={"name": "Riley", "age": "child", "rsvp": "invited", "party": str(target)})
    with app.app_context():
        from app.models import Party

        assert db.session.get(Guest, bid).party_id == target
        assert db.session.get(Party, old_party) is None  # empty group cleaned up
        assert db.session.get(Party, target).label == "Alex, Sam & Riley"
    # adding straight into an existing group
    host.post(f"/events/{eid}/guests", data={"name": "Baby", "party": str(target), "age": "child"})
    with app.app_context():
        assert Guest.query.filter_by(name="Baby").one().party_id == target


def test_guest_cannot_touch_others_dishes_or_host_pages(host, app):
    eid = make_event(host)
    host.post(f"/events/{eid}/guests", data={"name": "A, B"})
    host.post(f"/events/{eid}/menu/dish", data={"name": "Ham", "bringer": "host"})
    tok = guest_token(app, eid)
    with app.app_context():
        a, b = [x.id for x in Guest.query.order_by(Guest.id)]
        ham = Dish.query.filter_by(name="Ham").one().id
    g = app.test_client()
    g.post(f"/g/{tok}/me", data={"guest_id": a})
    assert g.post(f"/g/{tok}/dish/{ham}", data={"action": "delete"}).status_code == 403
    assert g.post(f"/g/{tok}/claim/{ham}").status_code == 302  # not open: refused with a message
    with app.app_context():
        assert db.session.get(Dish, ham).bringer == "host"
    assert g.get(f"/events/{eid}").status_code == 302  # host pages need a Mealie login
    assert g.get(f"/events/{eid}/shopping").status_code == 302


def test_guest_link_off_and_rotate(host, app):
    eid = make_event(host)
    tok = guest_token(app, eid)
    host.post(f"/events/{eid}/link", data={"action": "disable"})
    assert host.get(f"/g/{tok}").status_code == 404
    host.post(f"/events/{eid}/link", data={"action": "rotate"})
    new = guest_token(app, eid)
    assert new != tok
    assert host.get(f"/g/{tok}").status_code == 404
    assert host.get(f"/g/{new}").status_code == 200


def test_guest_pages_are_noindex(host, app):
    eid = make_event(host)
    r = host.get(f"/g/{guest_token(app, eid)}")
    assert r.headers["X-Robots-Tag"].startswith("noindex")


def test_deleting_guest_reopens_their_dish(host, app):
    eid = make_event(host)
    host.post(f"/events/{eid}/guests", data={"name": "Pat"})
    with app.app_context():
        mom = Guest.query.filter_by(name="Pat").one().id
    host.post(f"/events/{eid}/menu/dish", data={"name": "Stuffing", "bringer": f"guest:{mom}"})
    host.post(f"/events/{eid}/guests/{mom}", data={"action": "delete"})
    with app.app_context():
        d = Dish.query.filter_by(name="Stuffing").one()
        assert d.bringer == "open" and d.bringer_guest_id is None


def test_csrf_enforced_when_enabled(monkeypatch, tmp_path):
    import app.auth as auth
    from app import create_app

    monkeypatch.setattr(auth, "Mealie", FakeMealie)
    a = create_app({"SECRET_KEY": "x", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/c.db", "SESSION_COOKIE_SECURE": False})
    c = a.test_client()
    assert c.post("/login", data={"username": "taylor", "password": "pw"}).status_code == 400


def test_kitchen_settings_and_appliances(host, app):
    host.post("/kitchen", data={"action": "add_appliance", "name": "Big Green Egg", "kind": "grill", "capacity": "2"})
    host.post("/kitchen", data={"action": "settings", "cooks": "2", "preheat_minutes": "20", "temp_tolerance": "30", "minutes_per_10f": "1.5", "default_hold": "25"})
    with app.app_context():
        a = Appliance.query.one()
        assert (a.name, a.kind, a.capacity) == ("Big Green Egg", "grill", 2)
        host.post("/kitchen", data={"action": "update_appliance", "id": a.id, "name": "Egg", "kind": "smoker", "capacity": "3"})
    with app.app_context():
        a = Appliance.query.one()
        assert (a.name, a.kind, a.capacity) == ("Egg", "smoker", 3)
        from app.models import KitchenSettings

        ks = db.session.get(KitchenSettings, HOUSEHOLD)
        assert (ks.cooks, ks.preheat_minutes, ks.temp_tolerance, ks.minutes_per_10f, ks.default_hold) == (2, 20, 30, 1.5, 25)


def test_overbooked_oven_renders_clash_on_every_page(host, app):
    host.post("/kitchen", data={"action": "add_appliance", "name": "Oven", "kind": "oven", "capacity": "1"})
    eid = make_event(host)
    for name, temp in [("A", "325"), ("B", "425"), ("C", "350")]:
        host.post(f"/events/{eid}/menu/dish", data={"name": name, "bringer": "host"})
        with app.app_context():
            did = Dish.query.filter_by(event_id=eid, name=name).one().id
        host.post(
            f"/events/{eid}/dishes/{did}",
            data={"ready_at": "0", "max_hold": "0", "step_count": "1", "s0_label": "Bake", "s0_minutes": "60", "s0_where": "oven", "s0_temp": temp},
        )
    for page in ["", "/timeline", "/today", "/print"]:
        r = host.get(f"/events/{eid}{page}")
        assert r.status_code == 200, page
        assert "double-booked" in r.data.decode(), page


def test_login_page_warns_on_plain_http(monkeypatch, tmp_path):
    import app.auth as auth
    from app import create_app

    monkeypatch.setattr(auth, "Mealie", FakeMealie)
    a = create_app({"SECRET_KEY": "x", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/h.db"})
    html = a.test_client().get("/login").data.decode()
    assert "only works over https" in html
    html = a.test_client().get("/login", base_url="https://gravyboat.example").data.decode()
    assert "only works over https" not in html


def test_burner_quick_add_names_are_positional(host, app):
    host.post("/kitchen", data={"action": "preset", "preset": "burners4"})
    host.post("/kitchen", data={"action": "preset", "preset": "burners5"})
    with app.app_context():
        names = [a.name for a in Appliance.query.order_by(Appliance.id)]
    assert names[:4] == ["Back left burner", "Back right burner", "Front left burner", "Front right burner"]
    assert "Center burner" in names[4:]


def test_overnight_steps_show_the_day(host, app):
    setup_kitchen(host)
    eid = make_event(host)  # Fri Dec 25 at 2 PM
    host.post(f"/events/{eid}/menu/dish", data={"name": "Brisket", "bringer": "host"})
    with app.app_context():
        did = Dish.query.filter_by(name="Brisket").one().id
    host.post(f"/events/{eid}/dishes/{did}", data={"ready_at": "0", "max_hold": "0", "step_count": "1", "s0_label": "Smoke", "s0_minutes": "1020", "s0_where": "smoker", "s0_temp": "250"})
    html = host.get(f"/events/{eid}/timeline").data.decode()
    assert "Thu 9 PM" in html  # 17 hours before 2 PM Friday


def test_make_ahead_instructions_become_prep_tasks(host, app, monkeypatch):
    from .conftest import RECIPES

    monkeypatch.setitem(RECIPES, "slush", {
        "id": "r-slush", "slug": "slush", "name": "Slush Punch", "recipeServings": 50, "tags": [], "tools": [],
        "recipeCategory": [], "recipeInstructions": [{"id": "s1", "text": "Mix everything.", "durationSeconds": 300}, {"id": "s2", "text": "Freeze for at least 24 hours, until solid.", "durationSeconds": 86400}],
    })
    eid = make_event(host)
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "slush"})
    with app.app_context():
        assert [t.label for t in PrepTask.query.filter_by(event_id=eid)] == ["Slush Punch: Step 2: Freeze for at least 24 hours, until solid."]
        assert [s.label for s in Dish.query.filter_by(name="Slush Punch").one().steps] == ["Step 1 · Prep"]


def test_refresh_keeps_hand_entered_timing(host, app):
    eid = make_event(host)
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "dinner-rolls"})
    with app.app_context():
        from app.models import Step

        d = Dish.query.filter_by(event_id=eid).one()
        d.steps = [Step(seq=0, label="Rise", minutes=260), Step(seq=1, label="Bake", minutes=20, kind="oven", temp=350)]
        db.session.commit()
    host.post(f"/events/{eid}/menu/refresh")
    with app.app_context():
        assert [(s.label, s.minutes) for s in Dish.query.filter_by(event_id=eid).one().steps] == [("Rise", 260), ("Bake", 20)]


def test_sign_in_with_api_token(client):
    r = client.post("/login", data={"api_token": "tok-taylor"})
    assert r.status_code == 302 and r.headers["Location"] == "/"
    assert client.post("/login", data={"api_token": "wrong"}).status_code == 401


def test_old_mealie_gets_a_warning(client):
    FakeMealie.version_tuple = (3, 29, 0)
    client.post("/login", data={"username": "taylor", "password": "pw"})
    assert "older than v3.30" in client.get("/").data.decode()


def test_repeated_failed_sign_ins_are_slowed_down(client):
    for _ in range(10):
        assert client.post("/login", data={"username": "taylor", "password": "nope"}).status_code == 401
    r = client.post("/login", data={"username": "taylor", "password": "pw"})
    assert r.status_code == 429  # even the right password waits out the window


def test_switching_to_celsius_converts_saved_temperatures(host, app):
    setup_kitchen(host)
    eid = make_event(host)
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "dinner-rolls"})  # bakes at 375°F
    host.post("/kitchen", data={"action": "settings", "temp_unit": "C", "cooks": "1", "preheat_minutes": "15",
                                "temp_tolerance": "25", "minutes_per_10f": "1", "default_hold": "20"})
    with app.app_context():
        from app.models import KitchenSettings

        ks = db.session.get(KitchenSettings, HOUSEHOLD)
        assert ks.temp_unit == "C" and ks.temp_tolerance == 14
        assert Dish.query.filter_by(event_id=eid).one().steps[0].temp == 190
    html = host.get(f"/events/{eid}/timeline").data.decode()
    assert "190°C" in html and "°F" not in html
    # a recipe added after the switch is read straight into °C
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "pecan-pie"})  # 350°F
    with app.app_context():
        assert Dish.query.filter_by(event_id=eid, mealie_id="r-pie").one().steps[0].temp == 175


def test_guest_page_links_recipes_in_the_hosts_mealie_group(host, app):
    eid = make_event(host)
    host.post(f"/events/{eid}/menu/recipe", data={"slug": "dinner-rolls"})
    host.post(f"/events/{eid}/guests", data={"name": "Pat"})
    with app.app_context():
        ev = db.session.get(Event, eid)
        assert ev.group_slug == "home"
        ev.group_slug = "family"
        db.session.commit()
        pat = Guest.query.filter_by(name="Pat").one().id
    g = app.test_client()
    tok = guest_token(app, eid)
    g.post(f"/g/{tok}/me", data={"guest_id": pat})
    html = g.get(f"/g/{tok}").data.decode()
    assert "https://recipes.example/g/family/r/dinner-rolls" in html


def test_pages_make_no_third_party_requests(host):
    html = host.get("/kitchen").data.decode()
    assert "googleapis" not in html and "gstatic" not in html


def test_running_late_button_does_not_redirect_off_site(host):
    eid = make_event(host)
    r = host.post(f"/events/{eid}/shift", data={"minutes": "15", "back": "https://evil.example/"})
    assert r.headers["Location"] == "/"
    r = host.post(f"/events/{eid}/shift", data={"minutes": "15", "back": f"/events/{eid}/today"})
    assert r.headers["Location"] == f"/events/{eid}/today"


def test_security_headers(client):
    r = client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
