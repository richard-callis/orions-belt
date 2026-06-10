"""
Shared pytest fixtures for Orion's Belt tests.
Uses an in-memory SQLite DB so tests are isolated and fast.
"""
import os
import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-ci")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:11434/v1")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL", "test-model")


@pytest.fixture(scope="session")
def app():
    from app import create_app, db as _db

    _app = create_app("config.TestConfig")
    with _app.app_context():
        _db.create_all()
        yield _app


@pytest.fixture(scope="function")
def db(app):
    from app import db as _db

    with app.app_context():
        yield _db
        _db.session.rollback()


@pytest.fixture(scope="function")
def client(app):
    """Test client with auth bypassed."""
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    with app.test_client() as c:
        with app.app_context():
            # Bypass auth middleware for tests
            from flask import g
            with c.session_transaction() as sess:
                sess["user_id"] = "test-user"
            yield c
