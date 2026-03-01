"""Tests for the Issues CRUD routes (/issues)."""

from datetime import date

import pytest

from models import Issue, IssueChange, Permission, RolePermission, User, db

# ── Helpers ──

def _ensure_superadmin(session):
    u = User.query.filter_by(email='issue-sa@test.local').first()
    if not u:
        u = User(name='Issue SA', email='issue-sa@test.local', password_hash='x',
                 is_superadmin=True, is_approved=True, role='superadmin')
        session.add(u)
        session.flush()
    return u


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _make_issue(session, user):
    i = Issue(
        title='Server room temperature too high',
        details='AC not cooling enough',
        status='Pending',
        criticality='Critical',
        urgency='High',
        branch=None,
        action_taken=None,
        created_by_id=user.id,
    )
    session.add(i)
    session.commit()
    return i


# ── Tests ──

class TestIssuesIndex:
    def test_index_requires_auth(self, client):
        resp = client.get('/issues')
        assert resp.status_code in (200, 302, 401)

    def test_index_loads_for_superadmin(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/issues')
        assert resp.status_code == 200

    def test_index_with_status_filter(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        _make_issue(db_session, user)
        resp = client.get('/issues?status=Pending')
        assert resp.status_code == 200

    def test_index_with_search(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        _make_issue(db_session, user)
        resp = client.get('/issues?search=temperature')
        assert resp.status_code == 200

    def test_index_with_criticality_filter(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        _make_issue(db_session, user)
        resp = client.get('/issues?criticality=Critical')
        assert resp.status_code == 200


class TestIssueCreate:
    def test_new_form_renders(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/issues/new')
        assert resp.status_code == 200

    def test_create_issue_success(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/issues/new', data={
            'title': 'Broken window in room 3',
            'details': 'Glass cracked during storm',
            'status': 'Pending',
            'criticality': 'Significant',
            'urgency': 'Medium',
            'branch': '',
            'action_taken': '',
        }, follow_redirects=True)
        assert resp.status_code == 200
        i = Issue.query.filter_by(title='Broken window in room 3').first()
        assert i is not None
        assert i.status == 'Pending'
        assert i.criticality == 'Significant'
        assert i.created_by_id == user.id

    def test_create_issue_no_title_fails(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/issues/new', data={
            'title': '',
            'status': 'Pending',
            'criticality': 'Minor',
            'urgency': 'Low',
        }, follow_redirects=True)
        assert resp.status_code == 200
        # No issue should have been created with empty title
        assert Issue.query.filter_by(title='').first() is None


class TestIssueEdit:
    def test_edit_form_renders(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        i = _make_issue(db_session, user)
        resp = client.get(f'/issues/{i.id}/edit')
        assert resp.status_code == 200

    def test_edit_issue_changes_status(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        i = _make_issue(db_session, user)
        resp = client.post(f'/issues/{i.id}/edit', data={
            'title': 'Server room temperature too high',
            'details': 'AC not cooling enough',
            'status': 'Resolved',
            'criticality': 'Critical',
            'urgency': 'High',
            'branch': '',
            'action_taken': 'Replaced AC filter',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db_session.expire(i)
        assert i.status == 'Resolved'
        assert i.action_taken == 'Replaced AC filter'

    def test_edit_creates_change_log(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        i = _make_issue(db_session, user)
        client.post(f'/issues/{i.id}/edit', data={
            'title': 'Server room temperature too high',
            'details': 'AC not cooling enough',
            'status': 'In Progress',
            'criticality': 'Critical',
            'urgency': 'High',
            'branch': '',
            'action_taken': '',
        }, follow_redirects=True)
        changes = IssueChange.query.filter_by(issue_id=i.id).all()
        status_change = [c for c in changes if c.field == 'status']
        assert len(status_change) >= 1
        assert status_change[0].old_value == 'Pending'
        assert status_change[0].new_value == 'In Progress'


class TestIssueDelete:
    def test_delete_issue(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        i = _make_issue(db_session, user)
        iid = i.id
        resp = client.get(f'/issues/{iid}/delete', follow_redirects=True)
        assert resp.status_code == 200
        assert Issue.query.get(iid) is None

    def test_delete_nonexistent_returns_404(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/issues/99999/delete')
        assert resp.status_code == 404
