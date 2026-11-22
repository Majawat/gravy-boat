from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

import flask_migrate
from sqlalchemy import text

from app import create_app
from app.models import db


def test_migrations_match_models(app):
    """A model change without a matching migration fails here, not in production."""
    with app.app_context():
        with db.engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
    assert diff == []


def test_upgrade_keeps_existing_dishes_and_steps(tmp_path, monkeypatch):
    """Adding columns must not rebuild tables: with foreign keys on, rebuilding `dish` would cascade-delete steps."""
    a = create_app({"TESTING": True, "SECRET_KEY": "t", "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path}/m.db", "SESSION_COOKIE_SECURE": False})
    with a.app_context():
        flask_migrate.upgrade(revision="d02e2cc040ce")
        with db.engine.begin() as c:
            c.execute(text("INSERT INTO event (id, household_id, name, date, serve_time, location, notes, guest_token, guest_link_enabled, created_by, created_at) VALUES (1, 'h', 'E', '2026-11-26', '13:00:00', '', '', 'tok', 1, 'x', '2026-11-01 00:00:00')"))
            c.execute(text("INSERT INTO dish (id, event_id, name, course, bringer, mealie_tags, mealie_tools, diet, serveware, notes, ready_at, guest_added, sort) VALUES (1, 1, 'Turkey', 'main', 'host', '[]', '[]', '{}', '', '', 0, 0, 0)"))
            c.execute(text("INSERT INTO step (id, dish_id, seq, label, minutes, hands_on) VALUES (1, 1, 0, 'Smoke', 120, 0), (2, 1, 1, 'Rest', 30, 0)"))
            c.execute(text("INSERT INTO prep_task (id, event_id, dish_id, label, days_before, done) VALUES (1, 1, 1, 'Thaw', 3, 0)"))
        flask_migrate.upgrade()
        with db.engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM step")).scalar() == 2
            assert c.execute(text("SELECT dish_id FROM prep_task")).scalar() == 1
            assert c.execute(text("SELECT make_ahead_days, whole_batches, components FROM dish")).one() == (0, 0, "[]")
            assert c.execute(text("SELECT uses FROM step WHERE id = 1")).scalar() == "[]"
        db.session.remove()
