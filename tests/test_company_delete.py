import pytest
from flask import url_for

from app import app as flask_app
from models import Company, Invoice, db


@pytest.fixture()
def client(logged_in_superadmin_client):  # assumes existing fixture for superadmin login
    return logged_in_superadmin_client

@pytest.fixture()
def setup_companies(client):
    c1 = Company(name='Alpha Co')
    c2 = Company(name='Beta Co')
    db.session.add_all([c1,c2])
    db.session.commit()
    inv = Invoice(
        invoice_no='INV-TEST-0001',
        company_id=c1.id,
        created_by_id=None,
        invoice_date=db.func.date('now'),
        due_date=db.func.date('now'),
        parent_name='Parent',
        child_name='Child',
        period_start=db.func.date('now'),
        period_end=db.func.date('now'),
        sub_total=0,
        total=0,
        status='UNPAID'
    )
    db.session.add(inv)
    db.session.commit()
    return {'c1': c1, 'c2': c2, 'inv': inv}


def test_single_delete_reassign(client, setup_companies):
    c1 = setup_companies['c1']
    c2 = setup_companies['c2']
    # Reassign invoices from c1 to c2 then delete c1
    resp = client.post(f'/companies/{c1.id}/delete', data={
        'reassign_company_id': c2.id,
        'csrf_token': client.csrf_token
    }, follow_redirects=True)
    assert resp.status_code == 200
    # Confirm invoice moved
    inv = Invoice.query.filter_by(invoice_no='INV-TEST-0001').first()
    assert inv.company_id == c2.id
    # c1 deleted
    assert Company.query.get(c1.id) is None


def test_bulk_delete_mixed(client, setup_companies):
    c1 = setup_companies['c1']
    c2 = setup_companies['c2']
    # Create third empty company
    c3 = Company(name='Gamma Co')
    db.session.add(c3)
    db.session.commit()
    resp = client.post(f'/companies/bulk-delete?ids={c1.id},{c2.id},{c3.id}', data={
        'reassign_company_id': c2.id,
        'csrf_token': client.csrf_token
    }, follow_redirects=True)
    assert resp.status_code == 200
    # c1 should be deleted (invoices reassigned to c2), c3 deleted (empty), c2 exists
    assert Company.query.get(c1.id) is None
    assert Company.query.get(c3.id) is None
    assert Company.query.get(c2.id) is not None
    inv = Invoice.query.filter_by(invoice_no='INV-TEST-0001').first()
    assert inv.company_id == c2.id
