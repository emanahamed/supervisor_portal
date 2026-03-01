"""Tests for superadmin route accessibility and public routes.

Note: The app auto-authenticates a superadmin in test mode (before_request hook),
so unauthenticated access tests are not applicable.  These tests verify that key
routes are reachable and render without errors.
"""

import pytest

from models import User, db

# ── Helpers ──

def _ensure_superadmin(session):
    u = User.query.filter_by(email='dash-sa@test.local').first()
    if not u:
        u = User(name='Dash SA', email='dash-sa@test.local', password_hash='x',
                 is_superadmin=True, is_approved=True, role='superadmin')
        session.add(u)
        session.flush()
    return u


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


# ── Protected routes return 200 when authenticated ──

class TestProtectedRoutesAccessible:
    """Verify key routes are accessible and render correctly."""

    ROUTES = [
        '/todos',
        '/issues',
        '/meetings',
        '/observations',
        '/student-concerns',
        '/salary-reports',
        '/error-reports',
        '/floor',
        '/floor/shifts',
        '/floor/checklists',
        '/floor/reports',
        '/floor/call-list',
        '/admin/dbs',
        '/cycles',
    ]

    @pytest.mark.parametrize('route', ROUTES)
    def test_route_returns_200(self, client, db_session, app_instance, route):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get(route)
        assert resp.status_code == 200, f'{route} should be accessible to superadmin'


# ── Public Routes ──

class TestPublicRoutes:
    """Verify public routes are accessible without login."""

    def test_dbs_apply_public(self, client):
        resp = client.get('/dbs/apply')
        assert resp.status_code == 200

    def test_report_student_concern_public(self, client):
        resp = client.get('/report/student-concern')
        assert resp.status_code == 200

    def test_login_page_accessible(self, client):
        resp = client.get('/login')
        assert resp.status_code == 200


# ── Error Handling ──

class TestErrorPages:
    def test_404_for_nonexistent_route(self, client):
        resp = client.get('/this-route-does-not-exist')
        assert resp.status_code == 404
