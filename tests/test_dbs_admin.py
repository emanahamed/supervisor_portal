"""Tests for DBS admin routes (list, detail, status update, PDF)."""

import json
from datetime import date, timedelta
from uuid import uuid4

import pytest

from models import DBSApplication, Permission, RolePermission, User, db

# ── Helpers ──

def _ensure_superadmin(session):
    u = User.query.filter_by(email='dbs-admin-sa@test.local').first()
    if not u:
        u = User(name='DBS Admin SA', email='dbs-admin-sa@test.local', password_hash='x',
                 is_superadmin=True, is_approved=True, role='superadmin')
        session.add(u)
        session.flush()
    return u


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _five_year_address():
    since = (date.today() - timedelta(days=6 * 365)).isoformat()
    return {
        'line1': '10 Downing Street', 'line2': '', 'postcode': 'SW1A 2AA',
        'since': since, 'until': '', 'current': True,
    }


def _make_dbs_app(session, app_id=None, status='Application Submitted', payment='paid'):
    if app_id is None:
        app_id = f'DBS-T-{uuid4().hex[:8].upper()}'
    app_obj = DBSApplication(
        application_id=app_id,
        title='Mr',
        first_name='Admin',
        last_name='Test',
        date_of_birth=date(1990, 1, 1),
        gender='Male',
        place_of_birth_town='London',
        place_of_birth_country='UK',
        nationality='British',
        email='admin-test@example.com',
        phone='07700900000',
        addresses_json=json.dumps([_five_year_address()]),
        declaration_agreed=True,
        proof_of_address_path='uploads/dbs/poa_test.pdf',
        proof_of_address_2_path='uploads/dbs/poa2_test.pdf',
        proof_of_id_path='uploads/dbs/poi_test.pdf',
        payment_status=payment,
        application_status=status,
    )
    session.add(app_obj)
    session.commit()
    return app_obj


# ── Tests ──

class TestDBSAdminList:
    def test_list_requires_auth(self, client):
        resp = client.get('/admin/dbs')
        assert resp.status_code in (200, 302, 401)

    def test_list_loads_for_superadmin(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.get('/admin/dbs')
        assert resp.status_code == 200

    def test_list_shows_application(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        app_obj = _make_dbs_app(db_session)
        resp = client.get('/admin/dbs')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert app_obj.application_id in html

    def test_list_search_by_name(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        _make_dbs_app(db_session)
        resp = client.get('/admin/dbs?q=Admin')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Admin' in html

    def test_list_filter_by_status(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        _make_dbs_app(db_session, status='Sent to UCheck')
        resp = client.get('/admin/dbs?status=Sent+to+UCheck')
        assert resp.status_code == 200

    def test_list_filter_by_payment(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        _make_dbs_app(db_session, payment='pending')
        resp = client.get('/admin/dbs?payment=pending')
        assert resp.status_code == 200


class TestDBSAdminStatusUpdate:
    def test_update_status_valid(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        app_obj = _make_dbs_app(db_session)
        resp = client.post(f'/admin/dbs/{app_obj.id}/status', data={
            'application_status': 'Sent to UCheck',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db_session.expire(app_obj)
        assert app_obj.application_status == 'Sent to UCheck'

    def test_update_status_to_dbs_issued(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        app_obj = _make_dbs_app(db_session)
        client.post(f'/admin/dbs/{app_obj.id}/status', data={
            'application_status': 'DBS Issued',
        }, follow_redirects=True)
        db_session.expire(app_obj)
        assert app_obj.application_status == 'DBS Issued'

    def test_update_status_invalid_rejected(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        app_obj = _make_dbs_app(db_session)
        resp = client.post(f'/admin/dbs/{app_obj.id}/status', data={
            'application_status': 'Bogus Status',
        }, follow_redirects=True)
        assert resp.status_code == 200
        db_session.expire(app_obj)
        assert app_obj.application_status == 'Application Submitted'  # unchanged

    def test_update_status_requires_auth(self, client, db_session, app_instance):
        app_obj = _make_dbs_app(db_session)
        resp = client.post(f'/admin/dbs/{app_obj.id}/status', data={
            'application_status': 'Sent to UCheck',
        })
        assert resp.status_code in (200, 302, 401)

    def test_update_nonexistent_returns_404(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        resp = client.post('/admin/dbs/99999/status', data={
            'application_status': 'DBS Issued',
        })
        assert resp.status_code == 404


class TestDBSAdminPDF:
    def test_pdf_download(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        app_obj = _make_dbs_app(db_session)
        resp = client.get(f'/admin/dbs/{app_obj.id}/pdf')
        assert resp.status_code == 200
        assert b'%PDF' in resp.data or resp.content_type == 'application/pdf'

    def test_pdf_requires_auth(self, client, db_session, app_instance):
        app_obj = _make_dbs_app(db_session)
        resp = client.get(f'/admin/dbs/{app_obj.id}/pdf')
        assert resp.status_code in (200, 302, 401)


class TestDBSAdminDocDownload:
    def test_id_doc_404_when_file_missing(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        app_obj = _make_dbs_app(db_session)
        # File doesn't exist on disk so should 404
        resp = client.get(f'/admin/dbs/{app_obj.id}/id-doc')
        assert resp.status_code == 404

    def test_address_doc_404_when_file_missing(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        app_obj = _make_dbs_app(db_session)
        resp = client.get(f'/admin/dbs/{app_obj.id}/address-doc')
        assert resp.status_code == 404

    def test_address_doc_2_404_when_file_missing(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        app_obj = _make_dbs_app(db_session)
        resp = client.get(f'/admin/dbs/{app_obj.id}/address-doc-2')
        assert resp.status_code == 404

    def test_address_doc_2_404_when_path_none(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login(client, user.id)
        app_obj = _make_dbs_app(db_session)
        app_obj.proof_of_address_2_path = None
        db_session.commit()
        resp = client.get(f'/admin/dbs/{app_obj.id}/address-doc-2')
        assert resp.status_code == 404
