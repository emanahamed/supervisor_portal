from app import app, db
from models import Company, User


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)


def setup_module(module):
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(email='admin@example.com').first():
            u = User(name='Admin', email='admin@example.com', password_hash='x', is_superadmin=True)
            db.session.add(u)
        db.session.commit()


def test_company_json_endpoint():
    with app.app_context():
        admin = User.query.filter_by(email='admin@example.com').first()
        # Create or get a company (avoid uniqueness violation on re-run)
        c = Company.query.filter_by(name='Acme Ltd').first()
        if not c:
            c = Company(name='Acme Ltd', invoice_prefix='AC-', next_invoice_seq=42, payment_footer='Thanks', tagline='Quality', ofsted_reg_no='OF123', address='1 Road', phone='123', email='info@acme.test', website='https://acme.test')
            db.session.add(c)
            db.session.commit()
        cid = c.id
    with app.test_client() as client:
        with app.app_context():
            admin = User.query.filter_by(email='admin@example.com').first()
            admin_id = admin.id
        _login(client, admin_id)
        resp = client.get(f'/companies/{cid}/json')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['name'] == 'Acme Ltd'
        assert data['invoice_prefix'] == 'AC-'
        assert data['next_invoice_seq'] == 42
        assert data['payment_footer'] == 'Thanks'
        assert data['tagline'] == 'Quality'
        assert data['ofsted_reg_no'] == 'OF123'
        assert data['address'] == '1 Road'
        assert data['phone'] == '123'
        assert data['email'] == 'info@acme.test'
        assert data['website'] == 'https://acme.test'
