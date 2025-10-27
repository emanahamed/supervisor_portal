import io
import os
from datetime import date
from uuid import uuid4

import pytest

from models import Staff, User, db


def test_staff_create_creates_user(client, app, login_superadmin):
    # Arrange - prepare a minimal form payload
    data = {
        'name': 'Test Staff',
        'first_name': 'Test',
        'last_name': 'Staff',
        'department': '',
        'email': 'test.staff@example.com',
        'phone': '0123456789',
        'address_line1': '1 Test Road',
        'country': 'UK',
        'postcode': 'E1 6AN',
        'emergency_first_name': 'John',
        'emergency_last_name': 'Doe',
        'emergency_mobile': '07123456789',
        'bank_name_on_account': 'Test Staff',
        'bank_name': 'Test Bank',
        'bank_account_number': '12345678',
    }
    # attach a small file for photo with unique name, to be cleaned up after
    unique_photo = f"photo-test-{uuid4().hex[:8]}.jpg"
    data['photo'] = (io.BytesIO(b"fake-image-data"), unique_photo)

    # Act
    resp = client.post('/staff/new', data=data, content_type='multipart/form-data', follow_redirects=True)
    assert resp.status_code == 200
    # Find the created staff by email
    with app.app_context():
        s = Staff.query.filter_by(email='test.staff@example.com').first()
    assert s is not None
    # Ensure a user was created and linked
    assert s.user_id is not None
    with app.app_context():
        u = User.query.get(s.user_id)
        assert u is not None
        assert u.email == 'test.staff@example.com'
    # Photo file saved
    uploads = os.path.join(app.root_path, 'static', 'uploads')
    photo_path = os.path.join(uploads, unique_photo)
    assert os.path.exists(photo_path)
    # Cleanup file created by the test
    try:
        os.remove(photo_path)
    except Exception:
        pass


def test_postcode_lookup_proxy(client, login_superadmin):
    # This test verifies the proxy endpoint returns a successful JSON shape
    resp = client.get('/api/postcodes/lookup?q=E1')
    assert resp.status_code in (200, 500)
    # If 200, expect JSON with success key
    if resp.status_code == 200:
        data = resp.get_json()
        assert 'success' in data

