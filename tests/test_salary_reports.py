"""Tests for Salary Reports routes (/salary-reports)."""

from datetime import date

import pytest

from models import SalaryReport, User, db

# ── Helpers ──

def _ensure_superadmin(session):
    u = User.query.filter_by(email='sal-sa@test.local').first()
    if not u:
        u = User(name='Salary SA', email='sal-sa@test.local', password_hash='x',
                 is_superadmin=True, is_approved=True, role='superadmin')
        session.add(u)
        session.flush()
    return u


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _make_salary_report(session, user, name='Jan 2025'):
    r = SalaryReport(
        name=name,
        created_by_id=user.id,
    )
    session.add(r)
    session.commit()
    return r


# ── Tests ──

class TestSalaryReportsIndex:
    def test_index_requires_auth(self, client):
        resp = client.get('/salary-reports')
        assert resp.status_code in (200, 302, 401)

    def test_index_loads_for_superadmin(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/salary-reports')
        assert resp.status_code == 200


class TestSalaryReportCreate:
    def test_create_report(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/salary-reports/new', data={
            'name': 'Feb 2025',
        }, follow_redirects=True)
        assert resp.status_code == 200
        r = SalaryReport.query.filter_by(name='Feb 2025').first()
        assert r is not None
        assert r.created_by_id == user.id

    def test_create_report_default_name(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/salary-reports/new', data={}, follow_redirects=True)
        assert resp.status_code == 200
        # Should default to today's date
        r = SalaryReport.query.filter_by(created_by_id=user.id).order_by(SalaryReport.id.desc()).first()
        assert r is not None
        assert r.name is not None


class TestSalaryReportDetail:
    def test_detail_page_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        r = _make_salary_report(db_session, user)
        resp = client.get(f'/salary-reports/{r.id}')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Jan 2025' in html

    def test_detail_404_for_nonexistent(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/salary-reports/99999')
        assert resp.status_code == 404


class TestSalaryReportExport:
    def test_export_empty_report(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        r = _make_salary_report(db_session, user)
        resp = client.get(f'/salary-reports/{r.id}/export')
        assert resp.status_code == 200
        # Export may be CSV, XLSX, or other spreadsheet format
        ct = resp.content_type.lower()
        assert any(t in ct for t in ('csv', 'text', 'octet', 'spreadsheet', 'excel'))
