"""
DBS Application Routes – Public form + Admin management (Feb 2026)

Public:
  /dbs/apply              – Multi-step enhanced DBS application form
  /dbs/checkout           – Stripe checkout for DBS fee
  /dbs/payment-success    – Post-payment success handler
  /dbs/payment-cancelled  – Payment cancelled page

Admin (login required):
  /admin/dbs              – Application list with filters
  /admin/dbs/<id>         – Application detail view
  /admin/dbs/<id>/status  – Update application status
  /admin/dbs/<id>/pdf     – Download application as PDF
  /admin/dbs/<id>/id-doc  – Download uploaded proof of ID
  /admin/dbs/<id>/address-doc – Download uploaded proof of address
  /admin/dbs/export       – XLSX export with field selection
  /admin/dbs/settings     – Configure DBS fee
"""

import io
import json
import os
import re
from datetime import date, datetime, timedelta
from uuid import uuid4

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from flask_login import current_user, login_required
from flask_wtf.csrf import generate_csrf
from werkzeug.utils import secure_filename

from email_utils import send_email
from models import DBSApplication, Permission, Setting, db
from utils import get_setting, set_setting

dbs_bp = Blueprint('dbs', __name__)

# ── Helpers ──

DBS_UPLOAD_EXTS = {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp'}
DBS_STATUS_CHOICES = ['Application Submitted', 'Sent to UCheck', 'DBS Issued']


def _generate_application_id():
    """Generate a unique DBS application ID like DBS-20260210-XXXX."""
    today = date.today().strftime('%Y%m%d')
    suffix = uuid4().hex[:4].upper()
    return f"DBS-{today}-{suffix}"


def _get_dbs_fee():
    """Get the configurable DBS fee (default £72.00)."""
    try:
        val = get_setting('dbs_fee')
        if val:
            return float(val)
    except Exception:
        pass
    return 72.00


def _ensure_upload_dir():
    """Ensure DBS upload directory exists."""
    upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'dbs')
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


def _save_upload(file_storage, prefix):
    """Save uploaded file and return relative path from static/."""
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in DBS_UPLOAD_EXTS:
        return None
    upload_dir = _ensure_upload_dir()
    filename = f"{prefix}_{uuid4().hex[:8]}{ext}"
    filepath = os.path.join(upload_dir, filename)
    file_storage.save(filepath)
    return f"uploads/dbs/{filename}"


def _validate_five_year_coverage(addresses):
    """Check that addresses cover at least 5 years from today."""
    if not addresses:
        return False
    today = date.today()
    five_years_ago = today - timedelta(days=5 * 365)
    # Find earliest 'since' date
    earliest = today
    for addr in addresses:
        try:
            since = datetime.strptime(addr.get('since', ''), '%Y-%m-%d').date()
            if since < earliest:
                earliest = since
        except (ValueError, TypeError):
            continue
    return earliest <= five_years_ago


def _build_application_pdf_bytes(app_obj):
    """Render DBS application as PDF and return bytes."""
    from xhtml2pdf import pisa
    html = render_template('dbs/application_pdf.html', app=app_obj)
    buf = io.BytesIO()
    pisa.CreatePDF(io.StringIO(html), dest=buf)
    buf.seek(0)
    return buf.read()


def _build_invoice_html(app_obj, fee):
    """Build HTML invoice for DBS payment."""
    return render_template('dbs/invoice_email.html', app=app_obj, fee=fee)


def _build_application_email_html(app_obj):
    """Build HTML email for application submitted."""
    return render_template('dbs/application_email.html', app=app_obj)


# ── Public Routes ──

@dbs_bp.route('/dbs/apply', methods=['GET', 'POST'])
def dbs_apply():
    """Public DBS application form."""
    fee = _get_dbs_fee()
    if request.method == 'POST':
        errors = []
        # Personal details
        title = request.form.get('title', '').strip()
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        if not title or not first_name or not last_name:
            errors.append('Title, first name and last name are required.')

        other_names_flag = request.form.get('other_names') == 'yes'
        other_names_data = []
        if other_names_flag:
            idx = 0
            while True:
                on_title = request.form.get(f'on_title_{idx}', '').strip()
                on_first = request.form.get(f'on_first_name_{idx}', '').strip()
                on_last = request.form.get(f'on_last_name_{idx}', '').strip()
                on_from = request.form.get(f'on_date_from_{idx}', '').strip()
                on_to = request.form.get(f'on_date_to_{idx}', '').strip()
                if on_first or on_last:
                    other_names_data.append({
                        'title': on_title, 'first_name': on_first,
                        'last_name': on_last, 'date_from': on_from, 'date_to': on_to
                    })
                    idx += 1
                else:
                    break

        dob_str = request.form.get('date_of_birth', '').strip()
        gender = request.form.get('gender', '').strip()
        birth_town = request.form.get('place_of_birth_town', '').strip()
        birth_country = request.form.get('place_of_birth_country', '').strip()
        nationality = request.form.get('nationality', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()

        if not dob_str:
            errors.append('Date of birth is required.')
        if not gender:
            errors.append('Gender is required.')
        if not birth_town or not birth_country:
            errors.append('Place of birth (town and country) are required.')
        if not nationality:
            errors.append('Nationality is required.')
        if not email:
            errors.append('Email is required.')
        if not phone:
            errors.append('Contact telephone number is required.')

        # NI
        has_ni = request.form.get('has_ni') == 'yes'
        ni_number = request.form.get('ni_number', '').strip() if has_ni else None

        # Driving licence
        has_licence = request.form.get('has_driving_licence') == 'yes'
        licence_no = request.form.get('driving_licence_number', '').strip() if has_licence else None

        # Passport
        has_passport = request.form.get('has_passport') == 'yes'
        passport_number = request.form.get('passport_number', '').strip() if has_passport else None
        passport_issue = request.form.get('passport_issue_date', '').strip() if has_passport else None
        passport_expiry = request.form.get('passport_expiry_date', '').strip() if has_passport else None
        passport_country = request.form.get('passport_country_of_issue', '').strip() if has_passport else None

        # Addresses
        addresses = []
        addr_idx = 0
        while True:
            line1 = request.form.get(f'addr_line1_{addr_idx}', '').strip()
            line2 = request.form.get(f'addr_line2_{addr_idx}', '').strip()
            postcode = request.form.get(f'addr_postcode_{addr_idx}', '').strip()
            since = request.form.get(f'addr_since_{addr_idx}', '').strip()
            until = request.form.get(f'addr_until_{addr_idx}', '').strip()
            current = request.form.get(f'addr_current_{addr_idx}') == 'on'
            if line1 or postcode:
                if current:
                    until = ''
                addresses.append({
                    'line1': line1, 'line2': line2, 'postcode': postcode,
                    'since': since, 'until': until, 'current': current
                })
                addr_idx += 1
            else:
                break

        if not addresses:
            errors.append('At least one address is required.')
        elif not _validate_five_year_coverage(addresses):
            errors.append('Your address history must cover the last 5 years. Please add more addresses.')

        # Signature & declaration
        signature = request.form.get('signature', '').strip()
        declaration = request.form.get('declaration_agreed') == 'on'
        if not declaration:
            errors.append('You must agree to the declaration.')

        # File uploads
        proof_of_address_file = request.files.get('proof_of_address')
        proof_of_address_2_file = request.files.get('proof_of_address_2')
        proof_of_id_file = request.files.get('proof_of_id')
        if not proof_of_address_file or not proof_of_address_file.filename:
            errors.append('Proof of address 1 is required.')
        if not proof_of_address_2_file or not proof_of_address_2_file.filename:
            errors.append('Proof of address 2 is required.')
        if not proof_of_id_file or not proof_of_id_file.filename:
            errors.append('Proof of ID is required.')

        if errors:
            return render_template('dbs/apply.html', errors=errors, fee=fee,
                                   form_data=request.form), 400

        # Parse dates
        try:
            dob = datetime.strptime(dob_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return render_template('dbs/apply.html', errors=['Invalid date of birth format.'], fee=fee,
                                   form_data=request.form), 400

        passport_issue_date = None
        passport_expiry_date = None
        if has_passport:
            try:
                if passport_issue:
                    passport_issue_date = datetime.strptime(passport_issue, '%Y-%m-%d').date()
                if passport_expiry:
                    passport_expiry_date = datetime.strptime(passport_expiry, '%Y-%m-%d').date()
            except (ValueError, TypeError):
                return render_template('dbs/apply.html', errors=['Invalid passport date format.'], fee=fee,
                                       form_data=request.form), 400

        # Save uploads
        poa_path = _save_upload(proof_of_address_file, 'poa')
        poa2_path = _save_upload(proof_of_address_2_file, 'poa2')
        poi_path = _save_upload(proof_of_id_file, 'poi')

        if not poa_path:
            errors.append('Proof of address 1 file type not allowed. Accepted: PDF, PNG, JPG.')
        if not poa2_path:
            errors.append('Proof of address 2 file type not allowed. Accepted: PDF, PNG, JPG.')
        if not poi_path:
            errors.append('Proof of ID file type not allowed. Accepted: PDF, PNG, JPG.')
        if errors:
            return render_template('dbs/apply.html', errors=errors, fee=fee,
                                   form_data=request.form), 400

        # Create application
        app_obj = DBSApplication(
            application_id=_generate_application_id(),
            title=title,
            first_name=first_name,
            last_name=last_name,
            other_names=other_names_flag,
            other_names_json=json.dumps(other_names_data) if other_names_data else None,
            date_of_birth=dob,
            gender=gender,
            place_of_birth_town=birth_town,
            place_of_birth_country=birth_country,
            nationality=nationality,
            email=email,
            phone=phone,
            has_ni=has_ni,
            ni_number=ni_number,
            has_driving_licence=has_licence,
            driving_licence_number=licence_no,
            has_passport=has_passport,
            passport_number=passport_number,
            passport_issue_date=passport_issue_date,
            passport_expiry_date=passport_expiry_date,
            passport_country_of_issue=passport_country,
            addresses_json=json.dumps(addresses),
            signature_data=signature,
            declaration_agreed=declaration,
            proof_of_address_path=poa_path,
            proof_of_address_2_path=poa2_path,
            proof_of_id_path=poi_path,
            payment_status='pending',
            application_status='Application Submitted',
        )
        db.session.add(app_obj)
        db.session.commit()

        # Store application id in session for checkout
        session['dbs_application_id'] = app_obj.id
        return redirect(url_for('dbs.dbs_checkout'))

    return render_template('dbs/apply.html', errors=[], fee=fee, form_data={})


@dbs_bp.route('/dbs/checkout', methods=['GET', 'POST'])
def dbs_checkout():
    """Create Stripe checkout session for DBS fee."""
    app_id = session.get('dbs_application_id')
    if not app_id:
        flash('Please complete the application form first.', 'warning')
        return redirect(url_for('dbs.dbs_apply'))

    app_obj = DBSApplication.query.get(app_id)
    if not app_obj:
        flash('Application not found.', 'danger')
        return redirect(url_for('dbs.dbs_apply'))

    if app_obj.payment_status == 'paid':
        flash('Payment already completed.', 'info')
        return redirect(url_for('dbs.dbs_payment_success'))

    fee = _get_dbs_fee()

    if request.method == 'POST':
        try:
            import stripe

            from app import STRIPE_SECRET_KEY
            stripe.api_key = STRIPE_SECRET_KEY

            checkout_session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=[{
                    'price_data': {
                        'currency': 'gbp',
                        'product_data': {
                            'name': 'Enhanced DBS Application',
                            'description': f'DBS Application {app_obj.application_id}',
                        },
                        'unit_amount': int(fee * 100),
                    },
                    'quantity': 1,
                }],
                mode='payment',
                success_url=url_for('dbs.dbs_payment_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
                cancel_url=url_for('dbs.dbs_payment_cancelled', _external=True),
                customer_email=app_obj.email,
                metadata={
                    'dbs_application_id': str(app_obj.id),
                    'application_id': app_obj.application_id,
                },
            )

            app_obj.stripe_checkout_session_id = checkout_session.id
            db.session.commit()

            return jsonify({'checkout_url': checkout_session.url})
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    from app import STRIPE_PUBLIC_KEY
    return render_template('dbs/checkout.html', app=app_obj, fee=fee,
                           stripe_public_key=STRIPE_PUBLIC_KEY)


@dbs_bp.route('/dbs/payment-success')
def dbs_payment_success():
    """Handle successful Stripe payment."""
    session_id = request.args.get('session_id')
    app_id = session.get('dbs_application_id')

    app_obj = None
    if session_id:
        app_obj = DBSApplication.query.filter_by(stripe_checkout_session_id=session_id).first()
    if not app_obj and app_id:
        app_obj = DBSApplication.query.get(app_id)

    if not app_obj:
        flash('Application not found.', 'danger')
        return redirect(url_for('dbs.dbs_apply'))

    # Verify payment with Stripe
    if session_id and app_obj.payment_status != 'paid':
        try:
            import stripe

            from app import STRIPE_SECRET_KEY
            stripe.api_key = STRIPE_SECRET_KEY
            cs = stripe.checkout.Session.retrieve(session_id)
            if cs.payment_status == 'paid':
                app_obj.payment_status = 'paid'
                app_obj.stripe_payment_intent_id = cs.payment_intent
                db.session.commit()

                # Send emails
                fee = _get_dbs_fee()
                _send_dbs_emails(app_obj, fee)
        except Exception as e:
            print(f"[DBS] Stripe verification error: {e}")

    # Clear session
    session.pop('dbs_application_id', None)

    return render_template('dbs/payment_success.html', app=app_obj)


@dbs_bp.route('/dbs/payment-cancelled')
def dbs_payment_cancelled():
    """Payment was cancelled."""
    return render_template('dbs/payment_cancelled.html')


def _send_dbs_emails(app_obj, fee):
    """Send invoice email + application PDF email after payment."""
    try:
        # 1. Invoice / proof of payment email
        invoice_html = _build_invoice_html(app_obj, fee)
        send_email(
            to_email=app_obj.email,
            subject=f"DBS Application Payment Confirmation - {app_obj.application_id}",
            html=invoice_html,
        )
    except Exception as e:
        print(f"[DBS] Invoice email error: {e}")

    try:
        # 2. Application form as PDF attachment
        pdf_bytes = _build_application_pdf_bytes(app_obj)
        app_email_html = _build_application_email_html(app_obj)
        send_email(
            to_email=app_obj.email,
            subject=f"DBS Application Form - {app_obj.application_id}",
            html=app_email_html,
            attachments=[
                (pdf_bytes, 'application', 'pdf', f'DBS_Application_{app_obj.application_id}.pdf')
            ],
        )
    except Exception as e:
        print(f"[DBS] Application PDF email error: {e}")


# ── Admin Routes ──

@dbs_bp.route('/admin/dbs')
@login_required
def admin_dbs_list():
    """Admin: list all DBS applications."""
    if not (current_user.is_superadmin or _can('manage_dbs')):
        abort(403)

    search = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '')
    payment_filter = request.args.get('payment', '')
    page = request.args.get('page', 1, type=int)
    per_page = 25

    query = DBSApplication.query

    if search:
        like = f'%{search}%'
        query = query.filter(
            db.or_(
                DBSApplication.first_name.ilike(like),
                DBSApplication.last_name.ilike(like),
                DBSApplication.application_id.ilike(like),
                DBSApplication.email.ilike(like),
            )
        )
    if status_filter:
        query = query.filter_by(application_status=status_filter)
    if payment_filter:
        query = query.filter_by(payment_status=payment_filter)

    query = query.order_by(DBSApplication.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return render_template('dbs/admin_list.html',
                           applications=pagination.items,
                           pagination=pagination,
                           search=search,
                           status_filter=status_filter,
                           payment_filter=payment_filter,
                           status_choices=DBS_STATUS_CHOICES)


@dbs_bp.route('/admin/dbs/<int:app_id>')
@login_required
def admin_dbs_detail(app_id):
    """Admin: view DBS application detail."""
    if not (current_user.is_superadmin or _can('manage_dbs')):
        abort(403)
    app_obj = DBSApplication.query.get_or_404(app_id)
    return render_template('dbs/admin_detail.html', app=app_obj,
                           status_choices=DBS_STATUS_CHOICES)


@dbs_bp.route('/admin/dbs/<int:app_id>/status', methods=['POST'])
@login_required
def admin_dbs_update_status(app_id):
    """Admin: update application status."""
    if not (current_user.is_superadmin or _can('manage_dbs')):
        abort(403)
    app_obj = DBSApplication.query.get_or_404(app_id)
    new_status = request.form.get('application_status', '').strip()
    if new_status in DBS_STATUS_CHOICES:
        app_obj.application_status = new_status
        db.session.commit()
        flash(f'Status updated to "{new_status}".', 'success')
    else:
        flash('Invalid status.', 'danger')
    return redirect(url_for('dbs.admin_dbs_detail', app_id=app_id))


@dbs_bp.route('/admin/dbs/<int:app_id>/pdf')
@login_required
def admin_dbs_download_pdf(app_id):
    """Admin: download application as PDF."""
    if not (current_user.is_superadmin or _can('manage_dbs')):
        abort(403)
    app_obj = DBSApplication.query.get_or_404(app_id)
    pdf_bytes = _build_application_pdf_bytes(app_obj)
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'DBS_Application_{app_obj.application_id}.pdf'
    )


@dbs_bp.route('/admin/dbs/<int:app_id>/id-doc')
@login_required
def admin_dbs_download_id(app_id):
    """Admin: download proof of ID."""
    if not (current_user.is_superadmin or _can('manage_dbs')):
        abort(403)
    app_obj = DBSApplication.query.get_or_404(app_id)
    if not app_obj.proof_of_id_path:
        abort(404)
    full_path = os.path.join(current_app.root_path, 'static', app_obj.proof_of_id_path)
    if not os.path.isfile(full_path):
        abort(404)
    return send_file(full_path, as_attachment=True,
                     download_name=f'ID_{app_obj.application_id}{os.path.splitext(full_path)[1]}')


@dbs_bp.route('/admin/dbs/<int:app_id>/address-doc')
@login_required
def admin_dbs_download_address(app_id):
    """Admin: download proof of address."""
    if not (current_user.is_superadmin or _can('manage_dbs')):
        abort(403)
    app_obj = DBSApplication.query.get_or_404(app_id)
    if not app_obj.proof_of_address_path:
        abort(404)
    full_path = os.path.join(current_app.root_path, 'static', app_obj.proof_of_address_path)
    if not os.path.isfile(full_path):
        abort(404)
    return send_file(full_path, as_attachment=True,
                     download_name=f'Address1_{app_obj.application_id}{os.path.splitext(full_path)[1]}')


@dbs_bp.route('/admin/dbs/<int:app_id>/address-doc-2')
@login_required
def admin_dbs_download_address_2(app_id):
    """Admin: download second proof of address."""
    if not (current_user.is_superadmin or _can('manage_dbs')):
        abort(403)
    app_obj = DBSApplication.query.get_or_404(app_id)
    if not app_obj.proof_of_address_2_path:
        abort(404)
    full_path = os.path.join(current_app.root_path, 'static', app_obj.proof_of_address_2_path)
    if not os.path.isfile(full_path):
        abort(404)
    return send_file(full_path, as_attachment=True,
                     download_name=f'Address2_{app_obj.application_id}{os.path.splitext(full_path)[1]}')


@dbs_bp.route('/admin/dbs/export', methods=['GET', 'POST'])
@login_required
def admin_dbs_export():
    """Admin: export DBS applications to XLSX with selectable fields."""
    if not (current_user.is_superadmin or _can('manage_dbs')):
        abort(403)

    EXPORTABLE_FIELDS = {
        'application_id': 'Application ID',
        'title': 'Title',
        'first_name': 'First Name',
        'last_name': 'Last Name',
        'full_name': 'Full Name',
        'date_of_birth': 'Date of Birth',
        'gender': 'Gender',
        'nationality': 'Nationality',
        'place_of_birth_town': 'Place of Birth (Town)',
        'place_of_birth_country': 'Place of Birth (Country)',
        'email': 'Email',
        'phone': 'Phone',
        'has_ni': 'Has NI Number',
        'ni_number': 'NI Number',
        'has_driving_licence': 'Has Driving Licence',
        'driving_licence_number': 'Driving Licence No.',
        'has_passport': 'Has Passport',
        'passport_number': 'Passport Number',
        'passport_issue_date': 'Passport Issue Date',
        'passport_expiry_date': 'Passport Expiry Date',
        'passport_country_of_issue': 'Passport Country of Issue',
        'payment_status': 'Payment Status',
        'application_status': 'Application Status',
        'created_at': 'Submitted Date',
    }

    if request.method == 'GET':
        return render_template('dbs/admin_export.html', exportable_fields=EXPORTABLE_FIELDS)

    # POST: generate XLSX
    selected = request.form.getlist('fields')
    if not selected:
        flash('Please select at least one field.', 'warning')
        return redirect(url_for('dbs.admin_dbs_export'))

    import pandas as pd

    applications = DBSApplication.query.order_by(DBSApplication.created_at.desc()).all()
    rows = []
    for a in applications:
        row = {}
        for field in selected:
            if field == 'full_name':
                row[EXPORTABLE_FIELDS[field]] = a.full_name()
            elif field in ('date_of_birth', 'passport_issue_date', 'passport_expiry_date'):
                val = getattr(a, field, None)
                row[EXPORTABLE_FIELDS[field]] = val.strftime('%d/%m/%Y') if val else ''
            elif field == 'created_at':
                val = getattr(a, field, None)
                row[EXPORTABLE_FIELDS[field]] = val.strftime('%d/%m/%Y %H:%M') if val else ''
            elif field in ('has_ni', 'has_driving_licence', 'has_passport'):
                row[EXPORTABLE_FIELDS[field]] = 'Yes' if getattr(a, field, False) else 'No'
            else:
                row[EXPORTABLE_FIELDS[field]] = getattr(a, field, '') or ''
        rows.append(row)

    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine='openpyxl')
    buf.seek(0)

    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'DBS_Applications_{date.today().isoformat()}.xlsx'
    )


@dbs_bp.route('/admin/dbs/settings', methods=['GET', 'POST'])
@login_required
def admin_dbs_settings():
    """Admin: configure DBS fee."""
    if not current_user.is_superadmin:
        abort(403)
    if request.method == 'POST':
        new_fee = request.form.get('dbs_fee', '72').strip()
        try:
            fee_val = float(new_fee)
            if fee_val < 0:
                raise ValueError
            set_setting('dbs_fee', str(fee_val))
            flash(f'DBS fee updated to £{fee_val:.2f}', 'success')
        except (ValueError, TypeError):
            flash('Invalid fee value.', 'danger')
        return redirect(url_for('dbs.admin_dbs_settings'))

    fee = _get_dbs_fee()
    return render_template('dbs/admin_settings.html', fee=fee)


def _can(perm_key):
    """Quick permission check delegating to app's user_can."""
    try:
        from app import user_can
        return user_can(perm_key)
    except Exception:
        return False
