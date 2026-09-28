from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import URL

from app.storage.database import make_engine, make_session_factory


def test_engine_and_session_factory(database_url: URL) -> None:
    engine = make_engine(database_url.render_as_string(hide_password=False))
    try:
        factory = make_session_factory(engine)
        with factory() as session:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
        assert factory.kw["expire_on_commit"] is False
    finally:
        engine.dispose()
