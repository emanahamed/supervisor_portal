from decimal import Decimal

from email_utils import (build_admission_assessment_confirmation_email,
                         build_admission_assessment_scores_email)
from models import AdmissionAssessmentScore, AdmissionAssessmentSubmission


def _make_submission():
    sub = AdmissionAssessmentSubmission(
        student_name='Learner One',
        student_year_group='Year 6',
        parent_name='Guardian Jane',
        parent_email='guardian@example.com',
        parent_phone='07000000000',
        branch='Ilford',
        heard_about='Google',
    )
    sub.set_subjects(['Maths', 'English'])
    sub.subjects_other = 'Science'
    return sub


def test_admission_assessment_confirmation_email_uses_configured_link(app_instance):
    with app_instance.app_context():
        app_instance.config['PUBLIC_ADMISSIONS_URL'] = 'https://portal.example/admissions'
        submission = _make_submission()

        subject, html = build_admission_assessment_confirmation_email(submission)

    assert "We've received Learner One's admission assessment request" == subject
    assert 'Complete the admissions form' in html
    assert 'Maths' in html and 'Science' in html
    assert '07000000000' in html
    assert 'https://portal.example/admissions' in html


def test_admission_assessment_confirmation_email_uses_fallback_link(app_instance):
    with app_instance.app_context():
        for key in (
            'PUBLIC_ADMISSIONS_URL',
            'PUBLIC_ADMISSION_URL',
            'ADMISSIONS_PORTAL_URL',
            'ADMISSIONS_LANDING_URL',
        ):
            app_instance.config.pop(key, None)
        submission = _make_submission()

        _, html = build_admission_assessment_confirmation_email(submission)

    assert 'https://admissions.exceltutors.org.uk' in html


def test_admission_assessment_scores_email_formats_scores(app_instance):
    submission = _make_submission()
    maths = AdmissionAssessmentScore(subject='Maths')
    maths.marks_achieved = Decimal('45')
    maths.total_marks = Decimal('50')
    maths.percentage = Decimal('90')
    maths.recommendation = 'Place in Advanced Maths group.'

    english = AdmissionAssessmentScore(subject='English')
    english.marks_achieved = Decimal('37')
    english.total_marks = Decimal('50')
    english.percentage = Decimal('74.0')
    english.recommendation = 'Focus reading comprehension each week.'

    with app_instance.app_context():
        subject, html = build_admission_assessment_scores_email(submission, [maths, english])

    assert 'Admission assessment results for Learner One' == subject
    assert '90%' in html
    assert '74%' in html
    assert 'Advanced Maths group' in html
    assert 'Submit admissions form' in html
