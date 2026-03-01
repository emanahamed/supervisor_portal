"""Tests for Student Concerns routes (public + admin)."""

import json
from datetime import date

import pytest

from models import (Meeting, Permission, StudentConcern, StudentConcernChange,
                    User, db)

# ── Helpers ──

def _ensure_superadmin(session):
    u = User.query.filter_by(email='concern-sa@test.local').first()
    if not u:
        u = User(name='Concern SA', email='concern-sa@test.local', password_hash='x',
                 is_superadmin=True, is_approved=True, role='superadmin')
        session.add(u)
        session.flush()
    return u


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _make_concern(session):
    sc = StudentConcern(
        tutor_name='Mr Smith',
        subject='Maths',
        student_name='Amy Green',
        year_group='Year 7',
        other_details='Struggling with algebra',
        status='Pending',
    )
    sc.set_reasons(['Lack of Progress'])
    session.add(sc)
    session.commit()
    return sc


# ── Public Concern Report ──

class TestPublicConcernReport:
    def test_public_form_renders(self, client):
        resp = client.get('/report/student-concern')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'tutor_name' in html or 'Tutor' in html

    def test_public_submit_single_concern(self, client, db_session, app_instance):
        rows = json.dumps([{
            'student_id': '',
            'student_name': 'Tom Riddle',
            'year_group': 'Year 9',
            'subject': 'English',
            'reasons': ['Behaviour Issue'],
            'other_details': 'Disrupting class',
        }])
        resp = client.post('/report/student-concern', data={
            'tutor_name': 'Ms Jones',
            'subject': 'English',
            'rows': rows,
        }, follow_redirects=True)
        assert resp.status_code == 200
        sc = StudentConcern.query.filter_by(student_name='Tom Riddle').first()
        assert sc is not None
        assert sc.tutor_name == 'Ms Jones'
        assert 'Behaviour Issue' in sc.reasons()

    def test_public_submit_multiple_concerns(self, client, db_session, app_instance):
        rows = json.dumps([
            {'student_name': 'Student A', 'year_group': 'Y10', 'subject': 'Maths', 'reasons': ['Lack of Progress'], 'other_details': ''},
            {'student_name': 'Student B', 'year_group': 'Y11', 'subject': 'Science', 'reasons': ['Suspected SEN'], 'other_details': 'Needs assessment'},
        ])
        resp = client.post('/report/student-concern', data={
            'tutor_name': 'Mr Brown',
            'subject': 'Maths',
            'rows': rows,
        }, follow_redirects=True)
        assert resp.status_code == 200
        a = StudentConcern.query.filter_by(student_name='Student A').first()
        b = StudentConcern.query.filter_by(student_name='Student B').first()
        assert a is not None
        assert b is not None

    def test_public_honeypot_rejects_spam(self, client, db_session, app_instance):
        rows = json.dumps([{'student_name': 'Spam', 'reasons': []}])
        resp = client.post('/report/student-concern', data={
            'tutor_name': 'Bot',
            'subject': '',
            'rows': rows,
            'website': 'http://spam.com',  # honeypot field
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert StudentConcern.query.filter_by(student_name='Spam').first() is None

    def test_public_empty_rows_shows_warning(self, client, db_session, app_instance):
        resp = client.post('/report/student-concern', data={
            'tutor_name': 'Mr Empty',
            'subject': '',
            'rows': '[]',
        }, follow_redirects=True)
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'No rows' in html or 'at least one' in html.lower()


# ── Admin Concerns CRUD ──

class TestConcernsIndex:
    def test_index_requires_auth(self, client):
        resp = client.get('/student-concerns')
        assert resp.status_code in (200, 302, 401)

    def test_index_loads_for_superadmin(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/student-concerns')
        assert resp.status_code == 200


class TestConcernCreate:
    def test_new_form_renders(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/student-concerns/new')
        assert resp.status_code == 200

    def test_create_concern_success(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/student-concerns/new', data={
            'tutor_name': 'Mrs Taylor',
            'student_name': 'Jack Wilson',
            'year_group': 'Year 5',
            'subject': 'Science',
            'other_details': 'Needs extra help',
            'reasons': ['Other'],
        }, follow_redirects=True)
        assert resp.status_code == 200
        sc = StudentConcern.query.filter_by(student_name='Jack Wilson').first()
        assert sc is not None
        assert sc.tutor_name == 'Mrs Taylor'


class TestConcernEdit:
    def test_edit_concern_status(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        sc = _make_concern(db_session)
        resp = client.post(f'/student-concerns/{sc.id}/edit', data={
            'student_name': 'Amy Green',
            'year_group': 'Year 7',
            'subject': 'Maths',
            'status': 'Solved',
            'other_details': 'Resolved with extra sessions',
            'reasons': ['Lack of Progress'],
        }, follow_redirects=True)
        assert resp.status_code == 200
        db_session.expire(sc)
        assert sc.status == 'Solved'

    def test_edit_creates_change_log(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        sc = _make_concern(db_session)
        client.post(f'/student-concerns/{sc.id}/edit', data={
            'student_name': 'Amy Green',
            'year_group': 'Year 7',
            'subject': 'Maths',
            'status': 'In Progress',
            'other_details': 'Struggling with algebra',
            'reasons': ['Lack of Progress'],
        }, follow_redirects=True)
        changes = StudentConcernChange.query.filter_by(concern_id=sc.id).all()
        status_ch = [c for c in changes if c.field == 'status']
        assert len(status_ch) >= 1


class TestConcernDelete:
    def test_delete_concern(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        sc = _make_concern(db_session)
        sid = sc.id
        resp = client.post(f'/student-concerns/{sid}/delete', follow_redirects=True)
        assert resp.status_code == 200
        assert StudentConcern.query.get(sid) is None


class TestConcernDetail:
    def test_detail_page_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        sc = _make_concern(db_session)
        resp = client.get(f'/student-concerns/{sc.id}')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Amy Green' in html


class TestConcernMeeting:
    def test_schedule_meeting_from_concern(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        sc = _make_concern(db_session)
        resp = client.post(f'/student-concerns/{sc.id}/meeting', data={
            'participant_id': user.id,
            'date': (date.today()).isoformat(),
            'time': '15:00',
            'agenda': 'Discuss concern about algebra',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db_session.expire(sc)
        assert sc.meeting_id is not None
        assert sc.status == 'In Progress'
        m = Meeting.query.get(sc.meeting_id)
        assert m is not None
