import re
from datetime import datetime


def test_student_edit_creates_audit_log(client, db_session, app, login_superadmin):
    # Create a student via POST
    resp = client.post('/students/create', data={
        'student_id': 'S-AUD-1',
        'name': 'Audit Test',
        'type': 'Full',
        'year': '2025',
        'status': 'Active'
    }, follow_redirects=True)
    assert resp.status_code == 200
    # Fetch created student ID from list page html
    assert b'S-AUD-1' in resp.data

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
    # Should redirect to detail view containing new name
    assert b'Audit Test Updated' in resp2.data

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
