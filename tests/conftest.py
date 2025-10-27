import os
import sys

import pytest

# Ensure project root (containing app.py) is importable when pytest changes cwd/context.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import app as flask_app  # noqa: E402
from app import db


@pytest.fixture(scope='session')
def app_instance():
    """Provide the Flask app configured for testing without altering existing DB data.

    We do NOT drop or recreate tables. Tests must not depend on an empty DB.
    """
    flask_app.config['TESTING'] = True
    flask_app.config['WTF_CSRF_ENABLED'] = False
    yield flask_app


# Backwards compatibility: some tests expect a fixture named 'app'
@pytest.fixture(scope='session')
def app(app_instance):
    return app_instance


@pytest.fixture()
def client(app_instance):
    return app_instance.test_client()


@pytest.fixture()
def db_session(app_instance):
    """Provide a database session isolated in a transaction per test.

    All writes in a test are rolled back afterwards, so test-created data is not persisted.
    """
    from sqlalchemy import event
    with app_instance.app_context():
        # Establish a connection and begin an outer transaction
        connection = db.engine.connect()
        trans = connection.begin()

        # Bind a new scoped session to this connection
        options = dict(bind=connection, binds={})
        scoped_session = db.create_scoped_session(options=options)

        # Swap the global session with our test-scoped one
        old_session = db.session
        db.session = scoped_session

        # Start a SAVEPOINT so tests can call commit safely; we'll keep re-spawning it
        nested = connection.begin_nested()

        @event.listens_for(scoped_session(), "after_transaction_end")
        def restart_savepoint(sess, tx):  # noqa: ANN001
            nonlocal nested
            # Re-open SAVEPOINT after each session.commit()
            if tx.nested and not tx._parent.nested:  # type: ignore[attr-defined]
                nested = connection.begin_nested()

        try:
            yield db.session
        finally:
            # Remove and rollback everything this test did
            scoped_session.remove()
            try:
                trans.rollback()
            finally:
                connection.close()
            # Restore the original global session
            db.session = old_session


@pytest.fixture()
def login_superadmin(app_instance, client, db_session):
    # Ensure a superadmin user exists and log them in via session
    from models import Permission, RolePermission, User
    with app_instance.app_context():
        u = User.query.filter_by(email='superadmin@test.local').first()
        if not u:
            u = User(name='Super Admin', email='superadmin@test.local', password_hash='x', is_superadmin=True, is_approved=True)
            db.session.add(u)
        # ensure manage_students permission exists
        perm = Permission.query.filter_by(key='manage_students').first()
        if not perm:
            perm = Permission(key='manage_students', description='Manage students')
            db.session.add(perm)
        # Flush so IDs are available without committing
        db.session.flush()
        uid = u.id
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
    return uid


@pytest.fixture()
def logged_in_superadmin_client(app_instance, client, login_superadmin, db_session):
    """Compatibility fixture for older tests expecting `logged_in_superadmin_client`.
    Returns the test client with a superadmin user id set in session.
    """
    return client
