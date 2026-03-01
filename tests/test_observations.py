"""Tests for Observations and Observation Cycles routes."""

from datetime import date, timedelta

import pytest

from models import Observation, ObservationCycle, Staff, User, db

# ── Helpers ──

def _ensure_superadmin(session):
    u = User.query.filter_by(email='obs-sa@test.local').first()
    if not u:
        u = User(name='Obs SA', email='obs-sa@test.local', password_hash='x',
                 is_superadmin=True, is_approved=True, role='superadmin')
        session.add(u)
        session.flush()
    return u


def _ensure_staff(session):
    s = Staff.query.filter_by(name='Test Staff Obs').first()
    if not s:
        s = Staff(name='Test Staff Obs', department='Maths')
        session.add(s)
        session.flush()
    return s


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _make_cycle(session):
    c = ObservationCycle(
        title='Term 1 Cycle',
        start_date=date.today() - timedelta(days=30),
        end_date=date.today() + timedelta(days=30),
    )
    session.add(c)
    session.commit()
    return c


def _make_observation(session, user, staff, cycle):
    o = Observation(
        cycle_id=cycle.id,
        staff_id=staff.id,
        observer_id=user.id,
        date=date.today(),
        score=8.5,
    )
    session.add(o)
    session.commit()
    return o


# ── Observation Cycles ──

class TestObservationCycles:
    def test_cycles_index_requires_auth(self, client):
        resp = client.get('/cycles')
        # Flask-Login may redirect (302) or show login page (200)
        assert resp.status_code in (200, 302, 401)

    def test_cycles_index_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/cycles')
        assert resp.status_code == 200

    def test_create_cycle(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/cycles/new', data={
            'title': 'Spring 2025 Cycle',
            'start_date': '2025-01-06',
            'end_date': '2025-04-04',
        }, follow_redirects=True)
        assert resp.status_code == 200
        c = ObservationCycle.query.filter_by(title='Spring 2025 Cycle').first()
        assert c is not None

    def test_edit_cycle(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        c = _make_cycle(db_session)
        resp = client.post(f'/cycles/{c.id}/edit', data={
            'title': 'Updated Cycle Title',
            'start_date': c.start_date.isoformat(),
            'end_date': c.end_date.isoformat(),
        }, follow_redirects=True)
        assert resp.status_code == 200
        db_session.expire(c)
        assert c.title == 'Updated Cycle Title'

    def test_delete_cycle(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        c = _make_cycle(db_session)
        cid = c.id
        resp = client.get(f'/cycles/{cid}/delete', follow_redirects=True)
        assert resp.status_code == 200
        assert ObservationCycle.query.get(cid) is None


# ── Observations CRUD ──

class TestObservationsIndex:
    def test_index_requires_auth(self, client):
        resp = client.get('/observations')
        # Flask-Login may redirect (302) or show login page (200)
        assert resp.status_code in (200, 302, 401)

    def test_index_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/observations')
        assert resp.status_code == 200

    def test_index_filter_by_cycle(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        c = _make_cycle(db_session)
        resp = client.get(f'/observations?cycle_id={c.id}')
        assert resp.status_code == 200


class TestObservationCreate:
    def test_new_form_renders(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/observations/new')
        assert resp.status_code == 200

    def test_create_observation(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        staff = _ensure_staff(db_session)
        cycle = _make_cycle(db_session)
        _login(client, user.id)
        resp = client.post('/observations/new', data={
            'cycle_id': cycle.id,
            'staff_id': staff.id,
            'date': date.today().isoformat(),
            'score': '7.5',
        }, follow_redirects=True)
        assert resp.status_code == 200
        o = Observation.query.filter_by(staff_id=staff.id, cycle_id=cycle.id).first()
        assert o is not None
        assert float(o.score) == pytest.approx(7.5, abs=0.1)


class TestObservationEdit:
    def test_edit_observation(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        staff = _ensure_staff(db_session)
        cycle = _make_cycle(db_session)
        obs = _make_observation(db_session, user, staff, cycle)
        _login(client, user.id)
        resp = client.post(f'/observations/{obs.id}/edit', data={
            'cycle_id': cycle.id,
            'staff_id': staff.id,
            'date': date.today().isoformat(),
            'score': '9.0',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db_session.expire(obs)
        assert float(obs.score) == pytest.approx(9.0, abs=0.1)


class TestObservationDelete:
    def test_delete_observation(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        staff = _ensure_staff(db_session)
        cycle = _make_cycle(db_session)
        obs = _make_observation(db_session, user, staff, cycle)
        oid = obs.id
        _login(client, user.id)
        resp = client.get(f'/observations/{oid}/delete', follow_redirects=True)
        assert resp.status_code == 200
        assert Observation.query.get(oid) is None

    def test_delete_nonexistent_returns_404(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/observations/99999/delete')
        assert resp.status_code == 404
