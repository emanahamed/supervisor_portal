import csv
import io

import pytest

from app import db
from models import Staff, StaffAttendance


def make_csv(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['MachineID','Date','CheckIn','CheckOut','Late'])
    for r in rows:
        w.writerow(r)
    return io.BytesIO(buf.getvalue().encode('utf-8'))


def test_import_creates_rows(app_instance, client, logged_in_superadmin_client, db_session):
    # create a staff with a known machine id
    s = Staff(name='Test Tutor', access_code='9999', whitechapel_machine_id='M123')
    db_session.add(s)
    db_session.commit()

    csvf = make_csv([
        ('M123','01/10/2023','09:00','17:00','0'),
    ])
    data = {
        'branch': '',
    }
    resp = client.post('/staff/attendance/import', data={'file': (csvf, 'att.csv'), 'branch': ''}, content_type='multipart/form-data')
    assert resp.status_code in (302, 200)
    # Check DB row created
    rows = StaffAttendance.query.filter_by(machine_id='M123').all()
    assert len(rows) == 1
    r = rows[0]
    assert r.hours_seconds == 8*3600
    assert r.staff_id == s.id
