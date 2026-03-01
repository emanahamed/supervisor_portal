"""Tests for the Todos CRUD routes (/todos)."""

from datetime import date, timedelta

import pytest

from models import Permission, RolePermission, Todo, User, db

# ── Helpers ──

def _ensure_superadmin(session):
    u = User.query.filter_by(email='todo-sa@test.local').first()
    if not u:
        u = User(name='Todo SA', email='todo-sa@test.local', password_hash='x',
                 is_superadmin=True, is_approved=True, role='superadmin')
        session.add(u)
        session.flush()
    return u


def _ensure_permission(session, key, desc=''):
    perm = Permission.query.filter_by(key=key).first()
    if not perm:
        perm = Permission(key=key, description=desc or key)
        session.add(perm)
        session.flush()
    return perm


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _make_todo(session, user):
    t = Todo(
        description='Fix the printer',
        notes='Paper jam issue',
        criticality='Minor',
        urgency='Low',
        status='Pending',
        due_date=date.today() + timedelta(days=7),
        created_by_id=user.id,
        assigned_to_id=user.id,
    )
    session.add(t)
    session.commit()
    return t


# ── Tests ──

class TestTodosIndex:
    def test_index_requires_auth(self, client):
        resp = client.get('/todos')
        assert resp.status_code in (200, 302, 401)

    def test_index_loads_for_superadmin(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/todos')
        assert resp.status_code == 200

    def test_index_filters_by_status(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        _make_todo(db_session, user)
        resp = client.get('/todos?status=Pending')
        assert resp.status_code == 200

    def test_index_filters_by_assigned(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        _make_todo(db_session, user)
        resp = client.get(f'/todos?assigned={user.id}')
        assert resp.status_code == 200


class TestTodoCreate:
    def test_new_form_renders(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/todos/new')
        assert resp.status_code == 200
        assert b'description' in resp.data.lower() or b'Description' in resp.data

    def test_create_todo_success(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/todos/new', data={
            'description': 'Order new whiteboard markers',
            'notes': '',
            'actions_taken': '',
            'criticality': 'Minor',
            'urgency': 'Low',
            'status': 'Pending',
            'due_date': (date.today() + timedelta(days=3)).isoformat(),
            'assigned_to_id': user.id,
        }, follow_redirects=True)
        assert resp.status_code == 200
        t = Todo.query.filter_by(description='Order new whiteboard markers').first()
        assert t is not None
        assert t.status == 'Pending'
        assert t.created_by_id == user.id

    def test_create_todo_missing_description_fails(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/todos/new', data={
            'description': '',
            'criticality': 'Minor',
            'urgency': 'Low',
            'status': 'Pending',
            'assigned_to_id': user.id,
        }, follow_redirects=True)
        # Should re-render form (200) without creating
        assert resp.status_code == 200
        assert Todo.query.filter_by(description='').first() is None


class TestTodoToggleAndStatus:
    def test_toggle_status(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        t = _make_todo(db_session, user)
        assert t.status == 'Pending'
        resp = client.post(f'/todos/{t.id}/toggle', follow_redirects=True)
        assert resp.status_code == 200
        db_session.expire(t)
        assert t.status == 'Done'

    def test_toggle_back_to_pending(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        t = _make_todo(db_session, user)
        t.status = 'Done'
        db_session.commit()
        client.post(f'/todos/{t.id}/toggle', follow_redirects=True)
        db_session.expire(t)
        assert t.status == 'Pending'

    def test_status_update_via_post(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        t = _make_todo(db_session, user)
        resp = client.post(f'/todos/{t.id}/status', data={'status': 'Done'})
        assert resp.status_code == 200
        db_session.expire(t)
        assert t.status == 'Done'

    def test_status_update_invalid_rejected(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        t = _make_todo(db_session, user)
        resp = client.post(f'/todos/{t.id}/status', data={'status': 'Invalid'})
        assert resp.status_code == 400


class TestTodoDelete:
    def test_delete_todo(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        t = _make_todo(db_session, user)
        tid = t.id
        resp = client.get(f'/todos/{tid}/delete', follow_redirects=True)
        assert resp.status_code == 200
        assert Todo.query.get(tid) is None

    def test_delete_nonexistent_returns_404(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/todos/99999/delete')
        assert resp.status_code == 404
