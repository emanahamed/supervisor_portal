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
    # Disable CSRF in tests for simplicity
    flask_app.config['WTF_CSRF_ENABLED'] = False
    with flask_app.app_context():
        db.create_all()
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
    with app_instance.app_context():
        yield db.session


@pytest.fixture()
def login_superadmin(app_instance, client):
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
        # ensure role permission for superadmin not required (superadmin bypass) but keep for clarity
            db.session.commit()
        uid = u.id
    with client.session_transaction() as sess:
        sess['_user_id'] = str(uid)
    return uid
