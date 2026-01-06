from datetime import datetime, timedelta
from uuid import uuid4

from werkzeug.datastructures import MultiDict

from models import (AdmissionAssessmentNote, AdmissionAssessmentSubmission,
                    Permission, RolePermission, User)
from utils import BRANCH_CHOICES


def _sample_submission(subjects=None):
    submission = AdmissionAssessmentSubmission(
        student_name='Test Student',
        student_year_group='Year 6',
        parent_name='Parent One',
        parent_email='parent1@example.com',
        parent_phone='07123456789',
        branch=(BRANCH_CHOICES()[0] if BRANCH_CHOICES() else 'Whitechapel'),
        heard_about='Google',
    )
    submission.set_subjects(subjects or ['Maths'])
    return submission


def test_admission_assessment_form_get(client):
    resp = client.get('/admission-assessment')
    assert resp.status_code == 200
    assert b'Submit admission assessment request' in resp.data


def test_admission_assessment_form_get_public_alias(client):
    resp = client.get('/public/admission-assessment')
    assert resp.status_code == 200
    assert b'Submit admission assessment request' in resp.data


def test_admission_assessment_form_post_success(client, db_session):
    branch = BRANCH_CHOICES()[0] if BRANCH_CHOICES() else 'Whitechapel'
    payload = MultiDict([
        ('student_name', 'Learner One'),
        ('student_year_group', 'Year 7'),
        ('parent_name', 'Guardian'),
        ('parent_email', 'guardian@example.com'),
        ('parent_phone', '07000000000'),
        ('branch', branch),
        ('heard_about', 'Google'),
        ('subjects', 'Maths'),
        ('subjects', 'Science'),
        ('subject_other', 'Art'),
    ])
    resp = client.post('/admission-assessment', data=payload)
    assert resp.status_code in (301, 302, 303, 307, 308)
    assert resp.headers.get('Location') == 'https://admissions.exceltutors.org.uk/'
    created = AdmissionAssessmentSubmission.query.order_by(AdmissionAssessmentSubmission.id.desc()).first()
    assert created is not None
    assert created.parent_email == 'guardian@example.com'
    assert 'Maths' in created.subjects_list()
    assert 'Art' in created.subjects_list()
    assert created.status == 'Application Submitted'


def test_admission_assessment_admin_index_lists_submission(client, db_session):
    submission = _sample_submission(['English'])
    db_session.add(submission)
    db_session.commit()

    resp = client.get('/admission-assessments')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'Test Student' in html
    assert 'English' in html
    assert 'Daily submissions (last 30 days)' in html
    assert 'Weekly trend (12 weeks)' in html
    assert 'Conversion' in html
    assert 'assessment-daily' in html
    assert 'chart.umd.min.js' in html
    assert 'const data = {' in html


def test_admission_assessment_add_note(client, db_session):
    submission = _sample_submission(['Science'])
    db_session.add(submission)
    db_session.commit()

    resp = client.post(
        f'/admission-assessments/{submission.id}/notes',
        data={'note_body': 'Call parent regarding schedule'},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    note = (AdmissionAssessmentNote.query
            .filter_by(submission_id=submission.id)
            .order_by(AdmissionAssessmentNote.id.desc())
            .first())
    assert note is not None
    assert 'Call parent' in note.body


def test_admission_assessment_admin_index_sorts_latest_first(client, db_session):
    older = _sample_submission(['Maths'])
    older.student_name = 'Older Student'
    older.created_at = datetime.utcnow() - timedelta(days=1)
    newer = _sample_submission(['Science'])
    newer.student_name = 'Newer Student'
    newer.created_at = datetime.utcnow()
    db_session.add_all([older, newer])
    db_session.commit()

    resp = client.get('/admission-assessments')
    assert resp.status_code == 200
    content = resp.data.decode('utf-8')
    assert content.index('Newer Student') < content.index('Older Student')


def test_admission_assessment_admin_index_shows_delete_for_superadmin(client, db_session):
    submission = _sample_submission(['Maths'])
    db_session.add(submission)
    db_session.commit()

    superadmin = User.query.filter_by(is_superadmin=True).first()
    if not superadmin:
        superadmin = User(name='SA', email=f'sa-{uuid4().hex}@test.local', password_hash='x', is_superadmin=True, is_approved=True, role='superadmin')
        db_session.add(superadmin)
        db_session.commit()

    with client.session_transaction() as sess:
        sess['user_id'] = str(superadmin.id)
        sess['_user_id'] = str(superadmin.id)

    resp = client.get('/admission-assessments')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert f'data-assessment-delete="{submission.id}"' in html


def test_admission_assessment_admin_index_hides_delete_for_admin(client, db_session):
    submission = _sample_submission(['Science'])
    db_session.add(submission)

    perm = Permission.query.filter_by(key='manage_admission_assessments').first()
    if not perm:
        perm = Permission(key='manage_admission_assessments', description='Manage admission assessments')
        db_session.add(perm)

    if not RolePermission.query.filter_by(role='admin', permission_key='manage_admission_assessments').first():
        db_session.add(RolePermission(role='admin', permission_key='manage_admission_assessments'))

    unique_email = f"admin-list-{uuid4().hex}@test.local"
    admin_user = User(name='Admin User', email=unique_email, password_hash='x', is_superadmin=False, is_approved=True, role='admin')
    db_session.add(admin_user)
    db_session.commit()

    with client.session_transaction() as sess:
        sess['user_id'] = str(admin_user.id)
        sess['_user_id'] = str(admin_user.id)

    resp = client.get('/admission-assessments')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert f'data-assessment-delete="{submission.id}"' not in html


def test_admission_assessment_overview_shows_branch_and_source_breakdown(client, db_session):
    first = _sample_submission(['Maths'])
    first.branch = 'Whitechapel'
    first.heard_about = 'Google'
    second = _sample_submission(['English'])
    second.student_name = 'Student Two'
    second.branch = 'Stratford'
    second.heard_about = 'Facebook'
    db_session.add_all([first, second])
    db_session.commit()

    resp = client.get('/admission-assessments')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'Top branches' in html
    assert 'Whitechapel' in html
    assert 'Stratford' in html
    assert 'Top sources' in html
    assert 'Google' in html
    assert 'Facebook' in html


def test_admission_assessment_delete_requires_superadmin(client, db_session):
    submission = _sample_submission(['English'])
    db_session.add(submission)

    perm = Permission.query.filter_by(key='manage_admission_assessments').first()
    if not perm:
        perm = Permission(key='manage_admission_assessments', description='Manage admission assessments')
        db_session.add(perm)

    if not RolePermission.query.filter_by(role='admin', permission_key='manage_admission_assessments').first():
        db_session.add(RolePermission(role='admin', permission_key='manage_admission_assessments'))

    unique_email = f"admin-delete-{uuid4().hex}@test.local"
    user = User(name='Admin User', email=unique_email, password_hash='x', is_superadmin=False, is_approved=True, role='admin')
    db_session.add(user)
    db_session.commit()

    with client.session_transaction() as sess:
        sess['user_id'] = str(user.id)
        sess['_user_id'] = str(user.id)

    resp = client.post(f'/admission-assessments/{submission.id}/delete')
    assert resp.status_code == 403
    assert AdmissionAssessmentSubmission.query.get(submission.id) is not None


def test_admission_assessment_delete_superadmin_success(client, db_session):
    submission = _sample_submission(['Science'])
    db_session.add(submission)
    db_session.commit()

    superadmin = User.query.filter_by(is_superadmin=True).first()
    if not superadmin:
        superadmin = User(name='SA', email='sa@test.local', password_hash='x', is_superadmin=True, is_approved=True, role='superadmin')
        db_session.add(superadmin)
        db_session.commit()

    with client.session_transaction() as sess:
        sess['user_id'] = str(superadmin.id)
        sess['_user_id'] = str(superadmin.id)

    resp = client.post(f'/admission-assessments/{submission.id}/delete', follow_redirects=True)
    assert resp.status_code == 200
    assert AdmissionAssessmentSubmission.query.get(submission.id) is None
