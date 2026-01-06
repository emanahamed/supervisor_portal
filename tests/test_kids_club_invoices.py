from datetime import date
from decimal import Decimal
from uuid import uuid4

from app import db
from models import Company, Invoice, User


def _make_company(name=None):
    prefix = f"KC{uuid4().hex[:4].upper()}-"
    unique_name = name or f'Kids Club {uuid4().hex[:6]}'
    if name is not None:
        unique_name = f"{name} {uuid4().hex[:4]}"
    company = Company(
        name=unique_name,
        invoice_prefix=prefix,
        next_invoice_seq=2,
        payment_footer='Thanks for choosing Kids Club!',
        address='123 High Street',
        email='office@example.com',
    )
    db.session.add(company)
    db.session.commit()
    return company


def _make_invoice(company: Company) -> Invoice:
    invoice = Invoice(
        invoice_no=f"{company.invoice_prefix}0001",
        company_id=company.id,
        parent_name='Parent Example',
        parent_phone='07123456789',
        parent_email='parent@example.com',
        parent_address='1 Billing Road',
        child_name='Child Example',
        period_start=date(2025, 9, 1),
        period_end=date(2025, 9, 30),
        invoice_date=date(2025, 10, 1),
        due_date=date(2025, 10, 14),
        sub_total=Decimal('55.00'),
        total=Decimal('55.00'),
        status='PAID',
        notes='October childcare sessions',
    )
    db.session.add(invoice)
    db.session.commit()
    return invoice


def _login_superadmin(client, user_id: int) -> None:
    with client.session_transaction() as sess:
        sess['user_id'] = str(user_id)
        sess['_user_id'] = str(user_id)


def test_invoice_duplicate_prefills_form_without_dates(client, db_session):
    company = _make_company()
    invoice = _make_invoice(company)
    user = User(
        name='Manager',
        email=f'manager-{uuid4().hex}@example.com',
        password_hash='x',
        is_superadmin=True,
        is_approved=True,
    )
    db.session.add(user)
    db.session.commit()
    _login_superadmin(client, user.id)

    resp = client.get(f'/invoices/{invoice.id}/duplicate')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'Duplicate Kids Club Invoice' in html
    assert 'Parent Example' in html
    # The cloned form should not pre-fill previous period or invoice dates.
    assert '2025-09-01' not in html
    assert '2025-10-01' not in html


def test_invoice_duplicate_creates_new_invoice(client, db_session):
    company = _make_company(name='Westfield Kids Club')
    invoice = _make_invoice(company)
    user = User(
        name='Supervisor',
        email=f'supervisor-{uuid4().hex}@example.com',
        password_hash='x',
        is_superadmin=True,
        is_approved=True,
    )
    db.session.add(user)
    db.session.commit()
    _login_superadmin(client, user.id)

    payload = {
        'company_id': str(company.id),
        'parent_name': 'Parent Example',
        'parent_phone': '07123456789',
        'parent_email': 'parent@example.com',
        'parent_address': '1 Billing Road',
        'child_name': 'Child Example',
        'period_start': '2025-11-01',
        'period_end': '2025-11-30',
        'invoice_date': '2025-12-01',
        'due_date': '2025-12-15',
        'payment_method': '',
        'sub_total': '55.00',
        'total': '65.00',
        'status': 'UNPAID',
        'notes': 'November childcare sessions',
    }
    resp = client.post(f'/invoices/{invoice.id}/duplicate', data=payload)
    assert resp.status_code == 302

    company_invoices = (Invoice.query
                        .filter(Invoice.company_id == company.id)
                        .order_by(Invoice.id.asc())
                        .all())
    assert len(company_invoices) == 2
    new_invoice = company_invoices[-1]
    assert new_invoice.id != invoice.id
    assert new_invoice.invoice_no == f"{company.invoice_prefix}0002"
    assert new_invoice.parent_name == invoice.parent_name
    assert new_invoice.invoice_date == date(2025, 12, 1)
    assert new_invoice.period_start == date(2025, 11, 1)
    assert new_invoice.total == Decimal('65.00')
    assert Company.query.get(company.id).next_invoice_seq == 3