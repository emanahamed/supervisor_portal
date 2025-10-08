import re
from datetime import datetime


def test_student_edit_creates_audit_log(client, db_session, app, login_superadmin):
    # Create a student directly (bypassing form rendering complexity)
    from models import Student
    with app.app_context():
        existing = Student.query.filter_by(student_id='S-AUD-1').first()
        if not existing:
            s = Student(student_id='S-AUD-1', name='Audit Test', type='Full', year='2025', status='Active')
            db_session.add(s)
            db_session.commit()

    # Find the student id via direct query (since models are imported in app context)
    from models import Student, StudentChange
    student = Student.query.filter_by(student_id='S-AUD-1').first()
    assert student is not None

    # Edit student via POST (students_edit expects POST with form fields)
    resp2 = client.post(f'/students/{student.id}/edit', data={
        'student_id': 'S-AUD-1',
        'name': 'Audit Test Updated',
        'type': 'Full',
        'year': '2025',
        'email': '',
        'phone': '',
        'address': '',
        'academic': '',
        'status': 'Inactive'
    }, follow_redirects=True)
    assert resp2.status_code == 200

    # Audit entries
    changes = StudentChange.query.filter_by(student_id=student.id).all()
    # Expect at least two fields changed: name and status
    changed_fields = {c.field for c in changes}
    assert 'name' in changed_fields
    assert 'status' in changed_fields

    name_change = next(c for c in changes if c.field == 'name')
    assert name_change.old_value == 'Audit Test'
    assert name_change.new_value == 'Audit Test Updated'

    status_change = next(c for c in changes if c.field == 'status')
    assert status_change.old_value == 'Active'
    assert status_change.new_value == 'Inactive'
