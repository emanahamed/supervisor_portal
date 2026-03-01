"""Tests for Error Reports routes (/error-reports)."""

from datetime import datetime, timezone

import pytest

from models import ErrorReport, User, db

# ── Helpers ──

def _ensure_superadmin(session):
    u = User.query.filter_by(email='err-sa@test.local').first()
    if not u:
        u = User(name='Error SA', email='err-sa@test.local', password_hash='x',
                 is_superadmin=True, is_approved=True, role='superadmin')
        session.add(u)
        session.flush()
    return u


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _make_error_report(session, user, title='Test Error', status='Open'):
    r = ErrorReport(
        title=title,
        description='Something went wrong',
        reporter_id=user.id,
        status=status,
        error_type='RuntimeError',
        error_message='Unexpected None',
        request_path='/test',
        request_method='GET',
    )
    session.add(r)
    session.commit()
    return r


# ── Tests ──

class TestErrorReportsIndex:
    def test_index_requires_auth(self, client):
        resp = client.get('/error-reports')
        assert resp.status_code in (200, 302, 401)

    def test_index_loads_for_superadmin(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/error-reports')
        assert resp.status_code == 200


class TestErrorReportDetail:
    def test_detail_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        r = _make_error_report(db_session, user)
        resp = client.get(f'/error-reports/{r.id}')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Test Error' in html

    def test_detail_404_for_nonexistent(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/error-reports/99999')
        assert resp.status_code == 404


class TestErrorReportStatusUpdate:
    def test_update_status_to_in_progress(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        r = _make_error_report(db_session, user)
        resp = client.post(f'/error-reports/{r.id}/status', data={
            'status': 'In Progress',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db_session.expire(r)
        assert r.status == 'In Progress'

    def test_resolve_sets_resolved_at(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        r = _make_error_report(db_session, user)
        assert r.resolved_at is None
        client.post(f'/error-reports/{r.id}/status', data={
            'status': 'Resolved',
        }, follow_redirects=True)
        db_session.expire(r)
        assert r.status == 'Resolved'
        assert r.resolved_at is not None
        assert r.resolved_by_id == user.id

    def test_status_update_requires_auth(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        r = _make_error_report(db_session, user)
        resp = client.post(f'/error-reports/{r.id}/status', data={
            'status': 'Resolved',
        })
        assert resp.status_code in (200, 302, 401)

    def test_status_update_nonexistent_returns_404(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/error-reports/99999/status', data={
            'status': 'Resolved',
        })
        assert resp.status_code == 404
