"""Tests for Floor Plan routes (shifts, checklists, reports, call list)."""

import json
from datetime import date, timedelta

import pytest

from models import (CallRecord, EndOfDayChecklist, PrintReport, Shift, Student,
                    User, db)

# ── Helpers ──

def _ensure_superadmin(session):
    u = User.query.filter_by(email='floor-sa@test.local').first()
    if not u:
        u = User(name='Floor SA', email='floor-sa@test.local', password_hash='x',
                 is_superadmin=True, is_approved=True, role='superadmin')
        session.add(u)
        session.flush()
    return u


def _ensure_student(session):
    s = Student.query.filter_by(name='Floor Test Student').first()
    if not s:
        s = Student(name='Floor Test Student', student_id='FLOOR-STU-001')
        session.add(s)
        session.flush()
    return s


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _make_shift(session, user):
    s = Shift(
        staff_user_id=user.id,
        date=date.today(),
        day=date.today().strftime('%A'),
        timeslots='9-11,11-1',
        branch='Whitechapel',
        floors='Ground Floor',
        notes='Test shift',
    )
    session.add(s)
    session.commit()
    return s


# ── Floor Dashboard ──

class TestFloorDashboard:
    def test_dashboard_requires_auth(self, client):
        resp = client.get('/floor')
        assert resp.status_code in (200, 302, 401)

    def test_dashboard_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/floor')
        assert resp.status_code == 200


# ── Shifts ──

class TestFloorShifts:
    def test_shifts_index_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/floor/shifts')
        assert resp.status_code == 200

    def test_create_shift(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/floor/shifts/new', data={
            'staff_user_id': user.id,
            'date': date.today().strftime('%Y-%m-%d'),
            'timeslots': ['9-11', '11-1'],
            'branch': 'Whitechapel',
            'floors': ['Ground Floor'],
            'notes': 'Created in test',
        }, follow_redirects=True)
        assert resp.status_code == 200
        s = Shift.query.filter_by(staff_user_id=user.id, notes='Created in test').first()
        assert s is not None

    def test_delete_shift(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        s = _make_shift(db_session, user)
        sid = s.id
        resp = client.post(f'/floor/shifts/{sid}/delete', follow_redirects=True)
        assert resp.status_code == 200
        assert Shift.query.get(sid) is None


# ── Checklists ──

class TestFloorChecklists:
    def test_checklists_index_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/floor/checklists')
        assert resp.status_code == 200

    def test_create_checklist(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        shift = _make_shift(db_session, user)
        items = json.dumps([{'todo': 'Clean whiteboard', 'value': True}])
        resp = client.post('/floor/checklists/new', data={
            'shift_id': shift.id,
            'staff_user_id': user.id,
            'date': date.today().isoformat(),
            'floor': 'Ground Floor',
            'items': items,
        }, follow_redirects=True)
        assert resp.status_code == 200


# ── Reports ──

class TestFloorReports:
    def test_reports_index_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/floor/reports')
        assert resp.status_code == 200

    def test_create_report(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        shift = _make_shift(db_session, user)
        resp = client.post('/floor/reports/new', data={
            'staff_user_id': user.id,
            'date': date.today().isoformat(),
            'floor': 'Ground Floor',
            'branch': 'Whitechapel',
            'pages_printed': '5',
            'has_unapproved': '',
            'unapproved_details': '',
            'notes': 'End of day report',
            'shift_id': shift.id,
        }, follow_redirects=True)
        assert resp.status_code == 200
        r = PrintReport.query.filter_by(notes='End of day report').first()
        assert r is not None
        assert r.pages_printed == 5


# ── Call List ──

class TestFloorCallList:
    def test_call_list_index_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/floor/call-list')
        assert resp.status_code == 200

    def test_call_list_new_form_renders(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/floor/call-list/new')
        assert resp.status_code == 200

    def test_create_call_record(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        student = _ensure_student(db_session)
        _login(client, user.id)
        resp = client.post('/floor/call-list/new', data={
            'student_id': student.id,
            'reason': 'absence',
            'date': date.today().isoformat(),
            'discussion': 'Called parent about absence',
            'outcome': 'Will bring doctor note',
        }, follow_redirects=True)
        assert resp.status_code == 200
        cr = CallRecord.query.filter_by(student_id=student.id, reason='absence').first()
        assert cr is not None
        assert cr.discussion == 'Called parent about absence'
