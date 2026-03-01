"""
Comprehensive tests for:
  1. DBS application – two mandatory proof-of-address uploads
  2. Assessment dashboard – status update persistence fix

All database changes are rolled back via the db_session fixture.
"""

import io
import json
from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest

from models import (AdmissionAssessmentChange, AdmissionAssessmentScore,
                    AdmissionAssessmentSubmission, DBSApplication, Permission,
                    RolePermission, User, db)
from utils import BRANCH_CHOICES

# ═══════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════

def _fake_pdf():
    """Return a minimal fake PDF BytesIO."""
    return io.BytesIO(b'%PDF-1.4 test content')


def _five_year_address():
    """Return a single address covering 5+ years for DBS validation."""
    since = (date.today() - timedelta(days=6 * 365)).isoformat()
    return {
        'line1': '10 Downing Street',
        'line2': '',
        'postcode': 'SW1A 2AA',
        'since': since,
        'until': '',
        'current': True,
    }


def _dbs_form_data():
    """Return the baseline form payload for a valid DBS application."""
    return {
        'title': 'Mr',
        'first_name': 'John',
        'last_name': 'Doe',
        'other_names': 'no',
        'date_of_birth': '1990-06-15',
        'gender': 'Male',
        'place_of_birth_town': 'London',
        'place_of_birth_country': 'United Kingdom',
        'nationality': 'British',
        'email': 'john.doe@example.com',
        'phone': '07700900123',
        'has_ni': 'yes',
        'ni_number': 'AB 12 34 56 C',
        'has_driving_licence': 'no',
        'has_passport': 'no',
        'addr_line1_0': '10 Downing Street',
        'addr_line2_0': '',
        'addr_postcode_0': 'SW1A 2AA',
        'addr_since_0': (date.today() - timedelta(days=6 * 365)).isoformat(),
        'addr_current_0': 'on',
        'declaration_agreed': 'on',
        'signature': 'John Doe',
    }


def _ensure_superadmin(session):
    """Return or create a superadmin user."""
    u = User.query.filter_by(email='sa-test@test.local').first()
    if not u:
        u = User(
            name='SA Test',
            email='sa-test@test.local',
            password_hash='x',
            is_superadmin=True,
            is_approved=True,
            role='superadmin',
        )
        session.add(u)
        session.flush()
    return u


def _ensure_assessment_permission(session):
    """Ensure the manage_admission_assessments permission exists for admin role."""
    perm = Permission.query.filter_by(key='manage_admission_assessments').first()
    if not perm:
        perm = Permission(key='manage_admission_assessments', description='Manage admission assessments')
        session.add(perm)
    if not RolePermission.query.filter_by(role='admin', permission_key='manage_admission_assessments').first():
        session.add(RolePermission(role='admin', permission_key='manage_admission_assessments'))
    session.flush()


def _ensure_dbs_permission(session):
    """Ensure manage_dbs permission exists for admin role."""
    perm = Permission.query.filter_by(key='manage_dbs').first()
    if not perm:
        perm = Permission(key='manage_dbs', description='Manage DBS applications')
        session.add(perm)
    if not RolePermission.query.filter_by(role='admin', permission_key='manage_dbs').first():
        session.add(RolePermission(role='admin', permission_key='manage_dbs'))
    session.flush()


def _login_user(client, user_id):
    """Set session so Flask-Login treats us as the given user."""
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def _sample_submission(subjects=None):
    """Create an AdmissionAssessmentSubmission instance (not yet added to session)."""
    branch = BRANCH_CHOICES()[0] if BRANCH_CHOICES() else 'Whitechapel'
    sub = AdmissionAssessmentSubmission(
        student_name='Test Student',
        student_year_group='Year 6',
        parent_name='Parent One',
        parent_email='parent@example.com',
        parent_phone='07123456789',
        branch=branch,
        heard_about='Google',
        status='Application Submitted',
    )
    sub.set_subjects(subjects or ['Maths'])
    return sub


# ═══════════════════════════════════════════════════════════════════
#  PART A – DBS APPLICATION: TWO PROOF-OF-ADDRESS UPLOADS
# ═══════════════════════════════════════════════════════════════════

class TestDBSApplyFormRendering:
    """Test that the public DBS form page renders correctly."""

    def test_dbs_apply_get_returns_200(self, client):
        resp = client.get('/dbs/apply')
        assert resp.status_code == 200

    def test_form_contains_two_address_upload_fields(self, client):
        resp = client.get('/dbs/apply')
        html = resp.data.decode()
        assert 'name="proof_of_address"' in html
        assert 'name="proof_of_address_2"' in html
        assert 'name="proof_of_id"' in html

    def test_form_labels_indicate_two_proofs(self, client):
        resp = client.get('/dbs/apply')
        html = resp.data.decode()
        assert 'Proof of Address 1' in html
        assert 'Proof of Address 2' in html

    def test_form_mentions_two_separate_proofs(self, client):
        resp = client.get('/dbs/apply')
        html = resp.data.decode()
        assert 'two separate' in html.lower() or 'two separate' in html


class TestDBSApplyValidation:
    """Test server-side validation of the DBS form."""

    def test_missing_proof_of_address_1_returns_error(self, client, db_session):
        data = _dbs_form_data()
        # Only provide proof_of_address_2 and proof_of_id; omit proof_of_address
        data['proof_of_address_2'] = (_fake_pdf(), 'poa2.pdf')
        data['proof_of_id'] = (_fake_pdf(), 'id.pdf')
        resp = client.post('/dbs/apply', data=data, content_type='multipart/form-data')
        assert resp.status_code == 400
        html = resp.data.decode()
        assert 'Proof of address 1 is required' in html

    def test_missing_proof_of_address_2_returns_error(self, client, db_session):
        data = _dbs_form_data()
        data['proof_of_address'] = (_fake_pdf(), 'poa.pdf')
        data['proof_of_id'] = (_fake_pdf(), 'id.pdf')
        # proof_of_address_2 intentionally omitted
        resp = client.post('/dbs/apply', data=data, content_type='multipart/form-data')
        assert resp.status_code == 400
        html = resp.data.decode()
        assert 'Proof of address 2 is required' in html

    def test_missing_both_proofs_returns_both_errors(self, client, db_session):
        data = _dbs_form_data()
        data['proof_of_id'] = (_fake_pdf(), 'id.pdf')
        resp = client.post('/dbs/apply', data=data, content_type='multipart/form-data')
        assert resp.status_code == 400
        html = resp.data.decode()
        assert 'Proof of address 1 is required' in html
        assert 'Proof of address 2 is required' in html

    def test_missing_proof_of_id_returns_error(self, client, db_session):
        data = _dbs_form_data()
        data['proof_of_address'] = (_fake_pdf(), 'poa.pdf')
        data['proof_of_address_2'] = (_fake_pdf(), 'poa2.pdf')
        resp = client.post('/dbs/apply', data=data, content_type='multipart/form-data')
        assert resp.status_code == 400
        html = resp.data.decode()
        assert 'Proof of ID is required' in html

    def test_invalid_file_type_proof_of_address_1(self, client, db_session):
        data = _dbs_form_data()
        data['proof_of_address'] = (io.BytesIO(b'bad'), 'poa.exe')
        data['proof_of_address_2'] = (_fake_pdf(), 'poa2.pdf')
        data['proof_of_id'] = (_fake_pdf(), 'id.pdf')
        resp = client.post('/dbs/apply', data=data, content_type='multipart/form-data')
        assert resp.status_code == 400
        html = resp.data.decode()
        assert 'Proof of address 1 file type not allowed' in html

    def test_invalid_file_type_proof_of_address_2(self, client, db_session):
        data = _dbs_form_data()
        data['proof_of_address'] = (_fake_pdf(), 'poa.pdf')
        data['proof_of_address_2'] = (io.BytesIO(b'bad'), 'poa2.exe')
        data['proof_of_id'] = (_fake_pdf(), 'id.pdf')
        resp = client.post('/dbs/apply', data=data, content_type='multipart/form-data')
        assert resp.status_code == 400
        html = resp.data.decode()
        assert 'Proof of address 2 file type not allowed' in html


class TestDBSApplySuccess:
    """Test a fully valid DBS submission stores both address proofs."""

    def test_successful_submission_stores_both_proofs(self, client, db_session):
        data = _dbs_form_data()
        data['proof_of_address'] = (_fake_pdf(), 'poa.pdf')
        data['proof_of_address_2'] = (_fake_pdf(), 'poa2.pdf')
        data['proof_of_id'] = (_fake_pdf(), 'id.pdf')
        resp = client.post('/dbs/apply', data=data, content_type='multipart/form-data')
        # Should redirect to checkout
        assert resp.status_code in (301, 302, 303, 307, 308)

        app_obj = DBSApplication.query.order_by(DBSApplication.id.desc()).first()
        assert app_obj is not None
        assert app_obj.proof_of_address_path is not None
        assert app_obj.proof_of_address_2_path is not None
        assert app_obj.proof_of_id_path is not None
        # Paths are distinct
        assert app_obj.proof_of_address_path != app_obj.proof_of_address_2_path

    def test_successful_submission_sets_correct_personal_details(self, client, db_session):
        data = _dbs_form_data()
        data['proof_of_address'] = (_fake_pdf(), 'poa.pdf')
        data['proof_of_address_2'] = (_fake_pdf(), 'poa2.pdf')
        data['proof_of_id'] = (_fake_pdf(), 'id.pdf')
        client.post('/dbs/apply', data=data, content_type='multipart/form-data')

        app_obj = DBSApplication.query.order_by(DBSApplication.id.desc()).first()
        assert app_obj.first_name == 'John'
        assert app_obj.last_name == 'Doe'
        assert app_obj.email == 'john.doe@example.com'
        assert app_obj.application_status == 'Application Submitted'
        assert app_obj.payment_status == 'pending'

    def test_successful_submission_redirects_to_checkout(self, client, db_session):
        data = _dbs_form_data()
        data['proof_of_address'] = (_fake_pdf(), 'poa.pdf')
        data['proof_of_address_2'] = (_fake_pdf(), 'poa2.pdf')
        data['proof_of_id'] = (_fake_pdf(), 'id.pdf')
        resp = client.post('/dbs/apply', data=data, content_type='multipart/form-data')
        assert resp.status_code in (301, 302, 303, 307, 308)
        location = resp.headers.get('Location', '')
        assert '/dbs/checkout' in location

    def test_accepted_image_formats(self, client, db_session):
        """Test that PNG and JPG are accepted for proof of address uploads."""
        data = _dbs_form_data()
        data['proof_of_address'] = (io.BytesIO(b'\x89PNG\r\n\x1a\n'), 'poa.png')
        data['proof_of_address_2'] = (io.BytesIO(b'\xff\xd8\xff'), 'poa2.jpg')
        data['proof_of_id'] = (_fake_pdf(), 'id.pdf')
        resp = client.post('/dbs/apply', data=data, content_type='multipart/form-data')
        assert resp.status_code in (301, 302, 303, 307, 308)

        app_obj = DBSApplication.query.order_by(DBSApplication.id.desc()).first()
        assert app_obj.proof_of_address_path.endswith('.png')
        assert app_obj.proof_of_address_2_path.endswith('.jpg')


class TestDBSModelFields:
    """Test the DBSApplication model has the new field."""

    def test_model_has_proof_of_address_2_path(self, app_instance):
        with app_instance.app_context():
            columns = [c.name for c in DBSApplication.__table__.columns]
            assert 'proof_of_address_path' in columns
            assert 'proof_of_address_2_path' in columns
            assert 'proof_of_id_path' in columns


class TestDBSAdminDetail:
    """Test admin detail page shows both proof-of-address download links."""

    def test_admin_detail_shows_both_address_download_links(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login_user(client, user.id)

        app_obj = DBSApplication(
            application_id=f'DBS-T-{uuid4().hex[:8].upper()}',
            title='Ms',
            first_name='Jane',
            last_name='Smith',
            date_of_birth=date(1995, 3, 10),
            gender='Female',
            place_of_birth_town='Manchester',
            place_of_birth_country='UK',
            nationality='British',
            email='jane@example.com',
            phone='07700900456',
            addresses_json=json.dumps([_five_year_address()]),
            declaration_agreed=True,
            proof_of_address_path='uploads/dbs/poa_test.pdf',
            proof_of_address_2_path='uploads/dbs/poa2_test.pdf',
            proof_of_id_path='uploads/dbs/poi_test.pdf',
            payment_status='paid',
            application_status='Application Submitted',
        )
        db_session.add(app_obj)
        db_session.commit()

        resp = client.get(f'/admin/dbs/{app_obj.id}')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Address Proof 1' in html
        assert 'Address Proof 2' in html
        assert f'/admin/dbs/{app_obj.id}/address-doc' in html
        assert f'/admin/dbs/{app_obj.id}/address-doc-2' in html

    def test_admin_detail_hides_address2_link_when_not_present(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _login_user(client, user.id)

        app_obj = DBSApplication(
            application_id=f'DBS-T-{uuid4().hex[:8].upper()}',
            title='Mr',
            first_name='Bob',
            last_name='Brown',
            date_of_birth=date(1988, 1, 1),
            gender='Male',
            place_of_birth_town='Leeds',
            place_of_birth_country='UK',
            nationality='British',
            email='bob@example.com',
            phone='07700900789',
            addresses_json=json.dumps([_five_year_address()]),
            declaration_agreed=True,
            proof_of_address_path='uploads/dbs/poa_old.pdf',
            proof_of_address_2_path=None,
            proof_of_id_path='uploads/dbs/poi_old.pdf',
            payment_status='paid',
            application_status='Application Submitted',
        )
        db_session.add(app_obj)
        db_session.commit()

        resp = client.get(f'/admin/dbs/{app_obj.id}')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Address Proof 1' in html
        assert 'Address Proof 2' not in html


# ═══════════════════════════════════════════════════════════════════
#  PART B – ASSESSMENT STATUS UPDATE PERSISTENCE FIX
# ═══════════════════════════════════════════════════════════════════

class TestAssessmentStatusUpdateNoScores:
    """Test status-only updates (no score rows present) commit correctly."""

    def test_status_change_saves_when_no_subjects(self, client, db_session, app_instance):
        """Status change on a submission with no subjects should persist."""
        user = _ensure_superadmin(db_session)
        _ensure_assessment_permission(db_session)
        _login_user(client, user.id)

        sub = _sample_submission([])  # no subjects
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id

        resp = client.post(
            f'/admission-assessments/{sub_id}/scores',
            data={'status': 'Contacted'},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        refreshed = db_session.get(AdmissionAssessmentSubmission, sub_id)
        assert refreshed.status == 'Contacted'

    def test_status_change_records_audit_log(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _ensure_assessment_permission(db_session)
        _login_user(client, user.id)

        sub = _sample_submission([])
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id

        client.post(
            f'/admission-assessments/{sub_id}/scores',
            data={'status': 'Enrolled'},
            follow_redirects=True,
        )

        change = AdmissionAssessmentChange.query.filter_by(
            submission_id=sub_id, field='status'
        ).first()
        assert change is not None
        assert change.old_value == 'Application Submitted'
        assert change.new_value == 'Enrolled'


class TestAssessmentStatusUpdateWithScores:
    """Test status updates combined with score data."""

    def test_status_and_scores_persist_together(self, client, db_session, app_instance):
        """Changing status + providing scores should save both."""
        user = _ensure_superadmin(db_session)
        _ensure_assessment_permission(db_session)
        _login_user(client, user.id)

        sub = _sample_submission(['Maths', 'English'])
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id

        resp = client.post(
            f'/admission-assessments/{sub_id}/scores',
            data={
                'status': 'Contacted',
                'scores-0-subject': 'Maths',
                'scores-0-marks': '85',
                'scores-0-total': '100',
                'scores-0-percentage': '85.00',
                'scores-0-recommendation': '',
                'scores-1-subject': 'English',
                'scores-1-marks': '70',
                'scores-1-total': '100',
                'scores-1-percentage': '70.00',
                'scores-1-recommendation': '',
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        refreshed = db_session.get(AdmissionAssessmentSubmission, sub_id)
        assert refreshed.status == 'Contacted'

        maths_score = AdmissionAssessmentScore.query.filter_by(
            submission_id=sub_id, subject='Maths'
        ).first()
        assert maths_score is not None
        assert float(maths_score.percentage) == pytest.approx(85.0, abs=0.1)

        english_score = AdmissionAssessmentScore.query.filter_by(
            submission_id=sub_id, subject='English'
        ).first()
        assert english_score is not None
        assert float(english_score.percentage) == pytest.approx(70.0, abs=0.1)

    def test_status_persists_when_scores_unchanged(self, client, db_session, app_instance):
        """The key regression: status must persist even when scores are re-submitted unchanged.

        Before the fix, the autoflush during score queries caused is_modified() to return False,
        and the status change was silently dropped.
        """
        user = _ensure_superadmin(db_session)
        _ensure_assessment_permission(db_session)
        _login_user(client, user.id)

        sub = _sample_submission(['Maths'])
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id

        # First: save scores
        client.post(
            f'/admission-assessments/{sub_id}/scores',
            data={
                'status': 'Application Submitted',
                'scores-0-subject': 'Maths',
                'scores-0-marks': '90',
                'scores-0-total': '100',
                'scores-0-percentage': '90.00',
                'scores-0-recommendation': '',
            },
            follow_redirects=True,
        )

        # Verify scores exist
        score = AdmissionAssessmentScore.query.filter_by(
            submission_id=sub_id, subject='Maths'
        ).first()
        assert score is not None

        # Second: change only status (resubmit same scores)
        resp = client.post(
            f'/admission-assessments/{sub_id}/scores',
            data={
                'status': 'Enrolled',
                'scores-0-subject': 'Maths',
                'scores-0-marks': '90',
                'scores-0-total': '100',
                'scores-0-percentage': '90.00',
                'scores-0-recommendation': '',
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        refreshed = db_session.get(AdmissionAssessmentSubmission, sub_id)
        assert refreshed.status == 'Enrolled', (
            'Status was not persisted — the autoflush regression may have returned'
        )

    def test_same_status_resubmit_shows_no_changes(self, client, db_session, app_instance):
        """Submitting the same status should not create a change log entry."""
        user = _ensure_superadmin(db_session)
        _ensure_assessment_permission(db_session)
        _login_user(client, user.id)

        sub = _sample_submission([])
        sub.status = 'Contacted'
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id

        resp = client.post(
            f'/admission-assessments/{sub_id}/scores',
            data={'status': 'Contacted'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'No changes detected' in html

    def test_invalid_status_is_rejected(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _ensure_assessment_permission(db_session)
        _login_user(client, user.id)

        sub = _sample_submission([])
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id

        resp = client.post(
            f'/admission-assessments/{sub_id}/scores',
            data={'status': 'Bogus Status'},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # Status should remain unchanged
        refreshed = db_session.get(AdmissionAssessmentSubmission, sub_id)
        assert refreshed.status == 'Application Submitted'


class TestAssessmentStatusUpdateStandalone:
    """Test the dedicated status-only update endpoint."""

    def test_standalone_status_update(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _ensure_assessment_permission(db_session)
        _login_user(client, user.id)

        sub = _sample_submission(['Science'])
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id

        resp = client.post(
            f'/admission-assessments/{sub_id}/status',
            data={'status': 'Not Enrolled'},
            follow_redirects=True,
        )
        assert resp.status_code == 200

        refreshed = db_session.get(AdmissionAssessmentSubmission, sub_id)
        assert refreshed.status == 'Not Enrolled'

    def test_standalone_status_updates_timestamp(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _ensure_assessment_permission(db_session)
        _login_user(client, user.id)

        sub = _sample_submission(['Maths'])
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id
        old_ts = sub.status_updated_at

        client.post(
            f'/admission-assessments/{sub_id}/status',
            data={'status': 'Enrolled'},
            follow_redirects=True,
        )

        refreshed = db_session.get(AdmissionAssessmentSubmission, sub_id)
        assert refreshed.status_updated_at is not None
        if old_ts:
            assert refreshed.status_updated_at >= old_ts


class TestAssessmentDetailPageRendering:
    """Test the detail page renders the status dropdown and score form."""

    def test_detail_page_loads(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _ensure_assessment_permission(db_session)
        _login_user(client, user.id)

        sub = _sample_submission(['Maths', 'English'])
        db_session.add(sub)
        db_session.commit()

        resp = client.get(f'/admission-assessments/{sub.id}')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Status &amp; next step' in html
        assert 'name="status"' in html
        assert 'Maths' in html
        assert 'English' in html

    def test_detail_page_shows_current_status(self, client, db_session, app_instance):
        user = _ensure_superadmin(db_session)
        _ensure_assessment_permission(db_session)
        _login_user(client, user.id)

        sub = _sample_submission(['Maths'])
        sub.status = 'Contacted'
        db_session.add(sub)
        db_session.commit()

        resp = client.get(f'/admission-assessments/{sub.id}')
        html = resp.data.decode()
        assert 'Contacted' in html
