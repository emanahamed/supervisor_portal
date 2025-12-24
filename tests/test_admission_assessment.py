from werkzeug.datastructures import MultiDict

from models import AdmissionAssessmentNote, AdmissionAssessmentSubmission
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
    resp = client.post('/admission-assessment', data=payload, follow_redirects=True)
    assert resp.status_code == 200
    assert b'Thank you. Your details have been submitted and our admissions team will be in touch shortly.' in resp.data
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
    assert b'Test Student' in resp.data
    assert b'English' in resp.data


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
    note = AdmissionAssessmentNote.query.filter_by(submission_id=submission.id).first()
    assert note is not None
    assert 'Call parent' in note.body
