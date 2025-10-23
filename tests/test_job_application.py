import io
import re

from utils import BRANCH_CHOICES


def test_jobs_apply_get(client):
    resp = client.get('/jobs/apply')
    assert resp.status_code == 200
    assert b'Job Application' in resp.data
    # Should include at least one branch choice
    branches = BRANCH_CHOICES()
    if branches:
        assert branches[0].encode() in resp.data


def test_jobs_apply_post_success(client, db_session):
    branches = BRANCH_CHOICES()
    branch = branches[0] if branches else 'Main'
    payload = {
        'first_name': 'Alice',
        'last_name': 'Applicant',
        'email': 'alice@example.com',
        'confirm_email': 'alice@example.com',
        'phone': '0123456789',
        'address_line1': '1 Main St',
        'city': 'London',
        'postcode': 'N1 1AA',
        'university': 'UCL',
    'study_year': '2',
        'course_name': 'Mathematics',
        'alevel1_subject': 'Maths',
        'alevel1_grade': 'A*',
        'alevel1_status': 'Achieved',
        'alevel2_subject': 'Physics',
        'alevel2_grade': 'A',
        'alevel2_status': 'Achieved',
        'alevel3_subject': 'Chemistry',
        'alevel3_grade': 'A',
        'alevel3_status': 'Achieved',
        'branches': branch,
        'gcse_maths_grade': '9',
        'gcse_maths_status': 'Achieved',
        'gcse_english_grade': '9',
        'gcse_english_status': 'Achieved',
        'gcse_science_grade': '9',
        'gcse_science_status': 'Achieved',
        'tutoring_experience_yes': 'on',
        'uk_work_eligible_yes': 'on',
        'heard_about': 'Google',
        'subjects': 'Maths',
    }
    # Attach a small fake PDF as CV
    data = dict(payload)
    data['cv_file'] = (io.BytesIO(b'%PDF-1.4 test'), 'cv.pdf')
    resp = client.post('/jobs/apply', data=data, follow_redirects=True, content_type='multipart/form-data')
    assert resp.status_code == 200
    assert b'Thank you. Your application has been submitted.' in resp.data



def test_jobs_apply_post_email_mismatch(client):
    branches = BRANCH_CHOICES()
    branch = branches[0] if branches else 'Main'
    payload = {
        'first_name': 'Bob',
        'last_name': 'Candidate',
        'email': 'bob@example.com',
        'confirm_email': 'mismatch@example.com',
        'phone': '0123456789',
        'address_line1': '2 Main St',
        'city': 'London',
        'postcode': 'N1 1AA',
        'branches': branch,
        'university': 'UCL',
    'study_year': '3',
        'course_name': 'Economics',
        'alevel1_subject': 'Maths',
        'alevel1_grade': 'A*',
        'alevel1_status': 'Achieved',
        'alevel2_subject': 'Economics',
        'alevel2_grade': 'A',
        'alevel2_status': 'Achieved',
        'alevel3_subject': 'English',
        'alevel3_grade': 'A',
        'alevel3_status': 'Achieved',
        'gcse_maths_grade': '9',
        'gcse_maths_status': 'Achieved',
        'gcse_english_grade': '9',
        'gcse_english_status': 'Achieved',
        'gcse_science_grade': '9',
        'gcse_science_status': 'Achieved',
        'tutoring_experience_yes': 'on',
        'uk_work_eligible_yes': 'on',
        'heard_about': 'Google',
        'subjects': 'Maths',
    }
    resp = client.post('/jobs/apply', data=payload)
    assert resp.status_code == 200
    assert b'must match' in resp.data
