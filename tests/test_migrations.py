from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.models import db


def test_migrations_match_models(app):
    """A model change without a matching migration fails here, not in production."""
    with app.app_context():
        with db.engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), db.metadata)
    assert diff == []
