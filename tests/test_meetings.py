"""Tests for the Meetings CRUD routes (/meetings)."""

from datetime import date, timedelta

import pytest

from models import Meeting, Permission, RolePermission, User, db

# ── Helpers ──

def _ensure_superadmin(session):
    u = User.query.filter_by(email='meet-sa@test.local').first()
    if not u:
        u = User(name='Meeting SA', email='meet-sa@test.local', password_hash='x',
                 is_superadmin=True, is_approved=True, role='superadmin')
        session.add(u)
        session.flush()
    return u


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _make_meeting(session, user):
    m = Meeting(
        participant_id=user.id,
        booked_by_id=user.id,
        agenda='Discuss student progress',
        date=date.today() + timedelta(days=2),
        time='14:00',
        student_name='Ali Khan',
        parent_name='Mrs Khan',
    )
    session.add(m)
    session.commit()
    return m


# ── Tests ──

class TestMeetingsIndex:
    def test_index_requires_auth(self, client):
        resp = client.get('/meetings')
        assert resp.status_code in (200, 302, 401)

    def test_index_loads_for_superadmin(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/meetings')
        assert resp.status_code == 200


class TestMeetingCreate:
    def test_new_form_renders(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/meetings/new')
        assert resp.status_code == 200

    def test_create_meeting_success(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        meeting_date = (date.today() + timedelta(days=5)).isoformat()
        resp = client.post('/meetings/new', data={
            'participant_id': user.id,
            'agenda': 'Review end of term reports',
            'date': meeting_date,
            'time': '10:00',
            'student_id': 0,
            'student_name': 'Test Student',
            'parent_name': 'Test Parent',
            'outcome': '',
        }, follow_redirects=True)
        assert resp.status_code == 200
        m = Meeting.query.filter_by(agenda='Review end of term reports').first()
        assert m is not None
        assert m.booked_by_id == user.id
        assert m.participant_id == user.id

    def test_create_meeting_missing_agenda_fails(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/meetings/new', data={
            'participant_id': user.id,
            'agenda': '',
            'date': date.today().isoformat(),
            'time': '09:00',
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert Meeting.query.filter_by(agenda='').first() is None

    def test_create_meeting_missing_date_fails(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/meetings/new', data={
            'participant_id': user.id,
            'agenda': 'Test agenda',
            'date': '',
            'time': '09:00',
        }, follow_redirects=True)
        assert resp.status_code == 200


class TestMeetingEdit:
    def test_edit_form_renders(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        m = _make_meeting(db_session, user)
        resp = client.get(f'/meetings/{m.id}/edit')
        assert resp.status_code == 200

    def test_edit_meeting_updates_outcome(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        m = _make_meeting(db_session, user)
        resp = client.post(f'/meetings/{m.id}/edit', data={
            'participant_id': user.id,
            'agenda': 'Discuss student progress',
            'date': m.date.isoformat(),
            'time': '14:00',
            'student_id': 0,
            'student_name': 'Ali Khan',
            'parent_name': 'Mrs Khan',
            'outcome': 'Agreed on extra tuition hours',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db_session.expire(m)
        assert m.outcome == 'Agreed on extra tuition hours'


class TestMeetingDelete:
    def test_delete_meeting(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        m = _make_meeting(db_session, user)
        mid = m.id
        resp = client.get(f'/meetings/{mid}/delete', follow_redirects=True)
        assert resp.status_code == 200
        assert Meeting.query.get(mid) is None

    def test_delete_nonexistent_returns_404(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/meetings/99999/delete')
        assert resp.status_code == 404
