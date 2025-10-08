import pytest

from app import db
from models import Book, Permission, RolePermission, User


def ensure_permission(app_ctx, key, description=''):  # helper
    if not Permission.query.filter_by(key=key).first():
        db.session.add(Permission(key=key, description=description or key))
        db.session.commit()


def test_create_book_order(client, app_instance):
    with app_instance.app_context():
        # Seed a user & login (superadmin bypass or permission)
        u = User.query.filter_by(email='orderer@test.local').first()
        if not u:
            u = User(name='Order User', email='orderer@test.local', password_hash='x', is_superadmin=True, is_approved=True)
            db.session.add(u); db.session.commit()
        # Seed books
        if Book.query.count() < 2:
            db.session.add(Book(name='Order Book A', price=5.0, active=True))
            db.session.add(Book(name='Order Book B', price=7.0, active=True))
            db.session.commit()
        b1, b2 = Book.query.order_by(Book.id.asc()).limit(2).all()
    # Session login
    with client.session_transaction() as sess:
        sess['_user_id'] = str(u.id)
    # Create order via API
    payload = {
        'delivery_date': '2025-10-15',
        'items': [
            {'book_id': b1.id, 'quantity': 3},
            {'book_id': b2.id, 'quantity': 2},
        ]
    }
    resp = client.post('/tools/book-orders/create', json=payload)
    assert resp.status_code == 200, resp.data
    data = resp.get_json()
    assert data['success'] is True
    assert 'order_id' in data
    # Fetch detail page
    detail = client.get(f"/tools/book-orders/{data['order_id']}")
    assert detail.status_code == 200
    # List page
    listing = client.get('/tools/book-orders')
    assert listing.status_code == 200
