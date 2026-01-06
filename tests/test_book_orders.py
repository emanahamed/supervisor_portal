from uuid import uuid4

import pytest

from app import db
from models import Book, Permission, RolePermission, User


def ensure_permission(app_ctx, key, description=''):  # helper
    if not Permission.query.filter_by(key=key).first():
        db.session.add(Permission(key=key, description=description or key))
        db.session.commit()


def test_create_book_order(client, app_instance, db_session, monkeypatch):
    captured = {}

    def fake_send_email(to_email, subject, html, *, setting_name=None, attachments=None, cc=None):
        captured['to'] = to_email
        captured['subject'] = subject
        captured['cc'] = list(cc or [])

    monkeypatch.setattr('app.send_email', fake_send_email)
    with app_instance.app_context():
        # Seed a test-only user & books (rolled back after test)
        order_email = f"orderer-{uuid4().hex}@test.local"
        u = User(name='Order User', email=order_email, password_hash='x', is_superadmin=True, is_approved=True)
        db.session.add(u); db.session.flush()
        user_id = u.id
        b1 = Book(name='Order Book A (test)', price=5.0, active=True)
        b2 = Book(name='Order Book B (test)', price=7.0, active=True)
        db.session.add_all([b1, b2]); db.session.flush()
        book1_id, book2_id = b1.id, b2.id
        db.session.commit()
    # Session login
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
    # Create order via API
    payload = {
        'delivery_date': '2025-10-15',
        'items': [
                {'book_id': book1_id, 'quantity': 3},
                {'book_id': book2_id, 'quantity': 2},
        ]
    }
    resp = client.post('/tools/book-orders/create', json=payload)
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data['success'] is True
    assert 'order_id' in data
    assert captured['to'] == 'techsupport@exceltutors.org.uk'
    assert captured['cc'] == ['management@exceltutors.org.uk', 'enquiries@exceltutors.org.uk']
    # Fetch detail page
    detail = client.get(f"/tools/book-orders/{data['order_id']}")
    assert detail.status_code == 200
    # List page
    listing = client.get('/tools/book-orders')
    assert listing.status_code == 200
