import atexit
import base64
import csv
import io
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import wraps
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
from flask import (Flask, abort, flash, jsonify, make_response, redirect,
                   render_template, request, send_file, session, url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, case, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from apscheduler.schedulers.background import \
        BackgroundScheduler  # type: ignore[import]
except ImportError:  # pragma: no cover - optional dependency handled gracefully
    BackgroundScheduler = None

from attendance_utils import (combine_all_sheets, compute_date_range,
                              export_with_custom_header_to_bytes)
from email_utils import (build_appointment_admin_email,
                         build_appointment_email,
                         build_task_notification_email, send_email)
from forms import (AppointmentBookingActionForm, AppointmentBookingForm,
                   AppointmentSlotActionForm, AppointmentSlotBulkForm,
                   AppointmentSlotForm, AvailabilityForm, BookForm,
                   CompanyForm, CycleForm, InvoiceForm, IssueForm, LoginForm,
                   MeetingForm, ObservationForm, PricingConfigForm,
                   RegisterForm, StaffForm, StudentForm, TodoForm,
                   UserProfileForm)
from models import (AppointmentBooking, AppointmentSlot, Availability, Book,
                    BookOrder, BookOrderItem, Company, ErrorReport, Invoice,
                    Issue, IssueChange, Meeting, Observation, ObservationCycle,
                    Permission, PermissionAudit, RolePermission, Staff,
                    Student, StudentChange, Todo, User, UserPermission, db)
from utils import (BRANCH_CHOICES, allowed_file, get_setting,
                   normalize_staff_dataframe, parse_preferred_contact,
                   parse_schedule_message, set_setting)
from version_info import VERSION, changelog_json, get_changelog, latest_entry

SUPPORTED_LANGUAGES = {'en': 'English', 'bn': 'বাংলা'}

PUBLIC_BOOKING_COPY = {
    'en': {
        'page_title': 'Book an appointment',
        'headline': 'Meet with a member of our management team',
        'subheadline': 'Choose a convenient slot with a member of our management team. All fields are required.',
        'name': 'Your name',
        'student_ref': 'Student name / ID',
        'reason': 'Reason for appointment',
        'email': 'Email address',
        'phone': 'Phone number',
        'slot_label': 'Available slots',
        'submit': 'Book appointment',
        'no_slots': 'No slots are currently available. Please check back soon.',
        'slot_taken': 'That slot has just been taken. Please choose a different available time.',
        'language_label': 'Language',
        'toggle_en': 'English',
        'toggle_bn': 'বাংলা',
        'timezone_note': 'All times are shown in UK local time.',
        'slots_heading': 'Upcoming availability',
    'superadmin_label': 'Management team member',
        'form_help': 'We will send a confirmation email immediately and a reminder 12 hours before your meeting.',
        'success_title': 'Appointment booked!',
        'success_message': 'Thank you. We have emailed you the details. A reminder will arrive 12 hours before the meeting.',
        'success_cta': 'Book another appointment',
        'cancel_title': 'Cancel appointment',
        'cancel_message': 'Are you sure you want to cancel your appointment on {date} at {time} with {superadmin}?',
        'cancel_button': 'Cancel appointment',
        'cancelled_title': 'Appointment cancelled',
        'cancelled_message': 'Your appointment has been cancelled. Feel free to book another available slot.',
        'already_cancelled_title': 'Appointment already cancelled',
        'already_cancelled_message': 'This appointment has already been cancelled. You can book another slot from the public booking page.',
        'back_home': 'Return to booking page',
    },
    'bn': {
        'page_title': 'অ্যাপয়েন্টমেন্ট বুক করুন',
        'headline': 'ম্যানেজমেন্ট দলের একজন সদস্যের সাথে দেখা করুন',
        'subheadline': 'ম্যানেজমেন্ট দলের একজন সদস্যের সময় থেকে নির্বাচন করুন। সব ঘর পূরণ করা বাধ্যতামূলক।',
        'name': 'আপনার নাম',
        'student_ref': 'শিক্ষার্থীর নাম / আইডি',
        'reason': 'অ্যাপয়েন্টমেন্টের কারণ',
        'email': 'ইমেইল',
        'phone': 'ফোন',
        'slot_label': 'সময়',
        'submit': 'অ্যাপয়েন্টমেন্ট বুক করুন',
        'no_slots': 'এই মুহূর্তে কোনো সময় পাওয়া যাচ্ছে না। পরে আবার চেষ্টা করুন।',
        'slot_taken': 'এই সময়টি ইতিমধ্যে বুক হয়ে গেছে। অনুগ্রহ করে অন্য সময় নির্বাচন করুন।',
        'language_label': 'ভাষা',
        'toggle_en': 'English',
        'toggle_bn': 'বাংলা',
        'timezone_note': 'সময়গুলো যুক্তরাজ্যের স্থানীয় সময় অনুযায়ী প্রদর্শিত হয়েছে।',
        'slots_heading': 'উপলব্ধ সময়সূচি',
    'superadmin_label': 'ম্যানেজমেন্ট দলের সদস্য',
        'form_help': 'বুকিং করার সাথে সাথেই নিশ্চিতকরণ ইমেইল এবং অ্যাপয়েন্টমেন্টের ১২ ঘণ্টা আগে স্মারক পাঠানো হবে।',
        'success_title': 'অ্যাপয়েন্টমেন্ট বুক সম্পন্ন',
        'success_message': 'ধন্যবাদ। বিস্তারিত আপনাকে ইমেইলে পাঠানো হয়েছে। অ্যাপয়েন্টমেন্টের ১২ ঘণ্টা আগে স্মারক পাঠানো হবে।',
        'success_cta': 'আরেকটি অ্যাপয়েন্টমেন্ট বুক করুন',
        'cancel_title': 'অ্যাপয়েন্টমেন্ট বাতিল করুন',
        'cancel_message': '{superadmin}-এর সাথে {date} তারিখের {time} সময়ের অ্যাপয়েন্টমেন্ট বাতিল করতে চান?',
        'cancel_button': 'অ্যাপয়েন্টমেন্ট বাতিল করুন',
        'cancelled_title': 'অ্যাপয়েন্টমেন্ট বাতিল হয়েছে',
        'cancelled_message': 'আপনার অ্যাপয়েন্টমেন্ট বাতিল করা হয়েছে। প্রয়োজনে নতুন সময় বুক করতে পারেন।',
        'already_cancelled_title': 'অ্যাপয়েন্টমেন্ট আগে থেকেই বাতিল',
        'already_cancelled_message': 'এই অ্যাপয়েন্টমেন্টটি আগেই বাতিল করা হয়েছে। প্রয়োজনে জনসাধারণের বুকিং পেজ থেকে নতুন সময় বুক করুন।',
        'back_home': 'বুকিং পাতায় ফিরে যান',
    },
}


def _current_booking_language() -> str:
    lang = session.get('booking_lang', 'en') if session else 'en'
    if lang not in SUPPORTED_LANGUAGES:
        lang = 'en'
    return lang


def _set_booking_language(lang: str) -> None:
    if lang not in SUPPORTED_LANGUAGES:
        lang = 'en'
    session['booking_lang'] = lang


def _populate_superadmin_choices(form) -> None:
    if not hasattr(form, 'superadmin_id'):
        return
    superadmins = (User.query.filter_by(is_superadmin=True, is_approved=True)
                   .order_by(User.name.asc())
                   .all())
    form.superadmin_id.choices = [(sa.id, sa.name) for sa in superadmins]


def _combine_datetime(date_field, time_field) -> datetime:
    return datetime.combine(date_field, time_field)


def _slot_overlaps(superadmin_id: int, start_at: datetime, end_at: datetime, exclude_id: int | None = None) -> bool:
    q = AppointmentSlot.query.filter(AppointmentSlot.superadmin_id == superadmin_id)
    if exclude_id:
        q = q.filter(AppointmentSlot.id != exclude_id)
    q = q.filter(and_(AppointmentSlot.end_at > start_at, AppointmentSlot.start_at < end_at))
    return db.session.query(q.exists()).scalar()


def _send_email_safe(to_email: str, subject: str, html: str, *, log_prefix: str) -> None:
    try:
        send_email(to_email, subject, html)
    except Exception as exc:  # pragma: no cover - email transport failure should not break UX
        print(f"[WARN] {log_prefix} email send failed to {to_email}: {exc}")


def _active_booking(slot: AppointmentSlot) -> AppointmentBooking | None:
    for booking in slot.bookings:
        if booking.is_active():
            return booking
    return None


def _slot_label(slot: AppointmentSlot) -> str:
    return f"{slot.start_at.strftime('%d %b %Y %H:%M')} – {slot.end_at.strftime('%H:%M')} ({slot.superadmin.name})"


def _available_slots_query():
    # Use timezone-aware UTC datetime to avoid deprecation warnings.
    now = datetime.now(timezone.utc)
    slots = (
        AppointmentSlot.query
        .options(joinedload(AppointmentSlot.superadmin), joinedload(AppointmentSlot.bookings))
        .filter(AppointmentSlot.is_active.is_(True))
        .filter(AppointmentSlot.start_at >= now)
        .order_by(AppointmentSlot.start_at.asc())
        .all()
    )
    return [slot for slot in slots if slot.is_available()]


def _upcoming_slots_query(limit: int | None = None):
    now = datetime.now(timezone.utc)
    query = (
        AppointmentSlot.query
        .options(joinedload(AppointmentSlot.superadmin), joinedload(AppointmentSlot.bookings))
        .filter(AppointmentSlot.start_at >= now)
        .order_by(AppointmentSlot.start_at.asc())
    )
    if limit:
        return query.limit(limit).all()
    return query.all()


def _populate_booking_form(form: AppointmentBookingForm) -> None:
    available_slots = _available_slots_query()
    form.slot_id.choices = [(slot.id, _slot_label(slot)) for slot in available_slots]


def _booking_copy(language: str | None = None) -> dict:
    lang = language or _current_booking_language()
    return PUBLIC_BOOKING_COPY.get(lang, PUBLIC_BOOKING_COPY['en'])


scheduler = None


def _shutdown_scheduler():
    global scheduler
    if scheduler:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        scheduler = None


def _ensure_scheduler_started():
    global scheduler
    if BackgroundScheduler is None:
        return
    if scheduler is None:
        scheduler = BackgroundScheduler(timezone='UTC')  # type: ignore[call-arg]
        scheduler.start()
        atexit.register(_shutdown_scheduler)


def _send_reminder_job(booking_id: int) -> None:
    with app.app_context():
        booking = (AppointmentBooking.query
                   .options(joinedload(AppointmentBooking.slot).joinedload(AppointmentSlot.superadmin))
                   .get(booking_id))
        if not booking or not booking.is_active():
            return
        slot = booking.slot
        if not slot or slot.start_at <= datetime.now(timezone.utc):
            return
        cancel_url = booking.cancel_url or url_for('booking_cancel', token=booking.cancel_token, _external=True)
        subj, html = build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='reminder', cancel_url=cancel_url)
        _send_email_safe(booking.email, subj, html, log_prefix='Appointment reminder')
        admin_subj, admin_html = build_appointment_admin_email(booking, slot, mode='reminder')
        _send_email_safe(slot.superadmin.email, admin_subj, admin_html, log_prefix='Appointment reminder admin')
        booking.reminder_sent_at = datetime.now(timezone.utc)
        db.session.commit()


def _schedule_reminder(booking: AppointmentBooking) -> None:
    if BackgroundScheduler is None:
        return
    _ensure_scheduler_started()
    if scheduler is None:
        return
    slot = booking.slot
    if not slot:
        return
    run_at = slot.start_at - timedelta(hours=12)
    if run_at <= datetime.now(timezone.utc):
        return
    job_id = f"booking-reminder-{booking.id}"
    try:
        scheduler.add_job(_send_reminder_job, 'date', run_date=run_at, id=job_id, replace_existing=True, args=[booking.id])
    except Exception as exc:
        print(f"[WARN] Failed to schedule reminder for booking {booking.id}: {exc}")


def _cancel_reminder(booking_id: int) -> None:
    if scheduler is None:
        return
    job_id = f"booking-reminder-{booking_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass


def _prime_existing_reminders() -> None:
    if BackgroundScheduler is None:
        return
    _ensure_scheduler_started()
    if scheduler is None:
        return
    now = datetime.now(timezone.utc)
    upcoming = (
        AppointmentBooking.query
        .join(AppointmentSlot)
        .options(joinedload(AppointmentBooking.slot).joinedload(AppointmentSlot.superadmin))
        .filter(AppointmentBooking.status == 'booked')
        .filter(AppointmentBooking.cancelled_at.is_(None))
        .filter(AppointmentSlot.start_at > now)
        .all()
    )
    for booking in upcoming:
        _schedule_reminder(booking)

SECRET_KEY = "change-this-in-production"
SECURITY_SALT = "excel-tutors-reset-salt"
DATABASE_URI = "sqlite:///observations.db"

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SQLALCHEMY_DATABASE_URI=DATABASE_URI,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    UPLOAD_EXTENSIONS=[".xlsx", ".xls", ".csv"],
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
)

# Initialize CSRF protection (applies to all modifying requests). For API style
# fetch POSTs we will expose the token via a context processor/meta tag.
csrf = CSRFProtect(app)

# Register Jinja checklist normalization helpers
try:
    from checklist_utils import register_checklist_jinja
    register_checklist_jinja(app.jinja_env)
except Exception as _e:  # non-fatal
    print(f"[WARN] Failed to register checklist helpers: {_e}")

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

ts = URLSafeTimedSerializer(SECRET_KEY)

# Import-time minimal schema patch (ensures test harness db.create_all covers new columns)
with app.app_context():  # pragma: no cover - simple safety patch
    try:
        from sqlalchemy import text as _text
        with db.engine.connect() as _conn:
            cols = {row[1] for row in _conn.execute(_text("PRAGMA table_info(user)"))}
            if 'theme_preference' not in cols and 'user' in {t[0] for t in _conn.execute(_text("SELECT name FROM sqlite_master WHERE type='table'"))}:
                try:
                    _conn.execute(_text("ALTER TABLE user ADD COLUMN theme_preference VARCHAR(20) DEFAULT 'system'"))
                except Exception:
                    pass
        # Ensure tool permissions exist
        needed_perms = [
            ('manage_pricing','Manage pricing configuration'),
            ('manage_books','Manage book catalog'),
            ('order_books','Place book print orders'),
            ('access_enrollment_tool','Access enrollment calculator tool'),
            ('access_timetable_main','Access main timetable generator'),
            ('access_timetable_eastham','Access East Ham timetable generator'),
        ]
        created = False
        for k, desc in needed_perms:
            if not Permission.query.filter_by(key=k).first():
                db.session.add(Permission(key=k, description=desc))
                created = True
        if created:
            db.session.commit()
        # --- Lightweight schema patch for new Book fields (Oct 2025) ---
        try:
            book_cols = {row[1] for row in _conn.execute(_text("PRAGMA table_info(book)"))}
            needed_new = {
                'cover': "ALTER TABLE book ADD COLUMN cover VARCHAR(255)",
                'cover_url': "ALTER TABLE book ADD COLUMN cover_url VARCHAR(500)",
                'inner': "ALTER TABLE book ADD COLUMN inner VARCHAR(255)",
                'inner_url': "ALTER TABLE book ADD COLUMN inner_url VARCHAR(500)",
                'print_format': "ALTER TABLE book ADD COLUMN print_format VARCHAR(120)",
                'finishing': "ALTER TABLE book ADD COLUMN finishing VARCHAR(120)"
            }
            for col, stmt in needed_new.items():
                if col not in book_cols:
                    try:
                        _conn.execute(_text(stmt))
                    except Exception:
                        pass
        except Exception:
            pass
        # --- Lightweight schema patch for Invoice.created_by_id (Oct 2025) ---
        try:
            inv_cols = {row[1] for row in _conn.execute(_text("PRAGMA table_info(invoice)"))}
            if 'created_by_id' not in inv_cols:
                try:
                    _conn.execute(_text("ALTER TABLE invoice ADD COLUMN created_by_id INTEGER"))
                except Exception:
                    pass
        except Exception:
            pass
        # One-off seed: if Book table empty and legacy book_catalog setting absent but a JSON file is provided manually.
        try:
            from models import Book  # local import after db init
            if Book.query.count() == 0:
                # Attempt to read a local seed file if exists in instance or root (book_list_full.json)
                seed_paths = [
                    os.path.join(app.root_path, 'book_list_full.json'),
                    os.path.join(app.instance_path, 'book_list_full.json'),
                ]
                records = []
                import json as _json
                for p in seed_paths:
                    if os.path.isfile(p):
                        try:
                            with open(p,'r',encoding='utf-8') as fh:
                                records = _json.load(fh)
                                break
                        except Exception:
                            pass
                # Fallback: if no file present, check legacy setting
                if not records:
                    legacy = get_setting('book_catalog', [], as_json=True)
                    if legacy:
                        records = legacy
                created_any = False
                for rec in records:
                    name = rec.get('Book_Name') or rec.get('name')
                    if not name:
                        continue
                    price = rec.get('Price') or rec.get('price') or 0
                    subject = rec.get('Subject') or rec.get('subject')
                    year = rec.get('Year') or rec.get('year') or ''
                    # Normalize year numeric to key
                    year_group = None
                    try:
                        if isinstance(year, int) or (isinstance(year,str) and year.isdigit()):
                            yint = int(year)
                            if 3 <= yint <=5: year_group='year3-5'
                            elif 6 <= yint <=7: year_group='year6-7'
                            elif yint==8: year_group='year8'
                            elif yint==9: year_group='year9'
                            elif yint==10: year_group='year10'
                            elif yint==11: year_group='year11'
                            elif yint>=12: year_group='alevel'
                    except Exception:
                        pass
                    # Legacy image_url removed; ignore any legacy URL.
                    if Book.query.filter_by(name=name).first():
                        continue
                    db.session.add(Book(name=name.strip(), price=float(price or 0), subject=subject or None, year_group=year_group, active=True))
                    created_any = True
                if created_any:
                    db.session.commit()
            # Additional catalog seed (October 2025 spec) - idempotent
            new_spec_seed = [
                {"Book_Name": "Year 6 SATS Maths Arithmetic and Reasoning (2 books)", "Subject": "", "Year": "", "Price": 20.0, "Print_Format": "2-in-1", "Finishing": ""},
                {"Book_Name": "Student Planner (Small)", "Subject": "", "Year": "", "Price": 3.0, "Print_Format": "2-in-1", "Finishing": ""},
                {"Book_Name": "Student Planner (Large)", "Subject": "", "Year": "", "Price": 5.0, "Print_Format": "2-in-1", "Finishing": ""},
                {"Book_Name": "Science Writing Book", "Subject": "", "Year": "", "Price": 1.5, "Print_Format": "2-in-1", "Finishing": ""},
                {"Book_Name": "Printing White Paper", "Subject": "", "Year": "", "Price": 0.0, "Print_Format": "2-in-1", "Finishing": ""},
                {"Book_Name": "Maths Writing Book", "Subject": "", "Year": "", "Price": 1.5, "Print_Format": "2-in-1", "Finishing": ""},
                {"Book_Name": "Hand Writing Book", "Subject": "English", "Year": "", "Price": 6.0, "Print_Format": "2-in-1", "Finishing": ""},
                {"Book_Name": "Excel Year 8 Maths Workbook", "Subject": "Maths", "Year": 8, "Price": 23.0, "Print_Format": "2-in-1", "Finishing": ""},
            ]
            seeded_any = False
            for rec in new_spec_seed:
                if not Book.query.filter_by(name=rec["Book_Name"]).first():
                    year_val = rec.get('Year')
                    if isinstance(year_val, int):
                        # store direct numeric year as string for simplicity
                        year_group = str(year_val)
                    else:
                        year_group = (year_val or '').strip() or None
                    b = Book(
                        name=rec['Book_Name'],
                        price=float(rec.get('Price') or 0),
                        subject=(rec.get('Subject') or None) or None,
                        year_group=year_group,
                        print_format=rec.get('Print_Format') or None,
                        finishing=rec.get('Finishing') or None,
                        active=True
                    )
                    db.session.add(b)
                    seeded_any = True
            if seeded_any:
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
        except Exception as _se:
            print(f"[WARN] Book seed skipped: {_se}")
    except Exception:
        pass

# --------------- Permission Helpers (server-side) --------------- #


def user_can(perm_key: str) -> bool:
    """Server-side permission check aligned with template helper.

    Order: superadmin bypass > explicit user override > role permission.
    Fail-safe: return False on unexpected error.
    """
    try:
        if current_user.is_authenticated and getattr(current_user, 'is_superadmin', False):
            return True
        if not current_user.is_authenticated:
            return False
        up = UserPermission.query.filter_by(user_id=current_user.id, permission_key=perm_key).first()
        if up:
            return bool(up.allow)
        role = (current_user.role or 'staff')
        return bool(RolePermission.query.filter_by(role=role, permission_key=perm_key).first())
    except Exception:
        return False

def permission_required(*perm_keys: str, any: bool = False):
    """Route decorator enforcing permissions.

    @permission_required('perm_a')  # need perm_a
    @permission_required('perm_a','perm_b')  # need both
    @permission_required('perm_a','perm_b', any=True)  # need at least one
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if getattr(current_user, 'is_superadmin', False):
                return fn(*args, **kwargs)
            checks = [user_can(p) for p in perm_keys]
            allowed = any(checks) if any else all(checks)
            if not allowed:
                needed = ' or '.join(perm_keys) if any else ', '.join(perm_keys)
                abort(403, description=f"Requires permission: {needed}")
            return fn(*args, **kwargs)
        return wrapper
    return decorator

@app.context_processor
def inject_version():
    lang = _current_booking_language()
    # Generate a single CSRF token for this request (avoid multiple generate_csrf calls producing confusion)
    token = generate_csrf()
    return {
        "APP_VERSION": VERSION,
        'can': user_can,
        'booking_language': lang,
        'supported_languages': SUPPORTED_LANGUAGES,  # lower-case for standard use
        'SUPPORTED_LANGUAGES': SUPPORTED_LANGUAGES,  # expose uppercase name used in some templates
        'user_theme_preference': getattr(current_user, 'theme_preference', 'system') if current_user.is_authenticated else 'system',
        'CSRF_TOKEN': token,
        'csrf_token': lambda: token  # backward compatibility for {{ csrf_token() }}
    }

# --------- Common Template Filters (dates, money) ---------- #
@app.template_filter('fmt_date')
def fmt_date(value):
    try:
        if not value:
            return ''
        return value.strftime('%d-%m-%Y')
    except Exception:
        return ''

@app.template_filter('fmt_money')
def fmt_money(value):
    try:
        if value is None or value == '':
            return '£0.00'
        return f"£{float(value):.2f}"
    except Exception:
        return str(value)

@app.route('/version-history')
def version_history():
    return jsonify({"version": VERSION, "changelog": get_changelog()})

@app.route('/api/version')
def api_version():
    """Lightweight version endpoint (backwards compatible fields)."""
    full = get_changelog()
    entry = latest_entry()
    return jsonify({
        'version': VERSION,
        'changelog_current': (entry.body if entry else ''),
        'changelog_full': full,
        'date': entry.date if entry else None,
    })


@app.route('/api/changelog')
def api_changelog():
    """Structured changelog (parsed) for richer clients.

    Query Params:
        limit: optional int to limit number of entries returned.
    """
    try:
        limit_val = request.args.get('limit')
        limit = int(limit_val) if limit_val else None
    except ValueError:
        limit = None
    return jsonify({
        'version': VERSION,
        'entries': changelog_json(limit=limit)
    })

# ---------------- DEBUG (Temporary) ---------------- #
@app.route('/debug/observation/<int:oid>/checklists')
@login_required
def debug_observation_checklists(oid: int):
    obs = Observation.query.get_or_404(oid)
    if not obs.detail:
        return jsonify({'observation_id': oid, 'detail': None})
    d = obs.detail
    payload = {}
    for grp in ['weekly_test','homework','classwork','org_mgmt']:
        raw = getattr(d, grp)
        try:
            import json
            parsed_raw = json.loads(raw) if raw else {}
        except Exception:
            parsed_raw = {}
        norm = d.get_checklist(grp)
        payload[grp] = {
            'raw': parsed_raw,
            'normalized_keys': sorted(norm.keys()),
            'true_keys': sorted([k for k,v in norm.items() if v]),
            'false_keys': sorted([k for k,v in norm.items() if not v]),
        }
    return jsonify(payload)

# ---------------- Attendance Fix Page ---------------- #
@app.route('/attendance/fix')
@login_required
@permission_required('manage_attendance_fix')
def attendance_fix():
    return render_template('attendance/fix.html')

@app.route('/attendance/fix/process', methods=['POST'])
@login_required
def attendance_process():
    try:
        year = int(request.form.get('year'))
        month = int(request.form.get('month'))
    except Exception:
        return ('Invalid year or month', 400)
    excel_file = request.files.get('excel_file')
    if not excel_file:
        return ('No file uploaded', 400)
    excel_file.seek(0)
    try:
        df = combine_all_sheets(excel_file, year, month)
    except Exception as e:
        return (f'Failed to process workbook: {e}', 500)
    start_date, end_date = compute_date_range(year, month)
    try:
        out = export_with_custom_header_to_bytes(df, start_date, end_date)
    except Exception as e:
        return (f'Failed to build output workbook: {e}', 500)
    # Successful build – return generated file
    return send_file(out, as_attachment=True, download_name='Attendance_Fixed.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None

@app.before_request
def create_tables_and_superadmin():
    db.create_all()
    # Ensure newly added Book columns exist even if earlier import-time patch
    # ran before the book table was first created (older DBs or test DB reset).
    try:  # pragma: no cover - defensive migration logic
        from sqlalchemy import text as _text
        with db.engine.connect() as _conn:
            tables = {t[0] for t in _conn.execute(_text("SELECT name FROM sqlite_master WHERE type='table'"))}
            if 'book' in tables:
                existing_cols = {row[1] for row in _conn.execute(_text("PRAGMA table_info(book)"))}
                col_statements = {
                    'cover': "ALTER TABLE book ADD COLUMN cover VARCHAR(255)",
                    'cover_url': "ALTER TABLE book ADD COLUMN cover_url VARCHAR(500)",
                    'inner': 'ALTER TABLE book ADD COLUMN "inner" VARCHAR(255)',  # quoted due to SQL keyword
                    'inner_url': "ALTER TABLE book ADD COLUMN inner_url VARCHAR(500)",
                    'print_format': "ALTER TABLE book ADD COLUMN print_format VARCHAR(120)",
                    'finishing': "ALTER TABLE book ADD COLUMN finishing VARCHAR(120)",
                }
                applied_any = False
                for col, stmt in col_statements.items():
                    if col not in existing_cols:
                        try:
                            _conn.execute(_text(stmt))
                            applied_any = True
                        except Exception as _exc:
                            print(f"[WARN] Failed to backfill book column '{col}': {_exc}")
                if applied_any:
                    print('[INFO] Backfilled missing Book columns (cover/inner/format fields).')
    except Exception as _outer_exc:
        print(f"[WARN] Book column backfill skipped: {_outer_exc}")
    # Lightweight SQLite schema patch for new user columns (role, picture)
    try:
        with db.engine.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(user)"))}
            # Backfill user.is_active column EARLY (before any ORM query referencing it)
            if 'is_active' not in cols:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN is_active BOOLEAN DEFAULT 1"))
                    # Refresh cols set so later logic doesn't attempt again
                    cols.add('is_active')
                except Exception:
                    pass
            if 'role' not in cols:
                conn.execute(text("ALTER TABLE user ADD COLUMN role VARCHAR(80)"))
            if 'picture' not in cols:
                conn.execute(text("ALTER TABLE user ADD COLUMN picture VARCHAR(255)"))
            if 'theme_preference' not in cols:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN theme_preference VARCHAR(20) DEFAULT 'system'"))
                except Exception:
                    pass
    except Exception:
        # Silent fail; if this is not SQLite or table absent yet, it will be handled later
        pass
    # Ensure seeded superadmin exists and is flagged correctly
    sa = User.query.filter_by(email="superadmin@exceltutors.org.uk").first()
    if not sa:
        sa = User(
            name="Management Team Member",
            email="superadmin@exceltutors.org.uk",
            password_hash=generate_password_hash("superadmin123"),
            is_superadmin=True,
            is_approved=True,
            role='superadmin'
        )
        db.session.add(sa)
        db.session.commit()
    else:
        # If record exists but lost its superadmin or approval flags, restore them quietly.
        changed = False
        if not sa.is_superadmin:
            sa.is_superadmin = True
            changed = True
        if not sa.is_approved:
            sa.is_approved = True
            changed = True
        if not sa.role:
            sa.role = 'superadmin'
            changed = True
        if changed:
            db.session.commit()
    # (Moved is_active backfill earlier; legacy block retained intentionally removed)
    # Simple backfill for any existing users missing new columns (SQLite tolerant)
    try:
        users_no_role = User.query.filter(User.role.is_(None)).all()
        altered = False
        for u in users_no_role:
            u.role = 'staff'
            altered = True
        # Legacy role migration
        legacy_map = {'observer':'supervisor','lead':'centre_manager'}
        legacy_users = User.query.filter(User.role.in_(legacy_map.keys())).all()
        for lu in legacy_users:
            new_role = legacy_map.get(lu.role)
            if new_role and lu.role != new_role:
                lu.role = new_role
                altered = True
        if altered:
            db.session.commit()
    except Exception:
        pass
    # Ensure upload folder exists for profile pictures
    upload_dir = os.path.join(app.root_path, 'static', 'uploads')
    if not os.path.isdir(upload_dir):
        try:
            os.makedirs(upload_dir, exist_ok=True)
        except Exception:
            pass
    # Ensure availability table exists (lightweight auto-migrate approach)
    try:
        with db.engine.connect() as conn:
            conn.execute(text("CREATE TABLE IF NOT EXISTS availability (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, department VARCHAR(120), branches VARCHAR(255), days TEXT, subjects TEXT, notes TEXT, created_at DATETIME, updated_at DATETIME)"))
            conn.execute(text("CREATE TABLE IF NOT EXISTS issue (id INTEGER PRIMARY KEY, title VARCHAR(200) NOT NULL, details TEXT, status VARCHAR(50), criticality VARCHAR(50), urgency VARCHAR(50), branch VARCHAR(120), created_by_id INTEGER NOT NULL, created_at DATETIME, updated_at DATETIME, action_taken TEXT)"))
            conn.execute(text("CREATE TABLE IF NOT EXISTS issue_change (id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL, field VARCHAR(120) NOT NULL, old_value TEXT, new_value TEXT, changed_by_id INTEGER NOT NULL, changed_at DATETIME, FOREIGN KEY(issue_id) REFERENCES issue(id), FOREIGN KEY(changed_by_id) REFERENCES user(id))"))
            # Backfill action_taken column if older issue table missing it
            try:
                issue_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(issue)"))}
                if 'action_taken' not in issue_cols:
                    conn.execute(text("ALTER TABLE issue ADD COLUMN action_taken TEXT"))
            except Exception:
                pass
            # Backfill active column on staff if missing
            try:
                staff_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(staff)"))}
                if 'active' not in staff_cols:
                    conn.execute(text("ALTER TABLE staff ADD COLUMN active BOOLEAN DEFAULT 1"))
            except Exception:
                pass
            # Meetings table
            conn.execute(text("CREATE TABLE IF NOT EXISTS meeting (id INTEGER PRIMARY KEY, participant_id INTEGER NOT NULL, booked_by_id INTEGER NOT NULL, agenda VARCHAR(500) NOT NULL, student_name VARCHAR(200), parent_name VARCHAR(200), outcome TEXT, date DATE NOT NULL, time VARCHAR(10) NOT NULL, created_at DATETIME, updated_at DATETIME)"))
            # Backfill added columns if table existed without them
            try:
                meeting_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(meeting)"))}
                if 'student_name' not in meeting_cols:
                    conn.execute(text("ALTER TABLE meeting ADD COLUMN student_name VARCHAR(200)"))
                if 'parent_name' not in meeting_cols:
                    conn.execute(text("ALTER TABLE meeting ADD COLUMN parent_name VARCHAR(200)"))
                if 'outcome' not in meeting_cols:
                    conn.execute(text("ALTER TABLE meeting ADD COLUMN outcome TEXT"))
            except Exception:
                pass
            # Ensure indexes for IssueChange performance (SQLite creates automatically for PK, but add composite if needed)
            # Todo table (tasks)
            conn.execute(text("CREATE TABLE IF NOT EXISTS todo (id INTEGER PRIMARY KEY, description VARCHAR(400) NOT NULL, notes TEXT, actions_taken TEXT, criticality VARCHAR(50), urgency VARCHAR(50), status VARCHAR(30) DEFAULT 'Pending', due_date DATE, created_at DATETIME, updated_at DATETIME, created_by_id INTEGER NOT NULL, assigned_to_id INTEGER NOT NULL)"))
            # Backfill status column if earlier draft existed
            try:
                todo_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(todo)"))}
                if 'status' not in todo_cols:
                    conn.execute(text("ALTER TABLE todo ADD COLUMN status VARCHAR(30) DEFAULT 'Pending'"))
                if 'actions_taken' not in todo_cols:
                    conn.execute(text("ALTER TABLE todo ADD COLUMN actions_taken TEXT"))
            except Exception:
                pass
            # Observation detail extended table
            conn.execute(text("CREATE TABLE IF NOT EXISTS observation_detail (id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL UNIQUE, timeslot VARCHAR(20), weekly_test TEXT, weekly_test_comment TEXT, homework TEXT, homework_comment TEXT, classwork TEXT, classwork_comment TEXT, org_mgmt TEXT, org_mgmt_comment TEXT, positives TEXT, improvements TEXT, target_set TEXT, actions_taken TEXT, notes TEXT, next_review_date DATE, FOREIGN KEY(observation_id) REFERENCES observation(id))"))
            # Permissions tables (0.9.0) if not present
            conn.execute(text("CREATE TABLE IF NOT EXISTS permission (key VARCHAR(120) PRIMARY KEY, description VARCHAR(255))"))
            conn.execute(text("CREATE TABLE IF NOT EXISTS role_permission (id INTEGER PRIMARY KEY, role VARCHAR(80) NOT NULL, permission_key VARCHAR(120) NOT NULL, UNIQUE(role, permission_key))"))
            conn.execute(text("CREATE TABLE IF NOT EXISTS user_permission (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, permission_key VARCHAR(120) NOT NULL, allow BOOLEAN NOT NULL DEFAULT 1, UNIQUE(user_id, permission_key))"))
            # Permission audit (since 0.9.2)
            conn.execute(text("CREATE TABLE IF NOT EXISTS permission_audit (id INTEGER PRIMARY KEY, actor_user_id INTEGER NOT NULL, target_user_id INTEGER, role VARCHAR(80), permission_key VARCHAR(120) NOT NULL, action VARCHAR(40) NOT NULL, changed_at DATETIME, FOREIGN KEY(actor_user_id) REFERENCES user(id), FOREIGN KEY(target_user_id) REFERENCES user(id))"))
            # Company table column backfill (OFSTED reg no) if table exists from older schema
            try:
                company_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(company)"))}
                if 'ofsted_reg_no' not in company_cols:
                    conn.execute(text("ALTER TABLE company ADD COLUMN ofsted_reg_no VARCHAR(64)"))
            except Exception:
                pass
            # Students table (since 0.9.9) - simple schema; preferred_contact_raw retained
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS student (
                id INTEGER PRIMARY KEY,
                student_id VARCHAR(64) NOT NULL UNIQUE,
                name VARCHAR(255) NOT NULL,
                type VARCHAR(120),
                year VARCHAR(20),
                preferred_contact_raw TEXT,
                email VARCHAR(255),
                phone VARCHAR(64),
                address TEXT,
                academic TEXT,
                status VARCHAR(120),
                created_at DATETIME,
                updated_at DATETIME
            )"""))
            # Student change audit table (since 0.9.9)
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS student_change (
                id INTEGER PRIMARY KEY,
                student_id INTEGER NOT NULL,
                field VARCHAR(120) NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_by_id INTEGER NOT NULL,
                changed_at DATETIME,
                FOREIGN KEY(student_id) REFERENCES student(id),
                FOREIGN KEY(changed_by_id) REFERENCES user(id)
            )"""))
            # Staff change audit table (since 0.9.10)
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS staff_change (
                id INTEGER PRIMARY KEY,
                staff_id INTEGER NOT NULL,
                field VARCHAR(120) NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_by_id INTEGER NOT NULL,
                changed_at DATETIME,
                FOREIGN KEY(staff_id) REFERENCES staff(id),
                FOREIGN KEY(changed_by_id) REFERENCES user(id)
            )
            """))
    except Exception:
        pass
    # Seed permissions & default role mappings if empty (idempotent)
    try:
        base_permissions = [
            ('view_dashboard','Access dashboard'),
            ('manage_staff','Create/Edit staff records'),
            ('manage_cycles','Manage observation cycles'),
            ('manage_observations','Create/Edit observations'),
            ('manage_availability','View & manage tutor availability'),
            ('manage_issues','Issue tracker management'),
            ('manage_meetings','Create/Edit meetings'),
            ('manage_tasks','Create/Edit tasks'),
            ('manage_attendance_fix','Use attendance fix tool'),
            ('manage_users','Approve & manage users'),
            ('view_reports','View / generate reports'),
            ('manage_invoices','Invoice & company management'),
            ('manage_appointments','Manage appointment slots & bookings'),
            ('manage_students','Manage student records'),
            ('manage_books','Manage book catalog'),
            ('order_books','Place book print orders'),
            ('manage_pricing','Manage tuition pricing & fees'),
        ]
        existing_keys = {p.key for p in Permission.query.all()}
        for k, desc in base_permissions:
            if k not in existing_keys:
                db.session.add(Permission(key=k, description=desc))
        db.session.commit()

        # Refresh permission list after potential inserts
        all_perm_keys = [p.key for p in Permission.query.all()]

        # Default role permission seeds (updated taxonomy 0.9.3)
        role_defaults = {
            'staff': {'view_dashboard','manage_tasks','view_reports'},
            'supervisor': {'view_dashboard','manage_tasks','manage_observations','manage_meetings','view_reports','manage_books','order_books'},
            'centre_manager': {'view_dashboard','manage_tasks','manage_observations','manage_staff','manage_cycles','manage_issues','manage_availability','manage_meetings','view_reports','manage_books','order_books','manage_pricing'},
            'admin': {'view_dashboard','manage_tasks','manage_observations','manage_staff','manage_cycles','manage_issues','manage_availability','manage_meetings','manage_users','manage_attendance_fix','view_reports','manage_invoices','manage_appointments','manage_books','order_books','manage_pricing'},
        }
        # Admin should also manage students (append if not present for backward runs)
        role_defaults['admin'].add('manage_students')
        role_defaults['centre_manager'].add('manage_students')
        role_defaults['supervisor'].add('manage_students')  # allow view/manage if desired
        for role, perms in role_defaults.items():
            has_any = RolePermission.query.filter_by(role=role).first()
            if not has_any:
                for pk in perms:
                    if Permission.query.get(pk):
                        db.session.add(RolePermission(role=role, permission_key=pk))
        # Ensure superadmin explicit role rows contain ALL permissions (even though bypass exists)
        for pk in all_perm_keys:
            if not RolePermission.query.filter_by(role='superadmin', permission_key=pk).first():
                db.session.add(RolePermission(role='superadmin', permission_key=pk))

        # Normalize any superadmin users missing the 'superadmin' role value
        superadmins = User.query.filter_by(is_superadmin=True).all()
        changed = False
        for sa in superadmins:
            if sa.role != 'superadmin':
                sa.role = 'superadmin'
                changed = True
        if changed:
            print('[INFO] Normalized superadmin user role values to "superadmin"')
        db.session.commit()
    except Exception as e:
        print(f"[WARN] Permission seed failed: {e}")
    # Schema patches for appointments
    try:
        with db.engine.connect() as conn:
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS appointment_slot (
                id INTEGER PRIMARY KEY,
                superadmin_id INTEGER NOT NULL,
                created_by_id INTEGER NOT NULL,
                start_at DATETIME NOT NULL,
                end_at DATETIME NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                notes VARCHAR(255),
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(superadmin_id) REFERENCES user(id),
                FOREIGN KEY(created_by_id) REFERENCES user(id)
            )
            """))
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS appointment_booking (
                id INTEGER PRIMARY KEY,
                slot_id INTEGER NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'booked',
                name VARCHAR(200) NOT NULL,
                student_ref VARCHAR(200) NOT NULL,
                reason TEXT NOT NULL,
                email VARCHAR(255) NOT NULL,
                phone VARCHAR(50) NOT NULL,
                language VARCHAR(5) NOT NULL DEFAULT 'en',
                cancel_token VARCHAR(64) UNIQUE NOT NULL,
                cancel_url VARCHAR(500),
                confirmation_sent_at DATETIME,
                reminder_sent_at DATETIME,
                cancelled_at DATETIME,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(slot_id) REFERENCES appointment_slot(id) ON DELETE CASCADE
            )
            """))
    except Exception as exc:
        print(f"[WARN] Appointment schema init failed: {exc}")


# Flask 3.x removed before_first_request style hooks in some contexts; we lazily
# prime appointment reminder jobs on the first real request instead.
_BOOKING_SCHEDULER_PRIMED = False

@app.before_request
def _bootstrap_booking_scheduler():  # pragma: no cover - trivial guard
    global _BOOKING_SCHEDULER_PRIMED
    if not _BOOKING_SCHEDULER_PRIMED:
        try:
            _prime_existing_reminders()
        except Exception as exc:
            print(f"[WARN] Failed to prime booking reminders: {exc}")
        _BOOKING_SCHEDULER_PRIMED = True


@app.route("/")
def index():
    if not current_user.is_authenticated:
        return redirect(url_for('login'))

    # ---------------- Cycle Filter ----------------
    selected_cycle_ids = []
    for cid in request.args.getlist('cycle'):
        try:
            cid_i = int(cid)
            if cid_i > 0:
                selected_cycle_ids.append(cid_i)
        except Exception:
            pass
    selected_cycle_ids = list(dict.fromkeys(selected_cycle_ids))  # dedupe preserve order

    all_cycles = ObservationCycle.query.order_by(ObservationCycle.start_date.desc().nullslast()).all()

    # Base observation query (scoped by cycles if selected)
    obs_query = Observation.query
    if selected_cycle_ids:
        obs_query = obs_query.filter(Observation.cycle_id.in_(selected_cycle_ids))

    # ---------------- Counts & Basics ----------------
    total_staff = Staff.query.count()
    total_cycles = len(all_cycles)
    total_observations = obs_query.count()

    # Tutors with no observations within the selected cycles (or overall if none selected)
    if selected_cycle_ids:
        subq = db.select(Observation.staff_id).filter(Observation.cycle_id.in_(selected_cycle_ids)).distinct()
    else:
        subq = db.select(Observation.staff_id).distinct()
    # Use selectable directly to avoid SAWarning about coercing Subquery in IN()
    no_obs_staff = Staff.query.filter(~Staff.id.in_(subq)).order_by(Staff.name.asc()).all()

    # ---------------- Observer Leaderboard ----------------
    observer_counts_q = db.session.query(User.name, db.func.count(Observation.id).label('cnt')).join(Observation, Observation.observer_id == User.id)
    if selected_cycle_ids:
        observer_counts_q = observer_counts_q.filter(Observation.cycle_id.in_(selected_cycle_ids))
    observer_counts = (
        observer_counts_q.group_by(User.id).order_by(db.func.count(Observation.id).desc()).limit(10).all()
    )

    # ---------------- Tutor Performance (Top & Concern) ----------------
    tutor_avgs_q = db.session.query(Staff.id, Staff.name, db.func.avg(Observation.score).label('avg'), db.func.count(Observation.id).label('c')).join(Observation, Observation.staff_id == Staff.id)
    if selected_cycle_ids:
        tutor_avgs_q = tutor_avgs_q.filter(Observation.cycle_id.in_(selected_cycle_ids))
    tutor_avgs = tutor_avgs_q.group_by(Staff.id).having(db.func.count(Observation.id) > 0).all()
    top_tutors = sorted(tutor_avgs, key=lambda r: r.avg or 0, reverse=True)[:10]
    concern_tutors = sorted(tutor_avgs, key=lambda r: r.avg or 0)[:10]

    # ---------------- Staff Distributions (overall, not cycle-scoped) ----------------
    branch_counts = {}
    for staff in Staff.query.with_entities(Staff.branch).all():
        if not staff.branch:
            continue
        for b in [x.strip() for x in staff.branch.split(',') if x.strip()]:
            branch_counts[b] = branch_counts.get(b, 0) + 1
    branch_counts_items = sorted(branch_counts.items(), key=lambda x: (-x[1], x[0]))

    dept_counts = (
        db.session.query(Staff.department, db.func.count(Staff.id))
        .filter(Staff.department.isnot(None))
        .group_by(Staff.department)
        .order_by(db.func.count(Staff.id).desc())
        .all()
    )

    # ---------------- Score Summary (scoped) ----------------
    if total_observations:
        score_summary = obs_query.with_entities(
            db.func.min(Observation.score),
            db.func.avg(Observation.score),
            db.func.max(Observation.score),
        ).first()
    else:
        score_summary = (None, None, None)

    # ---------------- Recent Observations (scoped) ----------------
    recent_observations = obs_query.order_by(Observation.date.desc()).limit(10).all()

    # ---------------- Weekly Trend (last 12 weeks, scoped) ----------------
    from datetime import timedelta
    today = date.today()
    start_window = today - timedelta(weeks=12)
    weekly_q = db.session.query(
        db.func.strftime('%Y-%W', Observation.date).label('week'),
        db.func.count(Observation.id),
        db.func.avg(Observation.score),
    ).filter(Observation.date >= start_window)
    if selected_cycle_ids:
        weekly_q = weekly_q.filter(Observation.cycle_id.in_(selected_cycle_ids))
    weekly_rows = weekly_q.group_by('week').order_by('week').all()
    weekly_trend = [
        {
            'week': w,
            'count': int(c or 0),
            'avg': float(a) if a is not None else None,
        }
        for (w, c, a) in weekly_rows
    ]

    # ---------------- Cycle Stats (overall, for context) ----------------
    cycle_stats = (
        db.session.query(
            ObservationCycle.id,
            ObservationCycle.title,
            db.func.count(Observation.id).label('cnt'),
            db.func.avg(Observation.score).label('avg'),
        )
        .join(Observation, Observation.cycle_id == ObservationCycle.id, isouter=True)
        .group_by(ObservationCycle.id)
        .order_by(ObservationCycle.start_date.desc().nullslast())
        .all()
    )

    # ---------------- Variance (scoped overall & per cycle overall) ----------------
    overall_variance = None
    if total_observations > 1:
        overall_variance = obs_query.with_entities(
            (db.func.avg(Observation.score * Observation.score) - db.func.avg(Observation.score) * db.func.avg(Observation.score))
        ).scalar()

    cycle_variances = []
    for cid, title, cnt, avg in cycle_stats:
        var = None
        if cnt and cnt > 1:
            var = db.session.query(
                (db.func.avg(Observation.score * Observation.score) - db.func.avg(Observation.score) * db.func.avg(Observation.score))
            ).filter(Observation.cycle_id == cid).scalar()
        cycle_variances.append({
            'id': cid,
            'title': title,
            'count': int(cnt or 0),
            'avg': float(avg) if avg is not None else None,
            'variance': float(var) if var is not None else None,
        })

    # ---------------- Observer Calibration (scoped) ----------------
    global_avg = score_summary[1] if score_summary[1] is not None else None
    calibration_q = db.session.query(
        User.name,
        db.func.count(Observation.id).label('cnt'),
        db.func.avg(Observation.score).label('avg'),
        (db.func.avg(Observation.score * Observation.score) - db.func.avg(Observation.score) * db.func.avg(Observation.score)).label('var'),
    ).join(Observation, Observation.observer_id == User.id)
    if selected_cycle_ids:
        calibration_q = calibration_q.filter(Observation.cycle_id.in_(selected_cycle_ids))
    calibration_q = calibration_q.group_by(User.id).having(db.func.count(Observation.id) >= 3)
    calibration_raw = calibration_q.all()
    observer_calibration = []
    for name, cnt, avg, var in calibration_raw:
        deviation = None
        if global_avg is not None and avg is not None:
            deviation = avg - global_avg
        observer_calibration.append({
            'name': name,
            'count': int(cnt or 0),
            'avg': float(avg) if avg is not None else None,
            'variance': float(var) if var is not None else None,
            'deviation': float(deviation) if deviation is not None else None,
        })
    observer_calibration.sort(key=lambda r: abs(r['deviation']) if r['deviation'] is not None else 0, reverse=True)
    observer_calibration = observer_calibration[:10]

    return render_template(
        'dashboard/index.html',
        total_staff=total_staff,
        total_cycles=total_cycles,
        total_observations=total_observations,
        no_obs_staff=no_obs_staff,
        observer_counts=observer_counts,
        top_tutors=top_tutors,
        concern_tutors=concern_tutors,
        branch_counts=branch_counts_items,
        dept_counts=dept_counts,
        score_summary=score_summary,
        recent_observations=recent_observations,
        weekly_trend=weekly_trend,
        cycle_stats=cycle_stats,
        overall_variance=overall_variance,
        cycle_variances=cycle_variances,
        observer_calibration=observer_calibration,
        selected_cycle_ids=selected_cycle_ids,
        all_cycles=all_cycles,
    welcome_name=current_user.name if current_user.is_authenticated else None,
    )

# ---------------- AUTH ----------------
@app.route("/login", methods=["GET","POST"])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.strip().lower()).first()
        if user and check_password_hash(user.password_hash, form.password.data):
            if not user.is_approved:
                return render_template("auth/pending.html")
            if hasattr(user, 'is_active') and not user.is_active:
                flash('Account is deactivated. Contact a superadmin.', 'danger')
                return render_template("auth/login.html", form=form)
            try:
                login_user(user, remember=bool(form.remember.data))
            except TypeError:
                login_user(user)
            return redirect(url_for('index'))
        flash("Invalid credentials", "danger")
    return render_template("auth/login.html", form=form)

@app.route("/register", methods=["GET","POST"])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        if User.query.filter_by(email=form.email.data.lower()).first():
            flash("Email already registered", "warning")
        else:
            u = User(name=form.name.data, email=form.email.data.lower(),
                     password_hash=generate_password_hash(form.password.data),
                     is_approved=False)
            db.session.add(u)
            db.session.commit()
            flash("Account created. Await superadmin approval.", "success")
            return redirect(url_for('login'))
    return render_template("auth/register.html", form=form)

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))
@app.route("/approve")
@login_required
def approve_users():
    if not current_user.is_superadmin:
        flash("Only superadmin can approve users", "warning")
        return redirect(url_for('index'))
    role_filter = request.args.get('role')
    q = User.query
    if role_filter and role_filter not in ('all',''):
        q = q.filter(User.role == role_filter)
    users = q.order_by(User.created_at.desc()).all()
    # Build role capability legend from current RolePermission table + descriptions
    known_roles = ['staff','supervisor','centre_manager','admin','superadmin']
    role_caps = []
    # Cache permission descriptions
    perm_desc = {p.key: p.description for p in Permission.query.all()}
    for r in known_roles:
        if r == 'superadmin':
            # Superadmin implicit all permissions
            role_caps.append({
                'key': r,
                'label': 'Management Member',
                'permissions': sorted([p.description or p.key for p in Permission.query.all()])
            })
            continue
        assigned = [rp.permission_key for rp in RolePermission.query.filter_by(role=r).all()]
        role_caps.append({
            'key': r,
            'label': {
                'staff':'Staff',
                'supervisor':'Supervisor',
                'centre_manager':'Centre Manager',
                'admin':'Admin',
                'superadmin':'Management Member'
            }.get(r, r.title()),
            'permissions': sorted([perm_desc.get(k, k) for k in assigned])
        })
    return render_template("auth/approve.html", users=users, role_caps=role_caps, role_filter=role_filter or 'all')

@app.route('/approve/bulk-role', methods=['POST'])
@login_required
def bulk_role_assign():
    if not current_user.is_superadmin:
        abort(403)
    ids_raw = request.form.get('user_ids','')
    target_role = (request.form.get('target_role') or '').strip().lower()
    legacy_alias = {'observer':'supervisor','lead':'centre_manager'}
    if target_role in legacy_alias:
        target_role = legacy_alias[target_role]
    if target_role not in ['staff','supervisor','centre_manager','admin','superadmin']:
        flash('Invalid target role for bulk assignment','danger')
        return redirect(url_for('approve_users'))
    try:
        ids = [int(x) for x in ids_raw.split(',') if x.strip().isdigit()]
    except Exception:
        ids = []
    if not ids:
        flash('No users selected for bulk assignment','warning')
        return redirect(url_for('approve_users'))
    updated = 0
    for uid in ids:
        u = User.query.get(uid)
        if not u:
            continue
        # Cannot demote another superadmin to non-superadmin unless we keep at least one superadmin (light check)
        if u.is_superadmin and target_role != 'superadmin':
            # Skip demotion of superadmin via bulk to avoid accidental lockout
            continue
        u.role = target_role
        if target_role == 'superadmin':
            u.is_superadmin = True
        updated += 1
    db.session.commit()
    flash(f'Bulk role assignment complete: {updated} user(s) set to {target_role}.','success')
    return redirect(url_for('approve_users', role=target_role))

@app.route("/approve/<int:uid>", methods=["POST"])
@login_required
def approve_user(uid):
    if not current_user.is_superadmin:
        flash("Only superadmin can approve", "warning")
        return redirect(url_for('approve_users'))
    u = User.query.get_or_404(uid)
    u.is_approved = True
    db.session.commit()
    flash(f"Approved {u.email}", "success")
    return redirect(url_for('approve_users'))

@app.route("/approve/<int:uid>/toggle_sa", methods=["POST"])
@login_required
def toggle_superadmin(uid):
    if not current_user.is_superadmin:
        flash("Only superadmin can toggle SA", "warning")
        return redirect(url_for('approve_users'))
    u = User.query.get_or_404(uid)
    u.is_superadmin = not u.is_superadmin
    db.session.commit()
    flash(f"Toggled superadmin for {u.email}", "success")
    return redirect(url_for('approve_users'))

@app.route('/approve/<int:uid>/role', methods=['POST'])
@login_required
def set_user_role(uid):
    if not current_user.is_superadmin:
        abort(403)
    u = User.query.get_or_404(uid)
    role = request.form.get('role','').strip().lower()
    legacy_alias = {'observer': 'supervisor', 'lead': 'centre_manager'}
    if role in legacy_alias:
        role = legacy_alias[role]
    if role not in ['staff','supervisor','centre_manager','admin','superadmin']:
        flash('Invalid role', 'warning')
        return redirect(url_for('approve_users'))
    # Prevent locking yourself out accidentally if demoting last superadmin
    if u.is_superadmin and role != 'superadmin':
        # allow but warn; we still have seeded SA ensured by before_request
        pass
    u.role = role
    if role == 'superadmin':
        u.is_superadmin = True
    db.session.commit()
    flash(f"Role for {u.email} set to {role}", 'success')
    return redirect(url_for('approve_users'))

@app.route('/approve/<int:uid>/delete', methods=['POST'])
@login_required
def delete_user(uid):
    """Delete a user (superadmin only).

    Safety guards:
    - Cannot delete yourself.
    - Cannot delete the last remaining superadmin.
    - Blocks deletion if the user has dependent records (observations, meetings, tasks, appointment slots/bookings, permission audits, todos) to avoid orphaned references.
    """
    if not current_user.is_superadmin:
        abort(403)
    user = User.query.get_or_404(uid)
    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'warning')
        return redirect(url_for('approve_users'))
    if user.is_superadmin:
        others = User.query.filter(User.is_superadmin, User.id != user.id).count()
        if others == 0:
            flash('Cannot delete the last superadmin user.', 'danger')
            return redirect(url_for('approve_users'))
    # Lightweight dependency checks (avoid accidental orphaning)
    dep_counts = 0
    try:
        from models import (AppointmentBooking, AppointmentSlot, Meeting,
                            Observation, PermissionAudit, Todo)
        dep_counts += Observation.query.filter_by(observer_id=user.id).count()
        dep_counts += Meeting.query.filter((Meeting.participant_id==user.id) | (Meeting.booked_by_id==user.id)).count()
        dep_counts += AppointmentSlot.query.filter((AppointmentSlot.superadmin_id==user.id) | (AppointmentSlot.created_by_id==user.id)).count()
        dep_counts += AppointmentBooking.query.join(AppointmentSlot).filter(AppointmentSlot.superadmin_id==user.id).count()
        dep_counts += Todo.query.filter((Todo.created_by_id==user.id) | (Todo.assigned_to_id==user.id)).count()
        dep_counts += PermissionAudit.query.filter((PermissionAudit.actor_user_id==user.id) | (PermissionAudit.target_user_id==user.id)).count()
    except Exception:
        # If any issues (circular import etc.), fall back to allowing deletion; but keep guard variable.
        pass
    if dep_counts > 0:
        flash('User cannot be deleted while linked records exist (observations, meetings, slots, bookings, tasks, audits). Reassign or remove those first.', 'warning')
        return redirect(url_for('approve_users'))
    email = user.email
    try:
        db.session.delete(user)
        db.session.commit()
        flash(f'User {email} deleted.', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to delete user: {exc}', 'danger')
    return redirect(url_for('approve_users'))

@app.route('/approve/<int:uid>/toggle_active', methods=['POST'])
@login_required
def toggle_user_active(uid):
    if not current_user.is_superadmin:
        abort(403)
    u = User.query.get_or_404(uid)
    if u.id == current_user.id and u.is_active is False:
        # allow re-activating self, but not deactivating self (avoid lockout)
        pass
    elif u.id == current_user.id and u.is_active is True:
        flash('You cannot deactivate your own account.', 'warning')
        return redirect(url_for('approve_users'))
    u.is_active = not bool(u.is_active)
    db.session.commit()
    flash(f"{'Activated' if u.is_active else 'Deactivated'} {u.email}", 'success')
    return redirect(url_for('approve_users'))

@app.route('/approve/<int:uid>/picture', methods=['POST'])
@login_required
def upload_user_picture(uid):
    if not current_user.is_superadmin and current_user.id != uid:
        abort(403)
    u = User.query.get_or_404(uid)
    file = request.files.get('picture')
    if not file or not file.filename:
        flash('No file uploaded', 'warning')
        return redirect(url_for('approve_users'))
    # Basic extension validation
    allowed_ext = {'.png','.jpg','.jpeg','.gif','.webp'}
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in allowed_ext:
        flash('Unsupported image format', 'danger')
        return redirect(url_for('approve_users'))
    # Save with unique name
    fname = f"u{uid}_{uuid4().hex}{ext}"
    path = os.path.join(app.root_path, 'static','uploads', fname)
    try:
        file.save(path)
        # Optionally delete old file if exists and local
        if u.picture:
            old_path = os.path.join(app.root_path,'static','uploads',u.picture)
            if os.path.isfile(old_path):
                try: os.remove(old_path)
                except Exception: pass
        u.picture = fname
        db.session.commit()
        flash('Picture updated', 'success')
    except Exception as e:
        flash(f'Upload failed: {e}', 'danger')
    return redirect(url_for('approve_users'))

# ---------------- Permission Management (0.9.0) ----------------
@app.route('/admin/role-permissions', methods=['GET','POST'])
@login_required
def role_permissions():
    if not current_user.is_superadmin:
        abort(403)
    # Dynamic roles (exclude superadmin which is implicit all-access)
    roles = sorted({r[0] for r in db.session.query(User.role).distinct() if r[0] and r[0] != 'superadmin'} | {'staff','supervisor','centre_manager','admin'})
    perms = Permission.query.order_by(Permission.key.asc()).all()
    if request.method == 'POST':

        # For each role/perm pair, expect checkbox name rp_<role>__<perm>
        existing = {(rp.role, rp.permission_key): rp for rp in RolePermission.query.all()}
        seen_keys = set()
        for role in roles:
            for p in perms:
                field = f"rp_{role}__{p.key}"
                want = field in request.form
                key = (role, p.key)
                if want and key not in existing:
                    db.session.add(RolePermission(role=role, permission_key=p.key))
                    try:
                        db.session.flush()
                        db.session.add(PermissionAudit(actor_user_id=current_user.id, role=role, permission_key=p.key, action='added'))
                    except Exception:
                        pass
                if not want and key in existing:
                    db.session.delete(existing[key])
                    try:
                        db.session.flush()
                        db.session.add(PermissionAudit(actor_user_id=current_user.id, role=role, permission_key=p.key, action='removed'))
                    except Exception:
                        pass
                seen_keys.add(key)
        db.session.commit()
        flash('Role permissions updated','success')
        return redirect(url_for('role_permissions'))
    role_map = {r.role: set() for r in RolePermission.query.with_entities(RolePermission.role).distinct()}
    for rp in RolePermission.query.all():
        role_map.setdefault(rp.role, set()).add(rp.permission_key)
    return render_template('admin/role_permissions.html', roles=roles, perms=perms, role_map=role_map)

@app.route('/admin/role-permissions/appointments', methods=['POST'])
@login_required
def role_permissions_appointments():
    """Quick update endpoint to adjust only the manage_appointments permission per role.

    Does not disturb other role permissions (unlike full matrix save which rewrites all).
    Superadmin only.
    """
    if not current_user.is_superadmin:
        abort(403)
    roles = sorted({r[0] for r in db.session.query(User.role).distinct() if r[0] and r[0] != 'superadmin'} | {'staff','supervisor','centre_manager','admin'})
    target_perm = 'manage_appointments'
    # Snapshot existing state
    existing = {rp.role: rp for rp in RolePermission.query.filter_by(permission_key=target_perm).all() if rp.role in roles}
    desired_roles = set(request.form.getlist('roles'))
    changed_any = False
    for role in roles:
        has_now = role in existing
        want = role in desired_roles
        if want and not has_now:
            db.session.add(RolePermission(role=role, permission_key=target_perm))
            try:
                db.session.flush()
                db.session.add(PermissionAudit(actor_user_id=current_user.id, role=role, permission_key=target_perm, action='added'))
            except Exception:
                pass
            changed_any = True
        if has_now and not want:
            db.session.delete(existing[role])
            try:
                db.session.flush()
                db.session.add(PermissionAudit(actor_user_id=current_user.id, role=role, permission_key=target_perm, action='removed'))
            except Exception:
                pass
            changed_any = True
    if changed_any:
        try:
            db.session.commit()
            flash('Appointment permission updated for selected roles.', 'success')
        except Exception as exc:
            db.session.rollback()
            flash(f'Failed to update appointment access: {exc}', 'danger')
    else:
        flash('No changes to appointment access.', 'info')
    return redirect(url_for('role_permissions'))

@app.route('/admin/user-permissions', methods=['GET','POST'])
@login_required
def user_permissions():
    if not current_user.is_superadmin:
        abort(403)
    user_id = request.args.get('user_id', type=int) or request.form.get('user_id', type=int)
    users = User.query.order_by(User.name.asc()).all()
    perms = Permission.query.order_by(Permission.key.asc()).all()
    selected_user = User.query.get(user_id) if user_id else None
    if request.method == 'POST' and selected_user:
        # For each permission, radio: inherit / allow / deny -> name=perm_<key> value=inherit|allow|deny
        existing = {up.permission_key: up for up in UserPermission.query.filter_by(user_id=selected_user.id).all()}
        for p in perms:
            val = request.form.get(f'perm_{p.key}')
            if val == 'inherit':
                if p.key in existing:
                    db.session.delete(existing[p.key])
                    try:
                        db.session.flush()
                        db.session.add(PermissionAudit(actor_user_id=current_user.id, target_user_id=selected_user.id, permission_key=p.key, action='inherit'))
                    except Exception:
                        pass
            elif val in ('allow','deny'):
                allow_flag = (val == 'allow')
                if p.key in existing:
                    old_allow = existing[p.key].allow
                    existing[p.key].allow = allow_flag
                    if old_allow != allow_flag:
                        try:
                            db.session.flush()
                            db.session.add(PermissionAudit(actor_user_id=current_user.id, target_user_id=selected_user.id, permission_key=p.key, action='allow' if allow_flag else 'deny'))
                        except Exception:
                            pass
                else:
                    db.session.add(UserPermission(user_id=selected_user.id, permission_key=p.key, allow=allow_flag))
                    try:
                        db.session.flush()
                        db.session.add(PermissionAudit(actor_user_id=current_user.id, target_user_id=selected_user.id, permission_key=p.key, action='allow' if allow_flag else 'deny'))
                    except Exception:
                        pass
        db.session.commit()
        flash('User permission overrides saved','success')
        return redirect(url_for('user_permissions', user_id=selected_user.id))
    overrides = {}
    if selected_user:
        overrides = {up.permission_key: up.allow for up in UserPermission.query.filter_by(user_id=selected_user.id).all()}
    return render_template('admin/user_permissions.html', users=users, selected_user=selected_user, perms=perms, overrides=overrides)

# ---------------- Appointment Admin ---------------- #

def _render_admin_appointments(slot_form: AppointmentSlotForm | None = None,
                               bulk_form: AppointmentSlotBulkForm | None = None):
    slot_form = slot_form or AppointmentSlotForm()
    bulk_form = bulk_form or AppointmentSlotBulkForm()
    _populate_superadmin_choices(slot_form)
    _populate_superadmin_choices(bulk_form)
    now = datetime.now(timezone.utc)
    slots = (AppointmentSlot.query
             .options(joinedload(AppointmentSlot.superadmin), joinedload(AppointmentSlot.bookings))
             .all())

    # Filters
    q = (request.args.get('q') or '').strip().lower()
    status_filter = (request.args.get('status') or '').lower()  # available|booked|inactive
    sa_filter = (request.args.get('sa') or '').strip()
    start_raw = (request.args.get('start') or '').strip()
    end_raw = (request.args.get('end') or '').strip()
    sort_key = (request.args.get('sort') or 'date').lower()  # date|member|status
    direction = (request.args.get('direction') or 'asc').lower()

    def slot_status(slot: AppointmentSlot):
        b = _active_booking(slot)
        if b:
            return 'booked'
        if not slot.is_active:
            return 'inactive'
        return 'available'

    def matches(slot: AppointmentSlot) -> bool:
        if status_filter in {'available','booked','inactive'} and slot_status(slot) != status_filter:
            return False
        if sa_filter and str(slot.superadmin_id) != sa_filter:
            return False
        if start_raw:
            try:
                sd = datetime.strptime(start_raw, '%Y-%m-%d').date()
                if slot.start_at.date() < sd:
                    return False
            except Exception:
                pass
        if end_raw:
            try:
                ed = datetime.strptime(end_raw, '%Y-%m-%d').date()
                if slot.start_at.date() > ed:
                    return False
            except Exception:
                pass
        if q:
            pieces = [slot.superadmin.name if slot.superadmin else '', slot.notes or '']
            bk = _active_booking(slot)
            if bk:
                pieces.extend([bk.name or '', bk.student_ref or '', bk.email or '', bk.reason or ''])
            if q not in ' '.join(pieces).lower():
                return False
        return True

    filtered = [s for s in slots if matches(s)]

    # Sorting
    def sort_value(slot: AppointmentSlot):
        if sort_key == 'member':
            return (slot.superadmin.name.lower() if slot.superadmin else '')
        if sort_key == 'status':
            return slot_status(slot)
        return slot.start_at

    filtered.sort(key=sort_value, reverse=(direction == 'desc'))
    # Ensure consistent timezone awareness for comparisons (some legacy rows may be naive)
    def _coerce_aware(dt: datetime):
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    upcoming = []
    past = []
    for s in filtered:
        end_at = _coerce_aware(s.end_at)
        if end_at >= now:
            upcoming.append(s)
        else:
            past.append(s)
    booked_upcoming = [s for s in upcoming if _active_booking(s)]
    available_upcoming = [s for s in upcoming if s.is_active and not _active_booking(s)]

    # Stats over all slots (not just filtered) for consistency with existing UI
    total_upcoming = 0
    total_available = 0
    total_booked = 0
    for s in slots:
        end_at = _coerce_aware(s.end_at)
        if end_at >= now:
            total_upcoming += 1
            if _active_booking(s):
                total_booked += 1
            elif s.is_active:
                total_available += 1
    stats = {
        'total': len(slots),
        'upcoming': total_upcoming,
        'available': total_available,
        'booked': total_booked,
    }

    filters = {
        'q': q,
        'status': status_filter,
        'sa': sa_filter,
        'start': start_raw,
        'end': end_raw,
        'sort': sort_key,
        'direction': direction,
    }

    slot_action_form = AppointmentSlotActionForm()
    booking_action_form = AppointmentBookingActionForm()
    superadmins = (User.query.filter_by(is_superadmin=True, is_approved=True)
                   .order_by(User.name.asc()).all())
    return render_template(
        'admin/appointments/index.html',
        slot_form=slot_form,
        bulk_form=bulk_form,
        slot_action_form=slot_action_form,
        booking_action_form=booking_action_form,
        stats=stats,
        upcoming_slots=upcoming,
        past_slots=past,
        now=now,
        filters=filters,
        superadmins=superadmins,
    )


@app.route('/admin/appointments')
@login_required
@permission_required('manage_appointments')
def admin_appointments():
    return _render_admin_appointments()


@app.route('/admin/appointments/create', methods=['POST'])
@login_required
@permission_required('manage_appointments')
def admin_appointments_create():
    form = AppointmentSlotForm()
    _populate_superadmin_choices(form)
    if form.validate_on_submit():
        start_at = _combine_datetime(form.date.data, form.start_time.data)
        end_at = _combine_datetime(form.date.data, form.end_time.data)
        if end_at <= start_at:
            form.end_time.errors.append('End time must be after start time.')
            return _render_admin_appointments(slot_form=form)
        if _slot_overlaps(form.superadmin_id.data, start_at, end_at):
            form.start_time.errors.append('This slot overlaps with an existing slot for the selected management team member.')
            return _render_admin_appointments(slot_form=form)
        slot = AppointmentSlot(
            superadmin_id=form.superadmin_id.data,
            created_by_id=current_user.id,
            start_at=start_at,
            end_at=end_at,
            is_active=bool(form.is_active.data),
            notes=form.notes.data.strip() if form.notes.data else None,
        )
        db.session.add(slot)
        db.session.commit()
        flash('Appointment slot created.', 'success')
        return redirect(url_for('admin_appointments'))
    flash('Please correct the errors below.', 'danger')
    return _render_admin_appointments(slot_form=form)


@app.route('/admin/appointments/bulk', methods=['POST'])
@login_required
@permission_required('manage_appointments')
def admin_appointments_bulk():
    form = AppointmentSlotBulkForm()
    _populate_superadmin_choices(form)
    if form.validate_on_submit():
        start_at = _combine_datetime(form.date.data, form.start_time.data)
        end_at = _combine_datetime(form.date.data, form.end_time.data)
        duration = timedelta(minutes=form.duration_minutes.data)
        if end_at <= start_at:
            form.end_time.errors.append('End time must be after start time.')
            return _render_admin_appointments(bulk_form=form)
        if duration <= timedelta(0):
            form.duration_minutes.errors.append('Duration must be greater than zero.')
            return _render_admin_appointments(bulk_form=form)
        cursor = start_at
        created = 0
        skipped = 0
        while cursor < end_at:
            slot_end = cursor + duration
            if slot_end > end_at:
                break
            if _slot_overlaps(form.superadmin_id.data, cursor, slot_end):
                skipped += 1
            else:
                slot = AppointmentSlot(
                    superadmin_id=form.superadmin_id.data,
                    created_by_id=current_user.id,
                    start_at=cursor,
                    end_at=slot_end,
                    is_active=True,
                    notes=form.notes.data.strip() if form.notes.data else None,
                )
                db.session.add(slot)
                created += 1
            cursor = slot_end
        if created:
            db.session.commit()
        else:
            db.session.rollback()
        flash(f'Bulk creation complete: {created} slot(s) created, {skipped} skipped.', 'success' if created else 'warning')
        return redirect(url_for('admin_appointments'))
    flash('Please correct the errors below.', 'danger')
    return _render_admin_appointments(bulk_form=form)


@app.route('/admin/appointments/<int:slot_id>/action', methods=['POST'])
@login_required
@permission_required('manage_appointments')
def admin_appointments_slot_action(slot_id: int):
    form = AppointmentSlotActionForm()
    if not form.validate_on_submit() or int(form.slot_id.data) != slot_id:
        flash('Invalid slot action request.', 'danger')
        return redirect(url_for('admin_appointments'))
    slot = (AppointmentSlot.query
            .options(joinedload(AppointmentSlot.superadmin), joinedload(AppointmentSlot.bookings))
            .get_or_404(slot_id))
    action = (form.action.data or '').lower()
    booking = _active_booking(slot)
    if action == 'toggle':
        if booking and slot.is_active:
            flash('Cannot deactivate a slot that already has a booking. Cancel the booking first.', 'warning')
        else:
            slot.is_active = not slot.is_active
            db.session.commit()
            state = 'activated' if slot.is_active else 'deactivated'
            flash(f'Slot {state}.', 'success')
    elif action == 'cancel':
        slot.is_active = False
        if booking and booking.is_active():
            booking.status = 'cancelled'
            booking.cancelled_at = datetime.now(timezone.utc)
            _cancel_reminder(booking.id)
            if slot.start_at > datetime.now(timezone.utc):
                slot.is_active = False
            db.session.commit()
            subj, html = build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='cancelled', cancel_url=None)
            _send_email_safe(booking.email, subj, html, log_prefix='Appointment cancellation')
            admin_subj, admin_html = build_appointment_admin_email(booking, slot, mode='cancelled_admin')
            _send_email_safe(slot.superadmin.email, admin_subj, admin_html, log_prefix='Appointment admin cancellation')
            flash('Slot and associated booking cancelled.', 'success')
        else:
            db.session.commit()
            flash('Slot cancelled.', 'success')
    else:
        flash('Unsupported action.', 'danger')
    return redirect(url_for('admin_appointments'))


@app.route('/admin/appointments/bookings/action', methods=['POST'])
@login_required
@permission_required('manage_appointments')
def admin_appointments_booking_action():
    form = AppointmentBookingActionForm()
    if not form.validate_on_submit():
        flash('Invalid booking action request.', 'danger')
        return redirect(url_for('admin_appointments'))
    booking = (AppointmentBooking.query
               .options(joinedload(AppointmentBooking.slot).joinedload(AppointmentSlot.superadmin))
               .get_or_404(int(form.booking_id.data)))
    if form.action.data == 'cancel':
        if not booking.is_active():
            flash('Booking already cancelled.', 'info')
        else:
            booking.status = 'cancelled'
            booking.cancelled_at = datetime.now(timezone.utc)
            slot = booking.slot
            if slot and slot.start_at > datetime.now(timezone.utc):
                slot.is_active = True
            _cancel_reminder(booking.id)
            db.session.commit()
            subj, html = build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='cancelled', cancel_url=None)
            _send_email_safe(booking.email, subj, html, log_prefix='Appointment cancellation')
            admin_subj, admin_html = build_appointment_admin_email(booking, slot, mode='cancelled_admin')
            _send_email_safe(slot.superadmin.email, admin_subj, admin_html, log_prefix='Appointment admin cancellation')
            flash('Booking cancelled and attendee notified.', 'success')
    else:
        flash('Unsupported action.', 'danger')
    return redirect(url_for('admin_appointments'))


# ---------------- Public Booking ---------------- #


def _booking_context_lang(lang: str | None = None) -> tuple[str, dict]:
    language = lang or _current_booking_language()
    copy = _booking_copy(language)
    return language, copy


@app.route('/booking', methods=['GET', 'POST'])
def booking_index():
    lang, copy = _booking_context_lang()
    form = AppointmentBookingForm()
    _populate_booking_form(form)
    form.language.data = lang
    upcoming_slots = _upcoming_slots_query()
    available_choices = list(form.slot_id.choices)

    if request.method == 'POST' and form.validate_on_submit():
        # Use SQLAlchemy 2.0 style session.get (avoids legacy warning)
        slot = (db.session.get(AppointmentSlot, form.slot_id.data)
                if form.slot_id.data else None)
        if slot is None or not slot.is_available():
            form.slot_id.errors.append(copy.get('slot_taken') or 'Selected slot is no longer available.')
            _populate_booking_form(form)
            available_choices = list(form.slot_id.choices)
        else:
            # Create booking; explicitly seed cancel_token to avoid timing issues before flush
            booking = AppointmentBooking(
                slot_id=slot.id,
                name=form.name.data.strip(),
                student_ref=form.student_ref.data.strip(),
                reason=form.reason.data.strip(),
                email=form.email.data.strip(),
                phone=form.phone.data.strip(),
                language=lang,
                cancel_token=uuid4().hex,  # ensure present prior to flush
            )
            db.session.add(booking)
            db.session.flush()  # booking.id now available
            token = booking.cancel_token
            booking.cancel_url = url_for('booking_cancel', token=token, _external=True)

            cancel_url = booking.cancel_url
            subj, html = build_appointment_email(booking, slot, slot.superadmin, language=lang, mode='confirmation', cancel_url=cancel_url)
            _send_email_safe(booking.email, subj, html, log_prefix='Appointment confirmation')
            booking.confirmation_sent_at = datetime.now(timezone.utc)

            admin_subj, admin_html = build_appointment_admin_email(booking, slot, mode='confirmation')
            _send_email_safe(slot.superadmin.email, admin_subj, admin_html, log_prefix='Appointment admin confirmation')

            db.session.commit()
            _schedule_reminder(booking)

            return render_template(
                'booking/success.html',
                form=form,
                copy=copy,
                booking=booking,
                slot=slot,
                language=lang,
                supported_languages=SUPPORTED_LANGUAGES,
            )

    form_disabled = len(available_choices) == 0
    return render_template(
        'booking/index.html',
        form=form,
        copy=copy,
        language=lang,
        supported_languages=SUPPORTED_LANGUAGES,
        upcoming_slots=upcoming_slots,
        form_disabled=form_disabled,
    )


@app.route('/booking/lang/<lang_code>')
def booking_set_language(lang_code: str):
    _set_booking_language(lang_code)
    return redirect(url_for('booking_index'))


@app.route('/booking/cancel/<token>', methods=['GET', 'POST'])
def booking_cancel(token: str):
    booking = (AppointmentBooking.query
               .options(joinedload(AppointmentBooking.slot).joinedload(AppointmentSlot.superadmin))
               .filter_by(cancel_token=token)
               .first())
    if not booking:
        abort(404)

    _set_booking_language(booking.language)
    lang, copy = _booking_context_lang(booking.language)
    slot = booking.slot

    if request.method == 'POST':
        if booking.is_active():
            booking.status = 'cancelled'
            booking.cancelled_at = datetime.now(timezone.utc)
            if slot and slot.start_at > datetime.now(timezone.utc):
                slot.is_active = True
            _cancel_reminder(booking.id)
            db.session.commit()
            subj, html = build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='cancelled', cancel_url=None)
            _send_email_safe(booking.email, subj, html, log_prefix='Appointment cancellation')
            admin_subj, admin_html = build_appointment_admin_email(booking, slot, mode='cancelled_user')
            _send_email_safe(slot.superadmin.email, admin_subj, admin_html, log_prefix='Appointment admin cancellation')
            return render_template(
                'booking/cancelled.html',
                copy=copy,
                booking=booking,
                slot=slot,
                language=lang,
                supported_languages=SUPPORTED_LANGUAGES,
                already=False,
            )
        return render_template(
            'booking/cancelled.html',
            copy=copy,
            booking=booking,
            slot=slot,
            language=lang,
            supported_languages=SUPPORTED_LANGUAGES,
            already=True,
        )

    if not booking.is_active():
        return render_template(
            'booking/cancelled.html',
            copy=copy,
            booking=booking,
            slot=slot,
            language=lang,
            supported_languages=SUPPORTED_LANGUAGES,
            already=True,
        )

    if not slot:
        abort(404)

    date_label = slot.start_at.strftime('%d %B %Y')
    time_label = f"{slot.start_at.strftime('%H:%M')} – {slot.end_at.strftime('%H:%M')}"
    return render_template(
        'booking/cancel.html',
        copy=copy,
        booking=booking,
        slot=slot,
        date_label=date_label,
        time_label=time_label,
        language=lang,
        supported_languages=SUPPORTED_LANGUAGES,
    )

@app.route('/profile', methods=['GET','POST'])
@login_required
def profile():
    form = UserProfileForm()
    user = current_user
    # Lightweight async theme update path (sent from base.html dropdown)
    if request.method == 'POST' and request.form.get('_theme_update') == '1':
        pref = (request.form.get('theme_pref') or 'system').lower()
        if pref not in {'light','dark','system'}:
            pref = 'system'
        try:
            user.theme_preference = pref
            db.session.commit()
        except Exception:
            db.session.rollback()
        return ('', 204)
    if request.method == 'GET':
        form.name.data = user.name
        form.email.data = user.email
        form.role.data = user.role or 'staff'
        form.is_approved.data = user.is_approved
        form.is_superadmin.data = user.is_superadmin
        form.theme_preference.data = user.theme_preference or 'system'
    if form.validate_on_submit():
        # Email change: ensure not taken by another
        new_email = form.email.data.strip().lower()
        if new_email != user.email:
            if User.query.filter(User.email==new_email, User.id!=user.id).first():
                flash('Email already in use', 'danger')
                return redirect(url_for('profile'))
            user.email = new_email
        user.name = form.name.data.strip()
        # Only superadmin can change role / approval / superadmin flags
        if current_user.is_superadmin:
            user.role = form.role.data
            user.is_approved = form.is_approved.data
            user.is_superadmin = form.is_superadmin.data
        # Theme preference (any user can change their own)
        user.theme_preference = form.theme_preference.data or 'system'
        # Password update if provided
        if form.password.data:
            user.password_hash = generate_password_hash(form.password.data)
        db.session.commit()
        flash('Profile updated', 'success')
        return redirect(url_for('profile'))
    # Build effective permission breakdown
    all_perms = Permission.query.order_by(Permission.key.asc()).all()
    effective = []
    for p in all_perms:
        source = 'role'
        allowed = False
        if current_user.is_superadmin:
            allowed = True; source = 'superadmin'
        else:
            up = UserPermission.query.filter_by(user_id=user.id, permission_key=p.key).first()
            if up:
                allowed = bool(up.allow)
                source = 'override-allow' if up.allow else 'override-deny'
            else:
                role_key = user.role or 'staff'
                rp = RolePermission.query.filter_by(role=role_key, permission_key=p.key).first()
                if rp:
                    allowed = True
                    source = 'role'
        effective.append({'key': p.key, 'description': p.description, 'allowed': allowed, 'source': source})
    # Load recent permission audit involving this user (as actor or target)
    audits = PermissionAudit.query.filter((PermissionAudit.target_user_id==user.id) | (PermissionAudit.actor_user_id==user.id)).order_by(PermissionAudit.changed_at.desc()).limit(50).all()
    # Short changelog (current version block)
    changelog_block = ''
    try:
        from version_info import VERSION, get_changelog
        full = get_changelog()
        if full:
            lines = full.splitlines()
            capture=False
            for line in lines:
                if line.startswith(f'## {VERSION} '):
                    capture=True
                    changelog_block += line + '\n'
                    continue
                if capture and line.startswith('## '):
                    break
                if capture:
                    changelog_block += line + '\n'
    except Exception:
        pass
    return render_template('auth/profile.html', form=form, user=user, effective_permissions=effective, permission_audits=audits, changelog_block=changelog_block.strip())

# Password reset
@app.route("/reset", methods=["GET","POST"])
def request_reset():
    if request.method == 'POST':
        email = request.form.get('email','').strip().lower()
        user = User.query.filter_by(email=email).first()
        if user:
            token = ts.dumps(email, salt=SECURITY_SALT)
            link = url_for('reset_with_token', token=token, _external=True)
            html = f"""
            <h3>Password reset</h3>
            <p>Hello {user.name},</p>
            <p>Click the link below to set a new password:</p>
            <p><a href='{link}'>{link}</a></p>
            <p>If you didn't request this, you can ignore this email.</p>
            """
            try:
                send_email(email, "Reset your password", html)
                flash("Reset link sent to your email.", "success")
            except Exception as e:
                flash(f"Email send failed: {e}", "danger")
        else:
            flash("If the email exists, a reset link will be sent.", "info")
    return render_template("auth/request_reset.html")

@app.route("/reset/<token>", methods=["GET","POST"])
def reset_with_token(token):
    try:
        email = ts.loads(token, salt=SECURITY_SALT, max_age=3600)
    except (BadSignature, SignatureExpired):
        flash("Invalid or expired token", "danger")
        return redirect(url_for('request_reset'))
    user = User.query.filter_by(email=email).first_or_404()
    if request.method == 'POST':
        pwd = request.form.get('password')
        user.password_hash = generate_password_hash(pwd)
        db.session.commit()
        flash("Password updated. Please login.", "success")
        return redirect(url_for('login'))
    return render_template("auth/reset_with_token.html")

# ---------------- STAFF CRUD + Import/Export ----------------
@app.route("/staff")
@login_required
def staff_index():
    q = Staff.query
    # Multi-select branch (?branch=Whitechapel&branch=Stratford) and department (?department=Science)
    branches_selected = [b for b in request.args.getlist('branch') if b]
    if not branches_selected:
        # Fallback support legacy comma param 'branches'
        legacy = request.args.get('branches', '')
        if legacy:
            branches_selected = [b for b in legacy.split(',') if b]
    if branches_selected:
        q = q.filter(or_(*[Staff.branch.like(f"%{b}%") for b in branches_selected]))

    departments_selected = [d for d in request.args.getlist('department') if d]
    if departments_selected:
        q = q.filter(Staff.department.in_(departments_selected))

    # Active status filter (?active=1 or ?active=0, allow multi but treat latest or first)
    active_filters = [v for v in request.args.getlist('active') if v in ('0','1')]
    if active_filters:
        # If both provided, do nothing (shows all); else filter
        uniq = set(active_filters)
        if len(uniq) == 1:
            want_active = list(uniq)[0] == '1'
            q = q.filter(Staff.active == want_active)
    staff = q.order_by(Staff.name.asc()).all()
    # Distinct department list for filter
    dept_choices = [r[0] for r in db.session.query(Staff.department).distinct().filter(Staff.department.isnot(None)).order_by(Staff.department.asc()).all()]
    return render_template(
        "staff/index.html",
        staff=staff,
        branch_choices=BRANCH_CHOICES,
        selected_branches=branches_selected,
        departments=dept_choices,
        selected_departments=departments_selected,
        selected_active=list(dict.fromkeys(active_filters)),
    )

@app.route("/staff/new", methods=["GET","POST"])
@login_required
def staff_new():
    form = StaffForm()
    # Populate department select with existing distinct departments
    dept_rows = [r[0] for r in db.session.query(Staff.department).distinct().filter(Staff.department.isnot(None)).order_by(Staff.department.asc()).all()]
    form.department.choices = [('', '-- None --')] + [(d, d) for d in dept_rows]
    if form.validate_on_submit():
        branches = ",".join(form.branches.data) if form.branches.data else ""
        s = Staff(name=form.name.data, department=form.department.data,
                  email=form.email.data, phone=form.phone.data, branch=branches, active=form.active.data)
        db.session.add(s)
        db.session.commit()
        flash("Staff saved", "success")
        return redirect(url_for('staff_index'))
    return render_template("staff/form.html", form=form, staff=None)

@app.route("/staff/<int:sid>/edit", methods=["GET","POST"])
@login_required
def staff_edit(sid):
    s = Staff.query.get_or_404(sid)
    form = StaffForm()
    # Populate department choices
    dept_rows = [r[0] for r in db.session.query(Staff.department).distinct().filter(Staff.department.isnot(None)).order_by(Staff.department.asc()).all()]
    form.department.choices = [('', '-- None --')] + [(d, d) for d in dept_rows]
    if request.method == 'GET':
        form.name.data = s.name
        form.department.data = s.department
        form.email.data = s.email
        form.phone.data = s.phone
        form.branches.data = [b for b in (s.branch or '').split(',') if b]
        form.active.data = s.active
    if form.validate_on_submit():
        s.name = form.name.data
        s.department = form.department.data
        s.email = form.email.data
        s.phone = form.phone.data
        s.branch = ",".join(form.branches.data) if form.branches.data else ""
        s.active = form.active.data
        db.session.commit()
        flash("Staff updated", "success")
        return redirect(url_for('staff_index'))
    return render_template("staff/form.html", form=form, staff=s)

@app.route("/staff/<int:sid>/delete")
@login_required
def staff_delete(sid):
    s = Staff.query.get_or_404(sid)
    db.session.delete(s)
    db.session.commit()
    flash("Staff deleted", "success")
    return redirect(url_for('staff_index'))

@app.route('/staff/<int:sid>/toggle')
@login_required
def staff_toggle_active(sid):
    s = Staff.query.get_or_404(sid)
    s.active = not bool(s.active)
    db.session.commit()
    flash(f"{'Activated' if s.active else 'Deactivated'} {s.name}", 'success')
    # Preserve filters (except pagination) by redirecting back
    return redirect(request.referrer or url_for('staff_index'))

@app.route('/staff/<int:sid>')
@login_required
def staff_detail(sid: int):
    """Detail view for a staff member including recent change log (up to 200 entries)."""
    from models import StaffChange  # local import to avoid circular issues
    staff = Staff.query.get_or_404(sid)
    # Filters
    field_filter = (request.args.get('field') or '').strip()
    actor_filter = request.args.get('actor', type=int)
    start_date_raw = (request.args.get('start') or '').strip()
    end_date_raw = (request.args.get('end') or '').strip()
    page = max(request.args.get('page', type=int, default=1), 1)
    per_page = min(request.args.get('per_page', type=int, default=25), 200)

    q = StaffChange.query.filter_by(staff_id=staff.id)
    if field_filter:
        q = q.filter(StaffChange.field == field_filter)
    if actor_filter:
        q = q.filter(StaffChange.changed_by_id == actor_filter)
    from datetime import datetime as _dt
    date_fmt = '%Y-%m-%d'
    if start_date_raw:
        try:
            sd = _dt.strptime(start_date_raw, date_fmt)
            q = q.filter(StaffChange.changed_at >= sd)
        except Exception:
            pass
    if end_date_raw:
        try:
            ed = _dt.strptime(end_date_raw, date_fmt)
            q = q.filter(StaffChange.changed_at < ed + timedelta(days=1))
        except Exception:
            pass
    # Inline export support (?export=1) so template can degrade gracefully if dedicated route not yet registered.
    if request.args.get('export'):
        rows = q.order_by(StaffChange.changed_at.desc()).all()
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['when','field','old_value','new_value','actor'])
        for r in rows:
            actor_name = r.changed_by.name if getattr(r, 'changed_by', None) else ''
            when_str = r.changed_at.strftime('%Y-%m-%d %H:%M:%S') if r.changed_at else ''
            writer.writerow([when_str, r.field, r.old_value or '', r.new_value or '', actor_name])
        mem = io.BytesIO(out.getvalue().encode('utf-8'))
        mem.seek(0)
        fname = f"staff_{staff.id}_changes.csv"
        return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=fname)

    total = q.count()
    changes = (q.order_by(StaffChange.changed_at.desc())
                .offset((page-1)*per_page)
                .limit(per_page)
                .all())
    total_pages = (total // per_page) + (1 if total % per_page else 0)
    # Distinct fields & actors for filter selects
    distinct_fields = [r[0] for r in db.session.query(StaffChange.field).filter_by(staff_id=staff.id).distinct().order_by(StaffChange.field.asc()).all()]
    actor_rows = (db.session.query(User.id, User.name)
                  .join(StaffChange, StaffChange.changed_by_id == User.id)
                  .filter(StaffChange.staff_id == staff.id)
                  .distinct().order_by(User.name.asc()).all())
    return render_template('staff/detail.html', staff=staff, changes=changes,
                           field_filter=field_filter, actor_filter=actor_filter,
                           start_date=start_date_raw, end_date=end_date_raw,
                           page=page, per_page=per_page, total=total, total_pages=total_pages,
                           distinct_fields=distinct_fields, actor_rows=actor_rows)

@app.route('/staff/<int:sid>/changes/export')
@login_required
def staff_changes_export(sid: int):
    """Export (filtered) staff change log as CSV."""
    from models import StaffChange
    staff = Staff.query.get_or_404(sid)
    field_filter = (request.args.get('field') or '').strip()
    actor_filter = request.args.get('actor', type=int)
    start_date_raw = (request.args.get('start') or '').strip()
    end_date_raw = (request.args.get('end') or '').strip()
    q = StaffChange.query.filter_by(staff_id=staff.id)
    if field_filter:
        q = q.filter(StaffChange.field == field_filter)
    if actor_filter:
        q = q.filter(StaffChange.changed_by_id == actor_filter)
    from datetime import datetime as _dt
    date_fmt = '%Y-%m-%d'
    if start_date_raw:
        try:
            sd = _dt.strptime(start_date_raw, date_fmt)
            q = q.filter(StaffChange.changed_at >= sd)
        except Exception:
            pass
    if end_date_raw:
        try:
            ed = _dt.strptime(end_date_raw, date_fmt)
            q = q.filter(StaffChange.changed_at < ed + timedelta(days=1))
        except Exception:
            pass
    rows = q.order_by(StaffChange.changed_at.desc()).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(['when','field','old_value','new_value','actor'])
    for r in rows:
        actor_name = r.changed_by.name if getattr(r, 'changed_by', None) else ''
        when_str = r.changed_at.strftime('%Y-%m-%d %H:%M:%S') if r.changed_at else ''
        writer.writerow([when_str, r.field, r.old_value or '', r.new_value or '', actor_name])
    mem = io.BytesIO(out.getvalue().encode('utf-8'))
    mem.seek(0)
    fname = f"staff_{staff.id}_changes.csv"
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name=fname)

# ---------------- Students Module ---------------- #

@app.route('/students')
@login_required
@permission_required('manage_students')
def students_index():
    # Filters
    q = (request.args.get('q') or '').lower().strip()
    year_filter = (request.args.get('year') or '').strip()
    status_filter = (request.args.get('status') or '').strip()
    query = Student.query
    # Sorting
    sort = request.args.get('sort') or 'id'
    direction = request.args.get('direction') or 'asc'
    # Provide deterministic multi-key default: id asc, status Active first
    # We treat "active" concept as status == 'Active'; emulate by ordering on a boolean expression
    active_first = case((Student.status == 'Active', 0), else_=1)
    if sort == 'name':
        primary = Student.name.asc() if direction == 'asc' else Student.name.desc()
    elif sort == 'student_id':
        primary = Student.student_id.asc() if direction == 'asc' else Student.student_id.desc()
    elif sort == 'year':
        primary = Student.year.asc() if direction == 'asc' else Student.year.desc()
    elif sort == 'status':
        primary = Student.status.asc() if direction == 'asc' else Student.status.desc()
    else:
        sort = 'id'
        primary = Student.id.asc() if direction == 'asc' else Student.id.desc()
    # Default ordering always layers active_first then the chosen primary key then id tie-breaker
    query = query.order_by(active_first, primary, Student.id.asc())
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Student.name.ilike(like), Student.student_id.ilike(like), Student.address.ilike(like)))
    if year_filter:
        query = query.filter(Student.year == year_filter)
    if status_filter:
        query = query.filter(Student.status == status_filter)
    # Pagination (server-side) if dataset large
    try:
        page = max(int(request.args.get('page', 1)), 1)
        per_page = min(max(int(request.args.get('per_page', 100)), 1), 500)
    except ValueError:
        page, per_page = 1, 100
    total = query.count()
    students = (query.offset((page-1)*per_page)
                     .limit(per_page)
                     .all())
    # Distinct years & statuses for filter selects
    years = [r[0] for r in db.session.query(Student.year).distinct().filter(Student.year.isnot(None)).order_by(Student.year.asc()).all()]
    statuses = [r[0] for r in db.session.query(Student.status).distinct().filter(Student.status.isnot(None)).order_by(Student.status.asc()).all()]
    form = StudentForm()
    total_pages = (total // per_page) + (1 if total % per_page else 0)
    return render_template('students/index.html', students=students, form=form, years=years, statuses=statuses, year_filter=year_filter, status_filter=status_filter, q=q, total=total, page=page, per_page=per_page, total_pages=total_pages, sort=sort, direction=direction)

@app.route('/students/create', methods=['POST'])
@login_required
@permission_required('manage_students')
def students_create():
    form = StudentForm()
    if form.validate_on_submit():
        existing = Student.query.filter_by(student_id=form.student_id.data.strip()).first()
        if existing:
            flash('Student ID already exists', 'warning')
        else:
            s = Student(
                student_id=form.student_id.data.strip(),
                name=form.name.data.strip(),
                type=form.type.data.strip() if form.type.data else None,
                year=form.year.data.strip() if form.year.data else None,
                email=form.email.data.strip() if form.email.data else None,
                phone=form.phone.data.strip() if form.phone.data else None,
                address=form.address.data.strip() if form.address.data else None,
                academic=form.academic.data.strip() if form.academic.data else None,
                status=form.status.data.strip() if form.status.data else None,
            )
            db.session.add(s); db.session.commit()
            flash('Student created','success')
    else:
        flash('Please correct errors','danger')
    return redirect(url_for('students_index'))

def _log_student_changes(student, changed_by, before: dict, after: dict):
    for field, old_val in before.items():
        new_val = after.get(field)
        if old_val != new_val:
            db.session.add(StudentChange(student_id=student.id, field=field, old_value=str(old_val) if old_val is not None else None, new_value=str(new_val) if new_val is not None else None, changed_by_id=changed_by.id))

@app.route('/students/<int:sid>')
@login_required
@permission_required('manage_students')
def students_detail(sid):
    student = Student.query.get_or_404(sid)
    changes = StudentChange.query.filter_by(student_id=student.id).order_by(StudentChange.changed_at.desc()).limit(200).all()
    form = StudentForm(obj=student)
    return render_template('students/detail.html', student=student, changes=changes, form=form)

@app.route('/students/<int:sid>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_students')
def students_edit(sid):
    student = Student.query.get_or_404(sid)
    if request.method == 'GET':
        return redirect(url_for('students_detail', sid=student.id))
    form = StudentForm()
    if form.validate_on_submit():
        # Uniqueness check if student_id changed
        new_sid = form.student_id.data.strip()
        if new_sid != student.student_id and Student.query.filter_by(student_id=new_sid).first():
            flash('Student ID already exists','warning')
            return redirect(url_for('students_detail', sid=student.id))
        before = {
            'student_id': student.student_id,
            'name': student.name,
            'type': student.type,
            'year': student.year,
            'email': student.email,
            'phone': student.phone,
            'address': student.address,
            'academic': student.academic,
            'status': student.status,
        }
        student.student_id = new_sid
        student.name = form.name.data.strip()
        student.type = form.type.data.strip() if form.type.data else None
        student.year = form.year.data.strip() if form.year.data else None
        student.email = form.email.data.strip() if form.email.data else None
        student.phone = form.phone.data.strip() if form.phone.data else None
        student.address = form.address.data.strip() if form.address.data else None
        student.academic = form.academic.data.strip() if form.academic.data else None
        student.status = form.status.data.strip() if form.status.data else None
        after = {
            'student_id': student.student_id,
            'name': student.name,
            'type': student.type,
            'year': student.year,
            'email': student.email,
            'phone': student.phone,
            'address': student.address,
            'academic': student.academic,
            'status': student.status,
        }
        _log_student_changes(student, current_user, before, after)
        db.session.commit()
        flash('Student updated','success')
    else:
        flash('Please correct errors','danger')
    return redirect(url_for('students_detail', sid=student.id))

@app.route('/students/<int:sid>/delete', methods=['POST'])
@login_required
@permission_required('manage_students')
def students_delete(sid):
    student = Student.query.get_or_404(sid)
    db.session.delete(student)
    db.session.commit()
    flash('Student deleted','info')
    return redirect(url_for('students_index'))

@app.route('/students/bulk', methods=['POST'])
@login_required
@permission_required('manage_students')
def students_bulk():
    action = request.form.get('action')
    ids = request.form.getlist('ids')
    if not action or not ids:
        flash('No bulk action performed','warning')
        return redirect(request.referrer or url_for('students_index'))
    q = Student.query.filter(Student.id.in_(ids))
    count = 0
    if action == 'activate':
        for s in q:
            if s.status != 'Active':
                s.status = 'Active'; count += 1
    elif action == 'inactivate':
        for s in q:
            if s.status != 'Inactive':
                s.status = 'Inactive'; count += 1
    else:
        flash('Unknown action','danger')
        return redirect(request.referrer or url_for('students_index'))
    db.session.commit()
    flash(f'Bulk update applied to {count} students','success')
    return redirect(request.referrer or url_for('students_index'))

@app.route('/students/import', methods=['POST'])
@login_required
@permission_required('manage_students')
def students_import():
    file = request.files.get('file')
    if not file or not file.filename:
        flash('No file provided','warning'); return redirect(url_for('students_index'))
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in {'.xlsx','.xls','.csv'}:
        flash('Unsupported file type','danger'); return redirect(url_for('students_index'))
    try:
        file.seek(0)
        if ext == '.csv':
            import pandas as pd
            df = pd.read_csv(file)
        else:
            import pandas as pd
            df = pd.read_excel(file)
    except Exception as e:
        flash(f'Failed to read file: {e}','danger'); return redirect(url_for('students_index'))
    # Normalize columns
    col_map = {c.lower().strip(): c for c in df.columns}
    wanted = ['student id','name','type','year','preferred contact','address','academic','status']
    processed = 0; updated = 0; created = 0
    for idx, row in df.iterrows():
        def val(key):
            lk = key.lower()
            if lk in col_map:
                return row[col_map[lk]] if not (isinstance(row[col_map[lk]], float) and pd.isna(row[col_map[lk]])) else None
            return None
        sid = str(val('student id') or '').strip()
        name = str(val('name') or '').strip()
        if not sid or not name:
            continue
        preferred_raw = val('preferred contact')
        email, phone = parse_preferred_contact(str(preferred_raw) if preferred_raw is not None else None)
        student = Student.query.filter_by(student_id=sid).first()
        if not student:
            student = Student(student_id=sid, name=name)
            db.session.add(student)
            created += 1
        else:
            updated += 1
        # Update fields (do not blank out if missing; only overwrite if provided)
        student.name = name or student.name
        year_val = val('year'); student.year = str(year_val).strip() if year_val else student.year
        address_val = val('address'); student.address = str(address_val).strip() if address_val else student.address
        status_val = val('status'); student.status = str(status_val).strip() if status_val else student.status
        type_val = val('type'); student.type = str(type_val).strip() if type_val else student.type
        acad_val = val('academic'); student.academic = str(acad_val).strip() if acad_val else student.academic
        student.preferred_contact_raw = preferred_raw if preferred_raw is not None else student.preferred_contact_raw
        if email: student.email = email
        if phone: student.phone = phone
        processed += 1
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback(); flash(f'Import failed: {e}','danger'); return redirect(url_for('students_index'))
    flash(f'Import complete: {processed} rows processed, {created} created, {updated} updated.','success')
    return redirect(url_for('students_index'))

@app.route('/students/export')
@login_required
@permission_required('manage_students')
def students_export():
    import pandas as pd
    rows = Student.query.order_by(Student.student_id.asc()).all()
    data = []
    for s in rows:
        data.append({
            'Student ID': s.student_id,
            'Name': s.name,
            'Type': s.type,
            'Year': s.year,
            'Preferred Contact': s.preferred_contact_raw or (s.email or '') + (' ' + s.phone if s.phone else ''),
            'Address': s.address,
            'Academic': s.academic,
            'Status': s.status,
            'Email Parsed': s.email,
            'Phone Parsed': s.phone,
        })
    df = pd.DataFrame(data)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Students')
    out.seek(0)
    return send_file(out, as_attachment=True, download_name='students_export.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

DEFAULT_TUITION_MATRIX = {
    'year3-5': { '1':85,'2':160,'3':235 },
    'year6-7': { '1':90,'2':170,'3':240,'4':300 },
    'year8': { '1':95,'2':175,'3':250,'4':330 },
    'year9': { '1':100,'2':185,'3':260,'4':350 },
    'year10': { '1':105,'2':200,'3':270,'4':370 },
    'year11': { '1':110,'2':210,'3':290,'4':380 },
    'alevel': { '1':165,'2':300,'3':430,'4':575 },
}

def _current_tuition_matrix():
    return get_setting('tuition_matrix', DEFAULT_TUITION_MATRIX, as_json=True)

def _save_tuition_matrix(matrix):
    set_setting('tuition_matrix', matrix, as_json=True)

@app.route('/tools/pricing', methods=['GET','POST'])
@login_required
@permission_required('manage_pricing')
def pricing_config():
    matrix = _current_tuition_matrix()
    form = PricingConfigForm()
    if request.method == 'GET':
        # populate form fields from matrix (safely)
        def get(m, key, sub):
            return m.get(key, {}).get(sub)
        form.year3_5_1.data = get(matrix,'year3-5','1'); form.year3_5_2.data = get(matrix,'year3-5','2'); form.year3_5_3.data = get(matrix,'year3-5','3')
        form.year6_7_1.data = get(matrix,'year6-7','1'); form.year6_7_2.data = get(matrix,'year6-7','2'); form.year6_7_3.data = get(matrix,'year6-7','3'); form.year6_7_4.data = get(matrix,'year6-7','4')
        form.year8_1.data = get(matrix,'year8','1'); form.year8_2.data = get(matrix,'year8','2'); form.year8_3.data = get(matrix,'year8','3'); form.year8_4.data = get(matrix,'year8','4')
        form.year9_1.data = get(matrix,'year9','1'); form.year9_2.data = get(matrix,'year9','2'); form.year9_3.data = get(matrix,'year9','3'); form.year9_4.data = get(matrix,'year9','4')
        form.year10_1.data = get(matrix,'year10','1'); form.year10_2.data = get(matrix,'year10','2'); form.year10_3.data = get(matrix,'year10','3'); form.year10_4.data = get(matrix,'year10','4')
        form.year11_1.data = get(matrix,'year11','1'); form.year11_2.data = get(matrix,'year11','2'); form.year11_3.data = get(matrix,'year11','3'); form.year11_4.data = get(matrix,'year11','4')
        form.alevel_1.data = get(matrix,'alevel','1'); form.alevel_2.data = get(matrix,'alevel','2'); form.alevel_3.data = get(matrix,'alevel','3'); form.alevel_4.data = get(matrix,'alevel','4')
        # Coerce registration fee (stored as plain string setting) into numeric for DecimalField rendering
        _raw_reg = get_setting('registration_fee', 25)
        try:
            form.registration_fee.data = float(_raw_reg) if _raw_reg is not None and _raw_reg != '' else None
        except Exception:
            form.registration_fee.data = None
        # Stationery prices
        try:
            form.writing_book_price.data = float(get_setting('writing_book_price', 1.50))
            form.planner_price.data = float(get_setting('planner_price', 3.00))
        except Exception:
            pass
        # Book catalog JSON (pretty-print stored JSON if present)
        # Stationery JSON
        stationery = get_setting('stationery_items', None, as_json=True)
        if stationery:
            import json as _json
            try:
                form.stationery_json.data = _json.dumps(stationery, indent=2, ensure_ascii=False)
            except Exception:
                form.stationery_json.data = ''
        # Deposit percent
        try:
            form.deposit_percent.data = float(get_setting('deposit_percent', 50))
        except Exception:
            pass
    if form.validate_on_submit():
        # build matrix from fields (only include non-null to avoid zero overriding when left blank)
        to_num = lambda x: float(x) if x is not None else None
        new_matrix = {
            'year3-5': {k:to_num(v) for k,v in [('1',form.year3_5_1.data),('2',form.year3_5_2.data),('3',form.year3_5_3.data)] if v is not None},
            'year6-7': {k:to_num(v) for k,v in [('1',form.year6_7_1.data),('2',form.year6_7_2.data),('3',form.year6_7_3.data),('4',form.year6_7_4.data)] if v is not None},
            'year8': {k:to_num(v) for k,v in [('1',form.year8_1.data),('2',form.year8_2.data),('3',form.year8_3.data),('4',form.year8_4.data)] if v is not None},
            'year9': {k:to_num(v) for k,v in [('1',form.year9_1.data),('2',form.year9_2.data),('3',form.year9_3.data),('4',form.year9_4.data)] if v is not None},
            'year10': {k:to_num(v) for k,v in [('1',form.year10_1.data),('2',form.year10_2.data),('3',form.year10_3.data),('4',form.year10_4.data)] if v is not None},
            'year11': {k:to_num(v) for k,v in [('1',form.year11_1.data),('2',form.year11_2.data),('3',form.year11_3.data),('4',form.year11_4.data)] if v is not None},
            'alevel': {k:to_num(v) for k,v in [('1',form.alevel_1.data),('2',form.alevel_2.data),('3',form.alevel_3.data),('4',form.alevel_4.data)] if v is not None},
        }
        _save_tuition_matrix(new_matrix)
        if form.registration_fee.data is not None:
            set_setting('registration_fee', float(form.registration_fee.data))
        if form.writing_book_price.data is not None:
            set_setting('writing_book_price', float(form.writing_book_price.data))
        if form.planner_price.data is not None:
            set_setting('planner_price', float(form.planner_price.data))
        if form.deposit_percent.data is not None:
            set_setting('deposit_percent', float(form.deposit_percent.data))
    # Book catalog JSON deprecated; no handling required
        # Stationery JSON parse
        raw_stationery = (form.stationery_json.data or '').strip()
        if raw_stationery:
            import json as _json
            try:
                parsed_s = _json.loads(raw_stationery)
                if isinstance(parsed_s, list):
                    set_setting('stationery_items', parsed_s, as_json=True)
                else:
                    flash('Stationery JSON must be a list of objects.', 'warning')
            except Exception as exc:
                flash(f'Invalid stationery JSON: {exc}', 'danger')
        flash('Pricing configuration updated','success')
        return redirect(url_for('pricing_config'))
    return render_template('tools/pricing.html', form=form, matrix=matrix, registration_fee=get_setting('registration_fee', 25))

@app.route('/tools/enroll')
@login_required
@permission_required('access_enrollment_tool')
def tool_enroll():
    tuition_matrix = _current_tuition_matrix()
    registration_fee = get_setting('registration_fee', 25)
    return render_template('tools/enroll.html', tuition_matrix=tuition_matrix, registration_fee=registration_fee)

@app.route('/api/tuition-matrix')
def api_tuition_matrix():
    return jsonify({
        'matrix': _current_tuition_matrix(),
        'registration_fee': get_setting('registration_fee', 25),
        'writing_book_price': get_setting('writing_book_price', 1.50),
        'planner_price': get_setting('planner_price', 3.00),
    'book_catalog': [b.serialize() for b in Book.query.filter_by(active=True).order_by(Book.name.asc()).all()],
        'stationery_items': get_setting('stationery_items', [], as_json=True),
        'deposit_percent': get_setting('deposit_percent', 50)
    })

@app.route('/tools/schedule-message', methods=['GET','POST'])
@login_required
@permission_required('access_enrollment_tool', any=True)
def tool_schedule_message():
    """Generate a formatted welcome/schedule message from pasted raw schedule export."""
    raw = ''
    result = None
    class_start_date = ''
    if request.method == 'POST':
        raw = request.form.get('raw','')
        class_start_date = (request.form.get('class_start_date') or '').strip()
        try:
            result = parse_schedule_message(raw, class_start_date=class_start_date or None)
            if class_start_date:
                result['class_start_date'] = class_start_date
        except Exception as exc:
            result = {'error': f'Parse failed: {exc}'}
    return render_template('tools/schedule_message.html', raw=raw, result=result, class_start_date=class_start_date)

# ---------------- Books CRUD (replaces JSON book_catalog) ---------------- #
@app.route('/tools/books')
@login_required
@permission_required('manage_books')
def books_index():
    # ---------------- Query Params ---------------- #
    q = (request.args.get('q') or '').strip().lower()
    year = (request.args.get('year') or '').strip().lower()
    subject = (request.args.get('subject') or '').strip().lower()
    status_param = (request.args.get('status') or '').strip().lower()  # '', 'active', 'hidden'
    # Backwards compatibility: legacy ?inactive=1 to show hidden
    legacy_inactive_flag = bool(request.args.get('inactive'))
    sort = (request.args.get('sort') or 'name').strip().lower()
    direction = (request.args.get('direction') or 'asc').strip().lower()
    page = max(1, int(request.args.get('page', 1) or 1))
    per_page = min(100, int(request.args.get('per_page', 25) or 25))

    # ---------------- Base Query & Filters ---------------- #
    base_q = Book.query
    if year:
        base_q = base_q.filter(Book.year_group == year)
    if subject:
        base_q = base_q.filter(db.func.lower(Book.subject) == subject)

    # Status filtering semantics:
    #   default (no status param provided) -> only active (historic behaviour)
    #   status=active  -> active only
    #   status=hidden  -> inactive only
    #   status=all     -> all (or legacy inactive=1 combined)
    if status_param in ('active', '') and not legacy_inactive_flag and status_param != 'all':
        # Blank string means 'default active only'
        base_q = base_q.filter(Book.active.is_(True))
    elif status_param == 'active':
        base_q = base_q.filter(Book.active.is_(True))
    elif status_param == 'hidden':
        base_q = base_q.filter(Book.active.is_(False))
    # else: status=all (or legacy show inactive) -> no active filter

    if q:
        like = f"%{q}%"
        base_q = base_q.filter(or_(Book.name.ilike(like), Book.subject.ilike(like)))

    # ---------------- Sorting ---------------- #
    sort_map = {
        'name': Book.name,
        'year': Book.year_group,
        'subject': Book.subject,
        'price': Book.price,
        'status': Book.active,
    }
    sort_col = sort_map.get(sort, Book.name)
    if direction == 'desc':
        sort_col = sort_col.desc()
    books_query = base_q.order_by(sort_col, Book.id.asc())  # deterministic tie-break

    total = books_query.count()
    books_rows = (books_query
                  .offset((page-1)*per_page)
                  .limit(per_page)
                  .all())
    books = [b.serialize() for b in books_rows]
    pages = (total // per_page) + (1 if total % per_page else 0)

    # Distinct subjects for filter dropdown
    subjects = [r[0] for r in db.session.query(Book.subject)
                               .distinct()
                               .filter(Book.subject.isnot(None))
                               .order_by(Book.subject.asc()).all()]

    per_page_choices = [10,25,50,100]
    return render_template('tools/books_index.html',
                           books=books,
                           page=page,
                           pages=pages,
                           total=total,
                           per_page=per_page,
                           per_page_choices=per_page_choices,
                           sort=sort,
                           direction=direction,
                           selected_year=year,
                           selected_subject=subject,
                           selected_status=status_param or ('all' if legacy_inactive_flag else 'active'),
                           q=q,
                           subjects=subjects)

@app.route('/tools/book-orders')
@login_required
@permission_required('order_books')
def book_orders_index():
    # Filters: q (book name / user), from, to, delivery date, has_delivery (1), year-month maybe; limit
    q = (request.args.get('q') or '').strip().lower()
    created_from_raw = (request.args.get('from') or '').strip()
    created_to_raw = (request.args.get('to') or '').strip()
    delivery_raw = (request.args.get('delivery') or '').strip()
    has_delivery = request.args.get('has_delivery')
    base = BookOrder.query.options(joinedload(BookOrder.items), joinedload(BookOrder.created_by))
    # Date parsing expecting DD-MM-YYYY
    def parse_dmy(s):
        try:
            return datetime.strptime(s, '%d-%m-%Y')
        except Exception:
            return None
    if created_from_raw:
        dt = parse_dmy(created_from_raw)
        if dt: base = base.filter(BookOrder.created_at >= dt)
    if created_to_raw:
        dt = parse_dmy(created_to_raw)
        if dt: base = base.filter(BookOrder.created_at < dt + timedelta(days=1))
    if delivery_raw:
        try:
            ddate = datetime.strptime(delivery_raw, '%d-%m-%Y').date()
            base = base.filter(BookOrder.delivery_date == ddate)
        except Exception:
            pass
    if has_delivery == '1':
        base = base.filter(BookOrder.delivery_date.isnot(None))
    if q:
        # simple client-side like filter after fetch (SQLite insensitive); fallback to python filter to include items
        all_rows = base.order_by(BookOrder.created_at.desc()).all()
        filtered = []
        for o in all_rows:
            hay = ' '.join([
                (o.created_by.name.lower() if o.created_by and o.created_by.name else ''),
                ' '.join(i.book_name.lower() for i in o.items)
            ])
            if q in hay:
                filtered.append(o)
        orders = filtered
    else:
        orders = base.order_by(BookOrder.created_at.desc()).all()
    return render_template('tools/book_orders_index.html', orders=orders, q=q, created_from=created_from_raw, created_to=created_to_raw, delivery=delivery_raw, has_delivery=has_delivery)

def _fmt_dmy(dt: datetime | date | None) -> str:
    if not dt: return ''
    try:
        if isinstance(dt, datetime):
            return dt.strftime('%d-%m-%Y')
        return dt.strftime('%d-%m-%Y')
    except Exception:
        return ''

def _build_book_order_email(order: BookOrder) -> tuple[str,str]:
    date_label = _fmt_dmy(datetime.utcnow())
    subject = f"Book Order {date_label}"
    deadline_label = _fmt_dmy(order.delivery_date) if order.delivery_date else 'N/A'
    rows = []
    for idx, item in enumerate(order.items, start=1):
        cover_btn = f"<a href='{item.cover_url}' style='color:#2563eb;text-decoration:none;'>Cover</a>" if item.cover_url else ''
        inner_btn = f"<a href='{item.inner_url}' style='color:#2563eb;text-decoration:none;'>Inner</a>" if item.inner_url else ''
        link_cells = ' '.join([c for c in [cover_btn, inner_btn] if c])
        rows.append(f"<tr><td style='padding:6px 8px;border:1px solid #e2e8f0;font-size:13px;'>{idx}</td>"
                    f"<td style='padding:6px 8px;border:1px solid #e2e8f0;font-size:13px;'>{item.book_name}</td>"
                    f"<td style='padding:6px 8px;border:1px solid #e2e8f0;font-size:13px;'>{item.print_format or ''}</td>"
                    f"<td style='padding:6px 8px;border:1px solid #e2e8f0;font-size:13px;'>{item.finishing or ''}</td>"
                    f"<td style='padding:6px 8px;border:1px solid #e2e8f0;font-size:13px;text-align:center;'>{item.quantity}</td>"
                    f"<td style='padding:6px 8px;border:1px solid #e2e8f0;font-size:13px;'>{link_cells}</td></tr>")
    table_html = ("<table role='presentation' cellpadding='0' cellspacing='0' style='border-collapse:collapse;border:1px solid #e2e8f0;width:100%;'>"
                  "<thead><tr style='background:#f1f5f9;'>"
                  "<th style='padding:6px 8px;border:1px solid #e2e8f0;font-size:12px;text-align:left;'>No.</th>"
                  "<th style='padding:6px 8px;border:1px solid #e2e8f0;font-size:12px;text-align:left;'>Name of Book</th>"
                  "<th style='padding:6px 8px;border:1px solid #e2e8f0;font-size:12px;text-align:left;'>Print Format</th>"
                  "<th style='padding:6px 8px;border:1px solid #e2e8f0;font-size:12px;text-align:left;'>Finishing</th>"
                  "<th style='padding:6px 8px;border:1px solid #e2e8f0;font-size:12px;'>Copies</th>"
                  "<th style='padding:6px 8px;border:1px solid #e2e8f0;font-size:12px;text-align:left;'>Links</th>"
                  "</tr></thead><tbody>" + ''.join(rows) + "</tbody></table>")
    intro = ("Please print the following books. The number of copies is indicated next to the name of the book. "
             "Please use the existing covers and print format.")
    html = f"""
    <html><body style='font-family:Arial,Helvetica,sans-serif;background:#f8fafc;padding:24px;'>
      <div style='max-width:760px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;'>
        <div style='background:#0f172a;padding:20px 24px;'>
          <h1 style='margin:0;font-size:20px;color:#ffffff;font-weight:600;'>Book Order</h1>
          <p style='margin:4px 0 0;font-size:12px;color:#94a3b8;'>Requested by {order.created_by.name}</p>
        </div>
        <div style='padding:28px;'>
          <p style='margin:0 0 18px;font-size:14px;color:#334155;'>{intro}</p>
          {table_html}
          <p style='margin:24px 0 0;font-size:14px;color:#334155;'>Please deliver all the orders by <strong>4 PM</strong> on <strong>{deadline_label}</strong>.</p>
        </div>
        <div style='background:#f1f5f9;padding:14px 24px;text-align:center;font-size:11px;color:#64748b;'>
          &copy; {datetime.utcnow().year} Excel Tutors
        </div>
      </div>
    </body></html>
    """
    return subject, html

@app.route('/tools/book-orders/create', methods=['POST'])
@login_required
@permission_required('order_books')
def book_orders_create():
    """Create a new book order from submitted JSON (AJAX) payload.

    Expected JSON: { delivery_date: 'YYYY-MM-DD', items: [ { book_id, quantity }, ... ] }
    Each item prompts user previously for quantity. We snapshot current book fields.
    """
    payload = request.get_json(silent=True) or {}
    delivery_raw = (payload.get('delivery_date') or '').strip()
    items_raw = payload.get('items') or []
    if not isinstance(items_raw, list) or not items_raw:
        app.logger.warning('book_orders_create: empty items payload=%r', payload)
        return jsonify({'success': False, 'error': 'No items supplied'}), 400
    delivery_date = None
    if delivery_raw:
        parsed = None
        for fmt in ('%Y-%m-%d','%d-%m-%Y'):  # accept both for UX flexibility
            try:
                parsed = datetime.strptime(delivery_raw, fmt).date()
                break
            except Exception:
                continue
        if not parsed:
            app.logger.warning('book_orders_create: invalid date raw=%r', delivery_raw)
            return jsonify({'success': False, 'error': 'Invalid delivery_date format (use DD-MM-YYYY or YYYY-MM-DD)'}), 400
        delivery_date = parsed
    order = BookOrder(created_by_id=current_user.id, delivery_date=delivery_date)
    db.session.add(order); db.session.flush()
    added = 0
    for it in items_raw:
        try:
            book_id = int(it.get('book_id'))
            qty = int(it.get('quantity') or 0)
        except Exception:
            continue
        if qty <= 0:
            continue
        book = Book.query.get(book_id)
        if not book:
            app.logger.warning('book_orders_create: book not found id=%s', book_id)
            continue
        snap = BookOrderItem(order_id=order.id, book_id=book.id, quantity=qty,
                              book_name=book.name, print_format=book.print_format,
                              finishing=book.finishing, cover_url=book.cover_url, inner_url=book.inner_url)
        db.session.add(snap); added += 1
    if added == 0:
        app.logger.warning('book_orders_create: no valid items after processing raw=%r', items_raw)
        db.session.rollback()
        return jsonify({'success': False, 'error': 'No valid items'}), 400
    # Build & send email
    try:
        subject, html = _build_book_order_email(order)
        send_email('techsupport@exceltutors.org.uk', subject, html)
        order.email_sent_at = datetime.utcnow()
    except Exception as exc:
        app.logger.error('book_orders_create: email send failed %s', exc, exc_info=True)
        db.session.rollback()
        return jsonify({'success': False, 'error': f'Email send failed: {exc}'}), 500
    db.session.commit()
    return jsonify({'success': True, 'order_id': order.id})

@app.route('/api/book-orders/create', methods=['POST'])
@login_required
@permission_required('order_books')
def api_book_orders_create_no_email():
    """Diagnostic endpoint: create order without sending email to isolate failures."""
    payload = request.get_json(silent=True) or {}
    items_raw = payload.get('items') or []
    if not isinstance(items_raw, list) or not items_raw:
        return jsonify({'success': False, 'error': 'No items supplied'}), 400
    order = BookOrder(created_by_id=current_user.id)
    db.session.add(order); db.session.flush()
    added = 0
    for it in items_raw:
        try:
            book_id = int(it.get('book_id'))
            qty = int(it.get('quantity') or 0)
        except Exception:
            continue
        if qty <= 0:
            continue
        book = Book.query.get(book_id)
        if not book:
            continue
        snap = BookOrderItem(order_id=order.id, book_id=book.id, quantity=qty,
                              book_name=book.name, print_format=book.print_format,
                              finishing=book.finishing, cover_url=book.cover_url, inner_url=book.inner_url)
        db.session.add(snap); added += 1
    if added == 0:
        db.session.rollback(); return jsonify({'success': False, 'error':'No valid items'}), 400
    db.session.commit()
    return jsonify({'success': True, 'order_id': order.id, 'email':'skipped'})

@app.route('/tools/book-orders/<int:order_id>')
@login_required
@permission_required('order_books')
def book_orders_detail(order_id: int):
    order = (BookOrder.query
             .options(joinedload(BookOrder.items), joinedload(BookOrder.created_by))
             .get_or_404(order_id))
    return render_template('tools/book_orders_detail.html', order=order)

@app.route('/tools/book-orders/<int:order_id>/resend', methods=['POST'])
@login_required
@permission_required('order_books')
def book_orders_resend(order_id: int):
    order = (BookOrder.query
             .options(joinedload(BookOrder.items), joinedload(BookOrder.created_by))
             .get_or_404(order_id))
    try:
        subject, html = _build_book_order_email(order)
        send_email('techsupport@exceltutors.org.uk', subject, html)
        order.email_sent_at = datetime.utcnow()
        db.session.commit()
        flash('Order email re-sent','success')
    except Exception as exc:
        db.session.rollback(); flash(f'Resend failed: {exc}','danger')
    return redirect(url_for('book_orders_detail', order_id=order.id))

@app.route('/tools/book-orders/export')
@login_required
@permission_required('order_books')
def book_orders_export_all():
    fmt = (request.args.get('format') or 'csv').lower()
    rows = BookOrder.query.options(joinedload(BookOrder.items), joinedload(BookOrder.created_by)).order_by(BookOrder.created_at.desc()).all()
    if fmt == 'pdf':
        # Simple aggregated PDF (list orders + items) minimal styling for fidelity
        from xhtml2pdf import pisa
        html_parts = ["<html><body style='font-family:Arial,sans-serif;font-size:11px;'>",
                      f"<h2 style='margin:0 0 12px;'>Book Orders Export ({len(rows)})</h2>"]
        for o in rows:
            html_parts.append(f"<h3 style='font-size:13px;margin:18px 0 4px;'>Order #{o.id} – {_fmt_dmy(o.created_at)} by {o.created_by.name if o.created_by else 'Unknown'}</h3>")
            html_parts.append("<table width='100%' cellspacing='0' cellpadding='0' style='border-collapse:collapse;margin-bottom:8px;'>"
                              "<thead><tr style='background:#f1f5f9;'>"
                              "<th style='border:1px solid #ccc;padding:4px;font-size:10px;text-align:left;'>#</th>"
                              "<th style='border:1px solid #ccc;padding:4px;font-size:10px;text-align:left;'>Book</th>"
                              "<th style='border:1px solid #ccc;padding:4px;font-size:10px;text-align:left;'>Format</th>"
                              "<th style='border:1px solid #ccc;padding:4px;font-size:10px;text-align:left;'>Finishing</th>"
                              "<th style='border:1px solid #ccc;padding:4px;font-size:10px;text-align:left;'>Qty</th>"
                              "</tr></thead><tbody>")
            for idx, it in enumerate(o.items, start=1):
                html_parts.append(
                    f"<tr><td style='border:1px solid #ccc;padding:4px;'>{idx}</td>"
                    f"<td style='border:1px solid #ccc;padding:4px;'>{it.book_name}</td>"
                    f"<td style='border:1px solid #ccc;padding:4px;'>{it.print_format or ''}</td>"
                    f"<td style='border:1px solid #ccc;padding:4px;'>{it.finishing or ''}</td>"
                    f"<td style='border:1px solid #ccc;padding:4px;'>{it.quantity}</td></tr>"
                )
            html_parts.append("</tbody></table>")
        html_parts.append("</body></html>")
        html = ''.join(html_parts)
        pdf_io = io.BytesIO()
        pisa.CreatePDF(io.StringIO(html), dest=pdf_io)  # type: ignore[arg-type]
        pdf_io.seek(0)
        return send_file(pdf_io, as_attachment=True, download_name='book_orders_export.pdf', mimetype='application/pdf')
    # CSV export
    out = io.StringIO()
    import csv as _csv
    writer = _csv.writer(out)
    writer.writerow(['order_id','created','created_by','delivery_date','book_name','print_format','finishing','quantity'])
    for o in rows:
        for it in o.items:
            writer.writerow([o.id, _fmt_dmy(o.created_at), (o.created_by.name if o.created_by else ''), _fmt_dmy(o.delivery_date), it.book_name, it.print_format or '', it.finishing or '', it.quantity])
    mem = io.BytesIO(out.getvalue().encode('utf-8'))
    mem.seek(0)
    return send_file(mem, as_attachment=True, download_name='book_orders_export.csv', mimetype='text/csv')

@app.route('/tools/book-orders/<int:order_id>/pdf')
@login_required
@permission_required('order_books')
def book_orders_pdf(order_id: int):
    from xhtml2pdf import pisa
    order = (BookOrder.query
             .options(joinedload(BookOrder.items), joinedload(BookOrder.created_by))
             .get_or_404(order_id))
    # Reuse email body content (strip container) or build minimal pdf template
    subject, html_email = _build_book_order_email(order)
    # Wrap in simpler PDF friendly HTML (avoid dark background)
    html = html_email
    pdf_io = io.BytesIO()
    try:
        pisa.CreatePDF(io.StringIO(html), dest=pdf_io)  # type: ignore[arg-type]
    except Exception as exc:
        flash(f'PDF generation failed: {exc}','danger')
        return redirect(url_for('book_orders_detail', order_id=order.id))
    pdf_io.seek(0)
    return send_file(pdf_io, as_attachment=True, download_name=f'book_order_{order.id}.pdf', mimetype='application/pdf')

@app.route('/tools/books/create', methods=['GET','POST'])
@login_required
@permission_required('manage_books')
def books_create():
    form = BookForm()
    if form.validate_on_submit():
        finishing_values = [p.strip() for p in (form.finishing.data.split(',') if form.finishing.data else []) if p.strip()]
        book = Book(
            name=form.name.data.strip(),
            price=float(form.price.data or 0),
            subject=form.subject.data.strip() if form.subject.data else None,
            year_group=form.year_group.data.strip() if form.year_group.data else None,
            cover=form.cover.data.strip() if form.cover.data else None,
            cover_url=form.cover_url.data.strip() if form.cover_url.data else None,
            inner=form.inner.data.strip() if form.inner.data else None,
            inner_url=form.inner_url.data.strip() if form.inner_url.data else None,
            print_format=form.print_format.data.strip() if form.print_format.data else None,
            finishing=','.join(finishing_values) or None,
            active=bool(form.active.data)
        )
        db.session.add(book); db.session.flush()
        # Normalized finishing options sync
        try:
            from models import FinishingOption
            book.finishing_options = []
            for label in finishing_values:
                fo = FinishingOption.query.filter(db.func.lower(FinishingOption.name)==label.lower()).first()
                if not fo:
                    fo = FinishingOption(name=label)
                    db.session.add(fo); db.session.flush()
                if fo not in book.finishing_options:
                    book.finishing_options.append(fo)
        except Exception:
            pass
        db.session.commit()
        flash('Book created','success')
        return redirect(url_for('books_index'))
    return render_template('tools/books_form.html', form=form, book=None)

@app.route('/tools/books/<int:book_id>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_books')
def books_edit(book_id: int):
    book = Book.query.get_or_404(book_id)
    form = BookForm(obj=book)
    if form.validate_on_submit():
        book.name = form.name.data.strip()
        book.price = float(form.price.data or 0)
        book.subject = form.subject.data.strip() if form.subject.data else None
        book.year_group = form.year_group.data.strip() if form.year_group.data else None
        book.cover = form.cover.data.strip() if form.cover.data else None
        book.cover_url = form.cover_url.data.strip() if form.cover_url.data else None
        book.inner = form.inner.data.strip() if form.inner.data else None
        book.inner_url = form.inner_url.data.strip() if form.inner_url.data else None
        book.print_format = form.print_format.data.strip() if form.print_format.data else None
        finishing_values = [p.strip() for p in (form.finishing.data.split(',') if form.finishing.data else []) if p.strip()]
        book.finishing = ','.join(finishing_values) or None
        # Sync normalized finishing options
        try:
            from models import FinishingOption
            current = {fo.name.lower(): fo for fo in getattr(book,'finishing_options', [])}
            desired = {fv.lower(): fv for fv in finishing_values}
            # remove
            for key in list(current.keys()):
                if key not in desired:
                    book.finishing_options.remove(current[key])
            # add
            for fv in finishing_values:
                if fv.lower() not in current:
                    fo = FinishingOption.query.filter(db.func.lower(FinishingOption.name)==fv.lower()).first()
                    if not fo:
                        fo = FinishingOption(name=fv); db.session.add(fo); db.session.flush()
                    book.finishing_options.append(fo)
        except Exception:
            pass
        book.active = bool(form.active.data)
        db.session.commit()
        flash('Book updated','success')
        return redirect(url_for('books_index'))
    return render_template('tools/books_form.html', form=form, book=book)

@app.route('/tools/books/<int:book_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_books')
def books_delete(book_id: int):
    book = Book.query.get_or_404(book_id)
    try:
        db.session.delete(book)
        db.session.commit()
        flash('Book deleted','success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to delete book: {exc}','danger')
    return redirect(url_for('books_index'))

@app.route('/tools/books/<int:book_id>/toggle', methods=['POST'])
@login_required
@permission_required('manage_books')
def books_toggle(book_id: int):
    book = Book.query.get_or_404(book_id)
    book.active = not bool(book.active)
    try:
        db.session.commit()
        flash(f'Book {"activated" if book.active else "hidden"}.','success')
    except Exception as exc:
        db.session.rollback(); flash(f'Failed to toggle: {exc}','danger')
    return redirect(url_for('books_index', page=request.args.get('page',1)))

@app.route('/tools/books/bulk-import', methods=['GET','POST'])
@login_required
@permission_required('manage_books')
def books_bulk_import():
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file uploaded','warning')
            return redirect(url_for('books_bulk_import'))
        ext = os.path.splitext(f.filename)[1].lower()
        rows = []
        try:
            if ext == '.json':
                import json as _json
                rows = _json.loads(f.read().decode('utf-8'))
            elif ext == '.csv':
                import csv as _csv
                import io as _io
                text = f.read().decode('utf-8')
                reader = _csv.DictReader(_io.StringIO(text))
                rows = list(reader)
            else:
                flash('Unsupported file type (use .json or .csv)','danger')
                return redirect(url_for('books_bulk_import'))
        except Exception as exc:
            flash(f'Failed to parse file: {exc}','danger')
            return redirect(url_for('books_bulk_import'))
        created = 0; updated = 0
        for r in rows:
            try:
                name = (r.get('Book_Name') or r.get('name') or '').strip()
                if not name:
                    continue
                price = r.get('Price') or r.get('price') or 0
                subject = (r.get('Subject') or r.get('subject') or '').strip() or None
                raw_year = r.get('Year') or r.get('year') or ''
                # Store raw year as simple string (no mapping logic now)
                year_group = str(raw_year).strip() or None
                cover = (r.get('Cover') or r.get('cover') or '').strip() or None
                cover_url = (r.get('Cover_URL') or r.get('cover_url') or '').strip() or None
                inner = (r.get('Inner') or r.get('inner') or '').strip() or None
                inner_url = (r.get('Inner_URL') or r.get('inner_url') or '').strip() or None
                print_format = (r.get('Print_Format') or r.get('print_format') or '').strip() or None
                finishing = (r.get('Finishing') or r.get('finishing') or '').strip() or None
                existing = Book.query.filter_by(name=name).first()
                if existing:
                    existing.price = float(price or 0)
                    existing.subject = subject
                    existing.year_group = year_group
                    existing.cover = cover
                    if cover_url: existing.cover_url = cover_url
                    existing.inner = inner
                    if inner_url: existing.inner_url = inner_url
                    existing.print_format = print_format
                    existing.finishing = finishing
                    updated += 1
                else:
                    db.session.add(Book(
                        name=name,
                        price=float(price or 0),
                        subject=subject,
                        year_group=year_group,
                        cover=cover,
                        cover_url=cover_url,
                        inner=inner,
                        inner_url=inner_url,
                        print_format=print_format,
                        finishing=finishing,
                        active=True
                    ))
                    created += 1
            except Exception as _row_exc:
                # Skip problematic row but continue
                print(f"[WARN] Skipped row in bulk import: {_row_exc}")
        try:
            db.session.commit()
            flash(f'Bulk import complete: {created} created, {updated} updated','success')
        except Exception as exc:
            db.session.rollback(); flash(f'Bulk import failed: {exc}','danger')
        return redirect(url_for('books_index'))
    return render_template('tools/books_bulk_import.html')

@app.route('/tools/books/import-json-file', methods=['POST'])
@login_required
@permission_required('manage_books')
def books_import_json_file():
    """Upload a .json file containing a list of book objects and upsert by Book_Name.

    Expected structure: [ { "Book_Name": "...", "Price": 10.0, ... }, ... ]
    Keys respected (case-insensitive fallbacks):
      Book_Name/name, Subject/subject, Year/year, Price/price,
      Cover/cover, Cover_URL/cover_url, Inner/inner, Inner_URL/inner_url,
      Print_Format/print_format, Finishing/finishing (string or list)
    """
    f = request.files.get('file')
    if not f or not f.filename:
        flash('No file selected','warning')
        return redirect(url_for('books_index'))
    ext = os.path.splitext(f.filename)[1].lower()
    if ext != '.json':
        flash('Please upload a .json file','warning')
        return redirect(url_for('books_index'))
    try:
        import json as _json
        data = _json.loads(f.read().decode('utf-8'))
    except Exception as exc:
        flash(f'Failed to parse JSON: {exc}','danger')
        return redirect(url_for('books_index'))
    if not isinstance(data, list):
        flash('Top-level JSON must be a list of book objects','danger')
        return redirect(url_for('books_index'))
    created = updated = skipped = 0
    from models import FinishingOption
    def safe_str(v):
        try:
            if v is None:
                return ''
            return str(v).strip()
        except Exception:
            return ''
    for row in data:
        if not isinstance(row, dict):
            skipped += 1; continue
        name = safe_str(row.get('Book_Name') or row.get('name'))
        if not name:
            skipped += 1; continue
        # Parse fields
        price_raw = row.get('Price') or row.get('price') or 0
        try: price_val = float(price_raw)
        except Exception: price_val = 0.0
        subject = safe_str(row.get('Subject') or row.get('subject')) or None
        year_group = safe_str(row.get('Year') or row.get('year')) or None
        cover = safe_str(row.get('Cover') or row.get('cover')) or None
        cover_url = safe_str(row.get('Cover_URL') or row.get('cover_url')) or None
        inner = safe_str(row.get('Inner') or row.get('inner')) or None
        inner_url = safe_str(row.get('Inner_URL') or row.get('inner_url')) or None
        print_format = safe_str(row.get('Print_Format') or row.get('print_format')) or None
        finishing_raw = (row.get('Finishing') or row.get('finishing') or '')
        if isinstance(finishing_raw, list):
            finishing_values = [safe_str(v) for v in finishing_raw if safe_str(v)]
        else:
            finishing_values = [p.strip() for p in safe_str(finishing_raw).split(',') if p.strip()]
        finishing_csv = ','.join(finishing_values) or None
        existing = Book.query.filter(db.func.lower(Book.name)==name.lower()).first()
        if existing:
            existing.price = price_val
            existing.subject = subject
            existing.year_group = year_group
            existing.cover = cover
            if cover_url: existing.cover_url = cover_url
            existing.inner = inner
            if inner_url: existing.inner_url = inner_url
            existing.print_format = print_format
            existing.finishing = finishing_csv
            # Sync normalized finishing options
            try:
                current = {fo.name.lower(): fo for fo in getattr(existing,'finishing_options', [])}
                desired = {fv.lower(): fv for fv in finishing_values}
                for key in list(current.keys()):
                    if key not in desired:
                        existing.finishing_options.remove(current[key])
                for fv in finishing_values:
                    if fv.lower() not in current:
                        fo = FinishingOption.query.filter(db.func.lower(FinishingOption.name)==fv.lower()).first()
                        if not fo:
                            fo = FinishingOption(name=fv); db.session.add(fo); db.session.flush()
                        existing.finishing_options.append(fo)
            except Exception:
                pass
            updated += 1
        else:
            b = Book(name=name, price=price_val, subject=subject, year_group=year_group,
                     cover=cover, cover_url=cover_url, inner=inner, inner_url=inner_url,
                     print_format=print_format, finishing=finishing_csv, active=True)
            db.session.add(b); db.session.flush()
            try:
                b.finishing_options = []
                for fv in finishing_values:
                    fo = FinishingOption.query.filter(db.func.lower(FinishingOption.name)==fv.lower()).first()
                    if not fo:
                        fo = FinishingOption(name=fv); db.session.add(fo); db.session.flush()
                    b.finishing_options.append(fo)
            except Exception:
                pass
            created += 1
    try:
        db.session.commit()
        flash(f'JSON import: {created} created, {updated} updated, {skipped} skipped','success')
    except Exception as exc:
        db.session.rollback(); flash(f'Import failed: {exc}','danger')
    return redirect(url_for('books_index'))

@app.route('/tools/books/import-json', methods=['POST'])
@login_required
@permission_required('manage_books')
def books_import_json():
    """Import or update books from JSON body.

    Expected payload: list of objects with keys matching new schema:
      Book_Name, Subject, Year, Price, Cover, Cover_URL, Inner, Inner_URL, Print_Format, Finishing

    Upsert logic keyed by Book_Name (case-insensitive exact match). If existing, fields updated; not recreated.
    Returns JSON summary.
    """
    import json as _json
    try:
        payload = request.get_json(silent=True)
    except Exception:
        payload = None
    if not isinstance(payload, list):
        return jsonify({'success': False, 'error': 'JSON body must be a list of book objects'}), 400
    created = 0; updated = 0; skipped = 0; errors = []
    def safe_str(v):
        try:
            if v is None:
                return ''
            return str(v).strip()
        except Exception:
            return ''
    for idx, row in enumerate(payload):
        if not isinstance(row, dict):
            skipped += 1
            continue
        name = safe_str(row.get('Book_Name') or row.get('name'))
        if not name:
            skipped += 1
            continue
        try:
            price_raw = row.get('Price') or row.get('price') or 0
            try:
                price_val = float(price_raw)
            except Exception:
                price_val = 0.0
            subject = safe_str(row.get('Subject') or row.get('subject')) or None
            year_group = safe_str(row.get('Year') or row.get('year')) or None
            cover = safe_str(row.get('Cover') or row.get('cover')) or None
            cover_url = safe_str(row.get('Cover_URL') or row.get('cover_url')) or None
            inner = safe_str(row.get('Inner') or row.get('inner')) or None
            inner_url = safe_str(row.get('Inner_URL') or row.get('inner_url')) or None
            print_format = safe_str(row.get('Print_Format') or row.get('print_format')) or None
            finishing_raw = (row.get('Finishing') or row.get('finishing') or '')
            if isinstance(finishing_raw, list):
                finishing = ','.join([safe_str(v) for v in finishing_raw if safe_str(v)]) or None
            else:
                finishing = ','.join([p.strip() for p in safe_str(finishing_raw).split(',') if p.strip()]) or None
            existing = Book.query.filter(db.func.lower(Book.name)==name.lower()).first()
            if existing:
                existing.price = price_val
                existing.subject = subject
                existing.year_group = year_group
                existing.cover = cover
                if cover_url: existing.cover_url = cover_url
                existing.inner = inner
                if inner_url: existing.inner_url = inner_url
                existing.print_format = print_format
                existing.finishing = finishing
                updated += 1
            else:
                db.session.add(Book(name=name, price=price_val, subject=subject, year_group=year_group,
                                    cover=cover, cover_url=cover_url, inner=inner, inner_url=inner_url,
                                    print_format=print_format, finishing=finishing, active=True))
                created += 1
        except Exception as exc:
            errors.append({'index': idx, 'error': str(exc)})
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': f'Database commit failed: {exc}', 'created': created, 'updated': updated, 'skipped': skipped, 'row_errors': errors}), 500
    return jsonify({'success': True, 'created': created, 'updated': updated, 'skipped': skipped, 'row_errors': errors})

# ------------ Book API (read-only for now) ------------- #
@app.route('/api/books')
def api_books():
    """Public read-only book catalog.

    By default returns only active books. Include ?all=1 to include inactive.
    Supports basic filtering by q (name/subject) and year.
    """
    q = (request.args.get('q') or '').strip().lower()
    year = (request.args.get('year') or '').strip().lower()
    include_all = bool(request.args.get('all'))
    page = max(1, int(request.args.get('page', 1) or 1))
    per_page = min(200, int(request.args.get('per_page', 50) or 50))
    base_q = Book.query
    if year:
        base_q = base_q.filter(Book.year_group == year)
    if not include_all:
        base_q = base_q.filter(Book.active.is_(True))
    if q:
        like = f"%{q}%"
        base_q = base_q.filter(or_(Book.name.ilike(like), Book.subject.ilike(like)))
    total = base_q.count()
    rows = (base_q.order_by(Book.name.asc())
                 .offset((page-1)*per_page)
                 .limit(per_page)
                 .all())
    data = []
    for b in rows:
        data.append({
            'id': b.id,
            'name': b.name,
            'price': float(b.price or 0),
            'subject': b.subject,
            'year': b.year_group,
            'cover': b.cover,
            'cover_url': b.cover_url,
            'inner': b.inner,
            'inner_url': b.inner_url,
            'print_format': b.print_format,
            'finishing': b.finishing,
            'active': bool(b.active),
        })
    return jsonify({
        'page': page,
        'per_page': per_page,
        'total': total,
        'pages': (total // per_page) + (1 if total % per_page else 0),
        'results': data
    })


@app.route("/staff/import", methods=["GET","POST"])
@login_required
def staff_import():
    preview = None
    token = None
    instance_tmp = os.path.join(app.instance_path, 'imports')
    os.makedirs(instance_tmp, exist_ok=True)

    # First step: upload & preview
    if request.method == 'POST' and 'file' in request.files and request.files['file'].filename:
        f = request.files['file']
        if not allowed_file(f.filename):
            flash("Unsupported file type", "danger")
            return redirect(url_for('staff_import'))
        try:
            if f.filename.lower().endswith('.csv'):
                df = pd.read_csv(f)
            else:
                df = pd.read_excel(f)
            df2 = normalize_staff_dataframe(df)
            preview = df2.head(20).to_string(index=False)
            token = uuid4().hex
            temp_path = os.path.join(instance_tmp, f"{token}.csv")
            df2.to_csv(temp_path, index=False)
            return render_template("staff/import.html", preview=preview, token=token)
        except Exception as e:
            flash(f"Import failed: {e}", "danger")

    # Second step: confirm import
    if request.method == 'POST' and request.form.get('confirm') and request.form.get('token'):
        token = request.form.get('token')
        temp_path = os.path.join(instance_tmp, f"{token}.csv")
        if not os.path.exists(temp_path):
            flash("Import session expired. Please re-upload.", "warning")
            return redirect(url_for('staff_import'))
        try:
            import_df = pd.read_csv(temp_path, keep_default_na=False)

            def clean(val):
                if val is None:
                    return ''
                try:
                    import math
                    if isinstance(val, float) and math.isnan(val):
                        return ''
                except Exception:
                    pass
                return str(val).strip()

            from models import \
                StaffChange  # late import to ensure model registered
            added = updated = skipped_missing = unchanged = 0
            field_changes = 0
            for _, row in import_df.iterrows():
                name = clean(row.get('name'))
                if not name:
                    skipped_missing += 1
                    continue
                email_raw = clean(row.get('email'))
                email = email_raw.lower() if email_raw else None
                dept = clean(row.get('department')) or None
                phone = clean(row.get('phone')) or None
                branch_val = clean(row.get('branch')) or None

                # Matching strategy: email (case-insensitive) first, then exact name (case-insensitive)
                existing = None
                if email:
                    existing = Staff.query.filter(db.func.lower(Staff.email) == email).first()
                if not existing:
                    existing = Staff.query.filter(db.func.lower(Staff.name) == name.lower()).first()

                if existing:
                    mutable = {
                        'name': name,
                        'email': email,
                        'department': dept,
                        'phone': phone,
                        'branch': branch_val,
                    }
                    row_changed = False
                    for field, new_val in mutable.items():
                        old_val = getattr(existing, field)
                        norm_old = old_val or None
                        norm_new = new_val or None
                        if norm_old != norm_new:
                            setattr(existing, field, new_val)
                            db.session.flush()  # ensure existing.id populated
                            db.session.add(StaffChange(
                                staff_id=existing.id,
                                field=field,
                                old_value=old_val,
                                new_value=new_val,
                                changed_by_id=current_user.id,
                            ))
                            field_changes += 1
                            row_changed = True
                    if row_changed:
                        updated += 1
                    else:
                        unchanged += 1
                else:
                    s = Staff(name=name, department=dept, email=email, phone=phone, branch=branch_val)
                    db.session.add(s)
                    added += 1
            db.session.commit()
            try:
                os.remove(temp_path)
            except Exception:
                pass
            extra = f", {field_changes} field change(s)" if field_changes else ""
            flash(
                f"Import complete: {added} added, {updated} updated, {unchanged} unchanged, {skipped_missing} skipped (missing name){extra}.",
                "success"
            )
            return redirect(url_for('staff_index'))
        except Exception as e:
            db.session.rollback()
            flash(f"Final import failed: {e}", "danger")
            return redirect(url_for('staff_import'))
    return render_template("staff/import.html", preview=preview, token=token)

@app.route("/staff/export")
@login_required
def staff_export():
    rows = Staff.query.all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name","department","email","phone","branch"])
    for s in rows:
        writer.writerow([s.name, s.department, s.email, s.phone, s.branch])
    mem = io.BytesIO(output.getvalue().encode('utf-8'))
    mem.seek(0)
    return send_file(mem, mimetype='text/csv', as_attachment=True, attachment_filename='staff_export.csv')

# ---------------- Cycles CRUD ----------------
@app.route("/cycles")
@login_required
def cycles_index():
    cycles = ObservationCycle.query.order_by(ObservationCycle.start_date.desc().nullslast()).all()
    return render_template("cycles/index.html", cycles=cycles)

@app.route("/cycles/new", methods=["GET","POST"])
@login_required
def cycle_new():
    form = CycleForm()
    if form.validate_on_submit():
        c = ObservationCycle(title=form.title.data, start_date=form.start_date.data, end_date=form.end_date.data)
        db.session.add(c)
        db.session.commit()
        flash("Cycle created", "success")
        return redirect(url_for('cycles_index'))
    return render_template("cycles/form.html", form=form, cycle=None)

@app.route("/cycles/<int:cid>/edit", methods=["GET","POST"])
@login_required
def cycle_edit(cid):
    c = ObservationCycle.query.get_or_404(cid)
    form = CycleForm()
    if request.method == 'GET':
        form.title.data = c.title
        form.start_date.data = c.start_date
        form.end_date.data = c.end_date
    if form.validate_on_submit():
        c.title = form.title.data
        c.start_date = form.start_date.data
        c.end_date = form.end_date.data
        db.session.commit()
        flash("Cycle updated", "success")
        return redirect(url_for('cycles_index'))
    return render_template("cycles/form.html", form=form, cycle=c)

@app.route("/cycles/<int:cid>/delete")
@login_required
def cycle_delete(cid):
    c = ObservationCycle.query.get_or_404(cid)
    db.session.delete(c)
    db.session.commit()
    flash("Cycle deleted", "success")
    return redirect(url_for('cycles_index'))

# ---------------- Availability CRUD & Import ----------------
@app.route('/availability')
@login_required
def availability_index():
    # Real-time fetch & upsert from remote source each page load
    sync_count = 0
    sync_error = None
    try:
        import requests
        url = 'https://availability.pythonanywhere.com/data'
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        payload = r.json()
        for row in payload.get('data', []):
            name = (row.get('Name') or '').strip()
            if not name:
                continue
            dept = (row.get('Department') or '').strip() or None
            branches_raw = (row.get('Which Branch Take You Take Lesson In?') or '').replace('\n',' ').strip()
            branches_parts = [p.strip() for p in branches_raw.split(',') if p.strip()]
            normal_branches = []
            for bp in branches_parts:
                for canonical in ['Whitechapel','East Ham','Stratford','Docklands']:
                    if canonical.lower() in bp.lower() and canonical not in normal_branches:
                        normal_branches.append(canonical)
            branches = ",".join(normal_branches) if normal_branches else None
            days = (row.get('Which Days Are You Available') or '').replace('\n',' ').strip() or None
            subjects = (row.get('Which Subjects Can You Teach') or '').replace('\n',' ').strip() or None
            notes = (row.get('NotesMessages?') or '').replace('\n',' ').strip() or None
            existing = Availability.query.filter_by(name=name, department=dept).first()
            if existing:
                updated = False
                if existing.branches != branches:
                    existing.branches = branches; updated = True
                if existing.days != days:
                    existing.days = days; updated = True
                if existing.subjects != subjects:
                    existing.subjects = subjects; updated = True
                if existing.notes != notes:
                    existing.notes = notes; updated = True
                if updated:
                    sync_count += 1
            else:
                a = Availability(name=name, department=dept, branches=branches, days=days, subjects=subjects, notes=notes)
                db.session.add(a)
                sync_count += 1
        db.session.commit()
    except Exception as e:
        sync_error = str(e)
        # Do not abort—show whatever is in DB
    # Filters: department(s), branch(es), subject(s), day(s), plus free-text search
    q = Availability.query
    selected_departments = [d.strip() for d in request.args.getlist('department') if d.strip()]
    if selected_departments:
        q = q.filter(Availability.department.in_(selected_departments))
    selected_branches = [b.strip() for b in request.args.getlist('branch') if b.strip()]
    if selected_branches:
        q = q.filter(or_(*[Availability.branches.ilike(f"%{b}%") for b in selected_branches]))
    selected_subjects = [s.strip() for s in request.args.getlist('subject') if s.strip()]
    if selected_subjects:
        q = q.filter(or_(*[Availability.subjects.ilike(f"%{s}%") for s in selected_subjects]))
    selected_days = [d.strip() for d in request.args.getlist('day') if d.strip()]
    if selected_days:
        # Day match: simple contains of weekday token (e.g., 'Monday')
        q = q.filter(or_(*[Availability.days.ilike(f"%{d}%") for d in selected_days]))
    search = request.args.get('search','').strip()
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            Availability.name.ilike(like),
            Availability.subjects.ilike(like),
            Availability.days.ilike(like),
            Availability.notes.ilike(like)
        ))
    sort = request.args.get('sort','name')
    direction = request.args.get('dir','asc')
    sortable = {
        'name': Availability.name,
        'department': Availability.department,
        'created_at': Availability.created_at,
        'updated_at': Availability.updated_at,
    }
    col = sortable.get(sort, Availability.name)
    if direction == 'desc':
        col = col.desc()
    records = q.order_by(col).all()
    # Distinct departments
    depts = [r[0] for r in db.session.query(Availability.department).distinct().filter(Availability.department.isnot(None)).order_by(Availability.department.asc()).all()]
    # Distinct branches (ensure canonical list always present, e.g. Stratford)
    branch_values = []
    for r in db.session.query(Availability.branches).all():
        if not r[0]:
            continue
        for b in r[0].split(','):
            b2 = b.strip()
            if b2 and b2 not in branch_values:
                branch_values.append(b2)
    # Append any canonical branches not already in data
    canonical_branches = ['Whitechapel','East Ham','Stratford','Docklands']
    for cb in canonical_branches:
        if cb not in branch_values:
            branch_values.append(cb)
    branch_values.sort()
    # Distinct subjects (split by comma)
    subject_values = []
    for r in db.session.query(Availability.subjects).all():
        if not r[0]:
            continue
        for s in r[0].split(','):
            s2 = s.strip()
            if s2 and s2 not in subject_values:
                subject_values.append(s2)
    subject_values.sort()
    # Weekday tokens enumeration (derive from data)
    weekday_tokens = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
    day_values = []
    for r in db.session.query(Availability.days).all():
        if not r[0]:
            continue
        text_days = r[0]
        for wd in weekday_tokens:
            if wd in text_days and wd not in day_values:
                day_values.append(wd)
    # Preserve natural week order
    day_values = [d for d in weekday_tokens if d in day_values]
    return render_template('availability/index.html',
                           records=records,
                           depts=depts,
                           branches=branch_values,
                           subjects=subject_values,
                           days=day_values,
                           selected_departments=selected_departments,
                           selected_branches=selected_branches,
                           selected_subjects=selected_subjects,
                           selected_days=selected_days,
                           sync_count=sync_count,
                           sync_error=sync_error,
                           synced_at=datetime.now(timezone.utc))

@app.route('/availability/new', methods=['GET','POST'])
@login_required
def availability_new():
    form = AvailabilityForm()
    if form.validate_on_submit():
        a = Availability(
            name=form.name.data.strip(),
            department=form.department.data.strip() or None,
            branches=",".join(form.branches.data) if form.branches.data else None,
            days=form.days.data.strip() if form.days.data else None,
            subjects=form.subjects.data.strip() if form.subjects.data else None,
            notes=form.notes.data.strip() if form.notes.data else None,
        )
        db.session.add(a)
        db.session.commit()
        flash('Availability record created','success')
        return redirect(url_for('availability_index'))
    return render_template('availability/form.html', form=form, record=None)

@app.route('/availability/<int:aid>/edit', methods=['GET','POST'])
@login_required
def availability_edit(aid):
    a = Availability.query.get_or_404(aid)
    form = AvailabilityForm()
    if request.method == 'GET':
        form.name.data = a.name
        form.department.data = a.department
        form.branches.data = [b for b in (a.branches or '').split(',') if b]
        form.days.data = a.days
        form.subjects.data = a.subjects
        form.notes.data = a.notes
    if form.validate_on_submit():
        a.name = form.name.data.strip()
        a.department = form.department.data.strip() or None
        a.branches = ",".join(form.branches.data) if form.branches.data else None
        a.days = form.days.data.strip() if form.days.data else None
        a.subjects = form.subjects.data.strip() if form.subjects.data else None
        a.notes = form.notes.data.strip() if form.notes.data else None
        db.session.commit()
        flash('Availability updated','success')
        return redirect(url_for('availability_index'))
    return render_template('availability/form.html', form=form, record=a)

@app.route('/availability/<int:aid>/delete')
@login_required
def availability_delete(aid):
    a = Availability.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    flash('Availability deleted','success')
    return redirect(url_for('availability_index'))

@app.route('/availability/import_remote', methods=['POST'])
@login_required
def availability_import_remote():
    import base64
    import json

    import requests
    url = 'https://availability.pythonanywhere.com/data'
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        flash(f'Remote fetch failed: {e}', 'danger')
        return redirect(url_for('availability_index'))
    count = 0
    for row in payload.get('data', []):
        name = (row.get('Name') or '').strip()
        if not name:
            continue
        dept = (row.get('Department') or '').strip() or None
        branches_raw = (row.get('Which Branch Take You Take Lesson In?') or '').replace('\n',' ').strip()
        # Normalise branch names to our BRANCH_CHOICES keys where possible
        branches_parts = [p.strip() for p in branches_raw.split(',') if p.strip()]
        normal_branches = []
        for bp in branches_parts:
            # Extract canonical branch word (Whitechapel, East Ham, Stratford, Docklands)
            for canonical in ['Whitechapel','East Ham','Stratford','Docklands']:
                if canonical.lower() in bp.lower() and canonical not in normal_branches:
                    normal_branches.append(canonical)
        branches = ",".join(normal_branches) if normal_branches else None
        days = (row.get('Which Days Are You Available') or '').replace('\n',' ').strip() or None
        subjects = (row.get('Which Subjects Can You Teach') or '').replace('\n',' ').strip() or None
        notes = (row.get('NotesMessages?') or '').replace('\n',' ').strip() or None
        # Upsert semantics: if identical name+department exists, update; else create
        existing = Availability.query.filter_by(name=name, department=dept).first()
        if existing:
            existing.branches = branches
            existing.days = days
            existing.subjects = subjects
            existing.notes = notes
        else:
            a = Availability(name=name, department=dept, branches=branches, days=days, subjects=subjects, notes=notes)
            db.session.add(a)
        count += 1
    db.session.commit()
    flash(f'Imported/updated {count} availability records from remote source.', 'success')
    return redirect(url_for('availability_index'))

# ---------------- Issues (tracker) ----------------
@app.route('/issues')
@login_required
def issues_index():
    # Base query
    q = Issue.query
    # Filters
    status_filters = [v for v in request.args.getlist('status') if v]
    if status_filters:
        q = q.filter(Issue.status.in_(status_filters))
    crit_filters = [v for v in request.args.getlist('criticality') if v]
    if crit_filters:
        q = q.filter(Issue.criticality.in_(crit_filters))
    urg_filters = [v for v in request.args.getlist('urgency') if v]
    if urg_filters:
        q = q.filter(Issue.urgency.in_(urg_filters))
    branch_filters = [v for v in request.args.getlist('branch') if v]
    if branch_filters:
        q = q.filter(Issue.branch.in_(branch_filters))
    search = (request.args.get('search') or '').strip()
    if search:
        like = f"%{search.lower()}%"
        q = q.filter(or_(db.func.lower(Issue.title).like(like), db.func.lower(Issue.details).like(like)))

    issues = q.order_by(Issue.created_at.desc()).all()

    # Analytics / metrics
    total = len(issues)
    open_issues = [i for i in issues if (i.status or '').lower() != 'resolved']
    resolved = total - len(open_issues)
    critical_open = sum(1 for i in open_issues if (i.criticality or '').lower() == 'critical')
    high_urg_open = sum(1 for i in open_issues if (i.urgency or '').lower() == 'high')

    # Choice lists (distinct values in DB for dynamic filtering)
    statuses = [r[0] for r in db.session.query(Issue.status).distinct().filter(Issue.status.isnot(None)).order_by(Issue.status.asc()).all()]
    criticalities = [r[0] for r in db.session.query(Issue.criticality).distinct().filter(Issue.criticality.isnot(None)).order_by(Issue.criticality.asc()).all()]
    urgencies = [r[0] for r in db.session.query(Issue.urgency).distinct().filter(Issue.urgency.isnot(None)).order_by(Issue.urgency.asc()).all()]
    branches = [r[0] for r in db.session.query(Issue.branch).distinct().filter(Issue.branch.isnot(None)).order_by(Issue.branch.asc()).all()]

    # Preload recent changes (last 5) per issue id in one query for efficiency
    change_rows = db.session.query(IssueChange).filter(IssueChange.issue_id.in_([i.id for i in issues])).order_by(IssueChange.changed_at.desc()).all() if issues else []
    recent_changes = {}
    for ch in change_rows:
        bucket = recent_changes.setdefault(ch.issue_id, [])
        if len(bucket) < 5:
            bucket.append(ch)
    return render_template('issues/index.html', issues=issues, total=total, open_count=len(open_issues), resolved_count=resolved, critical_open=critical_open, high_urg_open=high_urg_open,
                           statuses=statuses, criticalities=criticalities, urgencies=urgencies, branches=branches,
                           selected_status=status_filters, selected_criticality=crit_filters, selected_urgency=urg_filters, selected_branches=branch_filters, search=search,
                           recent_changes=recent_changes)


@app.route('/issues/new', methods=['GET','POST'])
@login_required
def issue_new():
    form = IssueForm()
    if request.method == 'POST' and form.validate_on_submit():
        issue = Issue(
            title=form.title.data.strip(),
            details=form.details.data.strip() if form.details.data else None,
            status=form.status.data,
            criticality=form.criticality.data,
            urgency=form.urgency.data,
            branch=form.branch.data or None,
            action_taken=form.action_taken.data.strip() if form.action_taken.data else None,
            created_by_id=current_user.id,
        )
        db.session.add(issue)
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'id': issue.id})
        flash('Issue created','success')
        return redirect(url_for('issues_index'))
    # Preselect defaults
    if request.method == 'GET':
        form.status.data = 'Pending'
        form.criticality.data = 'Minor'
        form.urgency.data = 'Medium'
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('issues/partials/_form_inner.html', form=form, issue=None)
    return render_template('issues/form.html', form=form, issue=None)


@app.route('/issues/<int:iid>/edit', methods=['GET','POST'])
@login_required
def issue_edit(iid):
    issue = Issue.query.get_or_404(iid)
    form = IssueForm(obj=issue)
    if request.method == 'POST' and form.validate_on_submit():
        # Track changes field-by-field
        fields = {
            'title': (issue.title, form.title.data.strip()),
            'details': (issue.details, form.details.data.strip() if form.details.data else None),
            'status': (issue.status, form.status.data),
            'criticality': (issue.criticality, form.criticality.data),
            'urgency': (issue.urgency, form.urgency.data),
            'branch': (issue.branch, form.branch.data or None),
            'action_taken': (issue.action_taken, form.action_taken.data.strip() if form.action_taken.data else None),
        }
        for field, (old, new) in fields.items():
            if (old or '') != (new or ''):
                setattr(issue, field, new)
                ch = IssueChange(issue_id=issue.id, field=field, old_value=old, new_value=new, changed_by_id=current_user.id)
                db.session.add(ch)
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'id': issue.id})
        flash('Issue updated','success')
        return redirect(url_for('issues_index'))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('issues/partials/_form_inner.html', form=form, issue=issue)
    return render_template('issues/form.html', form=form, issue=issue)


@app.route('/issues/<int:iid>/delete')
@login_required
def issue_delete(iid):
    issue = Issue.query.get_or_404(iid)
    db.session.delete(issue)
    db.session.commit()
    flash('Issue deleted','success')
    return redirect(url_for('issues_index'))

# ---------------- Meetings ----------------
@app.route('/meetings')
@login_required
def meetings_index():
    from datetime import date as _date
    from datetime import timedelta
    today = _date.today()
    week_ahead = today + timedelta(days=7)
    q = Meeting.query
    # Filters (participant, booked_by, date range, search in agenda)
    part = request.args.get('participant', type=int)
    if part:
        q = q.filter(Meeting.participant_id==part)
    booked = request.args.get('booked_by', type=int)
    if booked:
        q = q.filter(Meeting.booked_by_id==booked)
    start_date = request.args.get('start')
    end_date = request.args.get('end')
    if start_date:
        try:
            sd = datetime.strptime(start_date, '%Y-%m-%d').date()
            q = q.filter(Meeting.date >= sd)
        except Exception:
            pass
    if end_date:
        try:
            ed = datetime.strptime(end_date, '%Y-%m-%d').date()
            q = q.filter(Meeting.date <= ed)
        except Exception:
            pass
    search = (request.args.get('search') or '').strip().lower()
    if search:
        q = q.filter(db.func.lower(Meeting.agenda).like(f"%{search}%"))
    meetings = q.order_by(Meeting.date.asc(), Meeting.time.asc()).all()

    # Analytics / summaries
    upcoming_today = [m for m in meetings if m.date == today]
    upcoming_week = [m for m in meetings if today <= m.date <= week_ahead]
    total = len(meetings)
    user_today = [m for m in upcoming_today if m.participant_id == current_user.id or m.booked_by_id == current_user.id]
    user_week = [m for m in upcoming_week if m.participant_id == current_user.id or m.booked_by_id == current_user.id]

    users = User.query.order_by(User.name.asc()).all()
    return render_template('meetings/index.html', meetings=meetings, users=users,
                           total=total, upcoming_today=len(upcoming_today), upcoming_week=len(upcoming_week),
                           user_today=len(user_today), user_week=len(user_week), search=search,
                           sel_part=part, sel_booked=booked, start_date=start_date, end_date=end_date)

# Fallback: handle accidental POSTs to /meetings (collection) by treating as create
@app.route('/meetings', methods=['POST'])
@login_required
def meetings_index_post():
    """Fallback creation endpoint if the create form posts to /meetings instead of /meetings/new.

    This guards against stale cached form markup or JS overrides causing a 405.
    Prefer using /meetings/new for creation; this simply delegates.
    """
    form = MeetingForm()
    users = User.query.order_by(User.name.asc()).all()
    form.participant_id.choices = [(u.id, u.name) for u in users]
    if form.validate_on_submit():
        m = Meeting(participant_id=form.participant_id.data, booked_by_id=current_user.id,
                    agenda=form.agenda.data.strip(), date=form.date.data, time=form.time.data.strip(),
                    student_name=form.student_name.data.strip() if form.student_name.data else None,
                    parent_name=form.parent_name.data.strip() if form.parent_name.data else None,
                    outcome=form.outcome.data.strip() if form.outcome.data else None)
        db.session.add(m)
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'id': m.id})
        flash('Meeting created','success')
        return redirect(url_for('meetings_index'))
    # On validation failure, re-render form (non-AJAX full page fallback)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('meetings/form.html', form=form, meeting=None)
    return render_template('meetings/form.html', form=form, meeting=None)

@app.route('/meetings/new', methods=['GET','POST'])
@login_required
def meeting_new():
    form = MeetingForm()
    users = User.query.order_by(User.name.asc()).all()
    form.participant_id.choices = [(u.id, u.name) for u in users]
    if request.method == 'POST' and form.validate_on_submit():
        m = Meeting(participant_id=form.participant_id.data, booked_by_id=current_user.id,
                    agenda=form.agenda.data.strip(), date=form.date.data, time=form.time.data.strip(),
                    student_name=form.student_name.data.strip() if form.student_name.data else None,
                    parent_name=form.parent_name.data.strip() if form.parent_name.data else None,
                    outcome=form.outcome.data.strip() if form.outcome.data else None)
        db.session.add(m)
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'id': m.id})
        flash('Meeting created','success')
        return redirect(url_for('meetings_index'))
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        # Return form HTML again for validation errors
        return render_template('meetings/form.html', form=form, meeting=None)
    return render_template('meetings/form.html', form=form, meeting=None)

@app.route('/meetings/<int:mid>/edit', methods=['GET','POST'])
@login_required
def meeting_edit(mid):
    m = Meeting.query.get_or_404(mid)
    form = MeetingForm(obj=m)
    users = User.query.order_by(User.name.asc()).all()
    form.participant_id.choices = [(u.id, u.name) for u in users]
    if request.method == 'POST' and form.validate_on_submit():
        m.participant_id = form.participant_id.data
        m.agenda = form.agenda.data.strip()
        m.date = form.date.data
        m.time = form.time.data.strip()
        m.student_name = form.student_name.data.strip() if form.student_name.data else None
        m.parent_name = form.parent_name.data.strip() if form.parent_name.data else None
        m.outcome = form.outcome.data.strip() if form.outcome.data else None
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'id': m.id})
        flash('Meeting updated','success')
        return redirect(url_for('meetings_index'))
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('meetings/form.html', form=form, meeting=m)
    return render_template('meetings/form.html', form=form, meeting=m)

@app.route('/meetings/<int:mid>/delete')
@login_required
def meeting_delete(mid):
    m = Meeting.query.get_or_404(mid)
    db.session.delete(m)
    db.session.commit()
    flash('Meeting deleted','success')
    return redirect(url_for('meetings_index'))

# ---------------- To-Do Module ----------------
@app.route('/todos')
@login_required
def todos_index():
    # Filters: assigned_to (default: current user), status multi, criticality multi, urgency multi, search
    q = Todo.query
    # Enforce visibility: non-superadmin can only see tasks assigned to them
    if current_user.is_superadmin:
        assigned = request.args.get('assigned', type=int) or current_user.id
    else:
        assigned = current_user.id
    q = q.filter(Todo.assigned_to_id == assigned)
    status_filters = [s for s in request.args.getlist('status') if s]
    if status_filters:
        q = q.filter(Todo.status.in_(status_filters))
    crit_filters = [c for c in request.args.getlist('criticality') if c]
    if crit_filters:
        q = q.filter(Todo.criticality.in_(crit_filters))
    urg_filters = [u for u in request.args.getlist('urgency') if u]
    if urg_filters:
        q = q.filter(Todo.urgency.in_(urg_filters))
    search = (request.args.get('search') or '').strip().lower()
    if search:
        like = f"%{search}%"
        q = q.filter(db.func.lower(Todo.description).like(like))
    # Sorting (most urgent/critical first then due_date then created_at)
    todos = q.order_by(
        db.case((Todo.status=='Pending', 0), else_=1),  # pending first
        db.case((Todo.criticality=='Critical',0),(Todo.criticality=='Medium',1),(Todo.criticality=='Significant',2),(Todo.criticality=='Minor',3), else_=4),
        db.case((Todo.urgency=='High',0),(Todo.urgency=='Medium',1),(Todo.urgency=='Low',2), else_=3),
        Todo.due_date.is_(None),
        Todo.due_date.asc().nullslast(),
        Todo.created_at.desc()
    ).all()

    # Metrics for dashboard cards (scoped to assigned filter)
    total = len(todos)
    open_items = [t for t in todos if not t.is_done()]
    done_items = total - len(open_items)
    overdue = [t for t in open_items if t.due_date and t.due_date < date.today()]
    due_soon = [t for t in open_items if t.due_date and (t.due_date - date.today()).days <= 3 and (t.due_date - date.today()).days >= 0]

    # Distinct values (overall for choices – not just this user) for filters
    statuses = [r[0] for r in db.session.query(Todo.status).distinct().filter(Todo.status.isnot(None)).order_by(Todo.status.asc()).all()]
    criticalities = [r[0] for r in db.session.query(Todo.criticality).distinct().filter(Todo.criticality.isnot(None)).order_by(Todo.criticality.asc()).all()]
    urgencies = [r[0] for r in db.session.query(Todo.urgency).distinct().filter(Todo.urgency.isnot(None)).order_by(Todo.urgency.asc()).all()]
    if current_user.is_superadmin:
        users = User.query.order_by(User.name.asc()).all()
    else:
        users = [current_user]

    return render_template('todos/index.html', todos=todos, users=users,
                           total=total, open_count=len(open_items), done_count=done_items,
                           overdue_count=len(overdue), due_soon_count=len(due_soon),
                           selected_assigned=assigned, statuses=statuses, criticalities=criticalities, urgencies=urgencies,
                           selected_status=status_filters, selected_criticality=crit_filters, selected_urgency=urg_filters, search=search,
                           today=date.today())

@app.route('/todos/new', methods=['GET','POST'])
@login_required
def todo_new():
    form = TodoForm()
    users = User.query.order_by(User.name.asc()).all()
    form.assigned_to_id.choices = [(u.id, u.name) for u in users]
    if request.method == 'POST' and form.validate_on_submit():
        t = Todo(
            description=form.description.data.strip(),
            notes=form.notes.data.strip() if form.notes.data else None,
            actions_taken=form.actions_taken.data.strip() if form.actions_taken.data else None,
            criticality=form.criticality.data,
            urgency=form.urgency.data,
            status=form.status.data,
            due_date=form.due_date.data,
            created_by_id=current_user.id,
            assigned_to_id=form.assigned_to_id.data,
        )
        db.session.add(t)
        db.session.commit()
        # If creator is superadmin, send notification email to assignee (avoid emailing yourself redundantly only if different users)
        try:
            if current_user.is_superadmin and t.assigned_to and t.assigned_to.email:
                html = build_task_notification_email(t, current_user, t.assigned_to)
                # Subject line emphasises status & due date
                subj_due = f" (Due {t.due_date.strftime('%Y-%m-%d')})" if t.due_date else ""
                send_email(t.assigned_to.email, f"New Task Assigned: {t.description[:60]}{subj_due}", html)
        except Exception as e:
            # Non-fatal: log to console; production could integrate proper logging
            print(f"[WARN] Task email failed: {e}")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'id': t.id})
        flash('Task created','success')
        return redirect(url_for('todos_index', assigned=t.assigned_to_id))
    # sensible defaults
    if request.method == 'GET':
        form.criticality.data = 'Minor'
        form.urgency.data = 'Medium'
        form.status.data = 'Pending'
        form.assigned_to_id.data = current_user.id
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('todos/partials/_form_inner.html', form=form, todo=None)
    return render_template('todos/form.html', form=form, todo=None)

@app.route('/todos/<int:tid>/edit', methods=['GET','POST'])
@login_required
def todo_edit(tid):
    t = Todo.query.get_or_404(tid)
    # Access: superadmin or creator or assigned user
    if not (current_user.is_superadmin or current_user.id in (t.created_by_id, t.assigned_to_id)):
        abort(403)
    form = TodoForm(obj=t)
    users = User.query.order_by(User.name.asc()).all()
    form.assigned_to_id.choices = [(u.id, u.name) for u in users]
    if request.method == 'POST' and form.validate_on_submit():
        t.description = form.description.data.strip()
        t.notes = form.notes.data.strip() if form.notes.data else None
        t.actions_taken = form.actions_taken.data.strip() if form.actions_taken.data else None
        t.criticality = form.criticality.data
        t.urgency = form.urgency.data
        t.status = form.status.data
        t.due_date = form.due_date.data
        t.assigned_to_id = form.assigned_to_id.data
        db.session.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': True, 'id': t.id})
        flash('Task updated','success')
        return redirect(url_for('todos_index', assigned=t.assigned_to_id))
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return render_template('todos/partials/_form_inner.html', form=form, todo=t)
    return render_template('todos/form.html', form=form, todo=t)

@app.route('/todos/<int:tid>/delete')
@login_required
def todo_delete(tid):
    t = Todo.query.get_or_404(tid)
    assigned = t.assigned_to_id
    db.session.delete(t)
    db.session.commit()
    flash('Task deleted','success')
    return redirect(url_for('todos_index', assigned=assigned))

@app.route('/todos/<int:tid>/toggle', methods=['POST'])
@login_required
def todo_toggle_status(tid):
    t = Todo.query.get_or_404(tid)
    # Only assigned user or creator can toggle; superadmin override
    if current_user.id not in (t.assigned_to_id, t.created_by_id) and not current_user.is_superadmin:
        abort(403)
    t.status = 'Done' if not t.is_done() else 'Pending'
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True, 'status': t.status})
    flash('Status updated','success')
    return redirect(request.referrer or url_for('todos_index', assigned=t.assigned_to_id))

@app.route('/todos/<int:tid>/status', methods=['POST'])
@login_required
def todo_update_status(tid):
    """Direct status update via inline select (AJAX)."""
    t = Todo.query.get_or_404(tid)
    if current_user.id not in (t.assigned_to_id, t.created_by_id) and not current_user.is_superadmin:
        abort(403)
    new_status = request.form.get('status')
    if new_status not in ['Pending','Done']:
        return jsonify({'success': False, 'error': 'Invalid status'}), 400
    t.status = new_status
    db.session.commit()
    return jsonify({'success': True, 'status': t.status})

# ---------------- Observations CRUD + filters ----------------
@app.route("/observations")
@login_required
def observations_index():
    q = Observation.query
    cycle_id = request.args.get('cycle_id', type=int)
    if cycle_id:
        q = q.filter(Observation.cycle_id==cycle_id)
    # Department filter (one or many) via ?department=Science&department=Math
    dept_filters = [d for d in request.args.getlist('department') if d]
    if dept_filters:
        q = q.join(Staff).filter(Staff.department.in_(dept_filters))
    observations = q.order_by(Observation.date.desc()).all()

    # group counts per tutor in selected cycle
    grouped = None
    if cycle_id:
        rows = db.session.query(Staff.name, db.func.count(Observation.id)).join(Observation, Observation.staff_id==Staff.id, isouter=True).filter(Observation.cycle_id==cycle_id).group_by(Staff.name).all()
        grouped = [type('Row',(),{'name': r[0], 'count': int(r[1])}) for r in rows]

    cycles = ObservationCycle.query.order_by(ObservationCycle.start_date.desc().nullslast()).all()
    dept_choices = [r[0] for r in db.session.query(Staff.department).distinct().filter(Staff.department.isnot(None)).order_by(Staff.department.asc()).all()]
    return render_template("observations/index.html", observations=observations, cycles=cycles, grouped=grouped, departments=dept_choices, selected_departments=dept_filters)

@app.route("/observations/new", methods=["GET","POST"])
@login_required
def observation_new():
    form = ObservationForm()
    cycles = ObservationCycle.query.order_by(ObservationCycle.start_date.desc().nullslast()).all()
    staff = Staff.query.order_by(Staff.name.asc()).all()
    if request.method == 'POST':
        o = Observation(
            cycle_id=int(request.form.get('cycle_id')),
            staff_id=int(request.form.get('staff_id')),
            observer_id=current_user.id,
            date=datetime.strptime(request.form.get('date'), '%Y-%m-%d').date(),
            score=float(request.form.get('score')),
        )
        db.session.add(o)
        db.session.commit()
        flash("Observation saved", "success")
        return redirect(url_for('observations_index', cycle_id=o.cycle_id))
    return render_template("observations/form.html", form=form, cycles=cycles, staff=staff, obs=None)

@app.route("/observations/<int:oid>/edit", methods=["GET","POST"])
@login_required
def observation_edit(oid):
    o = Observation.query.get_or_404(oid)
    cycles = ObservationCycle.query.order_by(ObservationCycle.start_date.desc().nullslast()).all()
    staff = Staff.query.order_by(Staff.name.asc()).all()
    if request.method == 'POST':
        o.cycle_id = int(request.form.get('cycle_id'))
        o.staff_id = int(request.form.get('staff_id'))
        o.date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
        o.score = float(request.form.get('score'))
        if current_user.id != o.observer_id and not current_user.is_superadmin:
            abort(403)
        db.session.commit()
        flash("Observation updated", "success")
        return redirect(url_for('observations_index', cycle_id=o.cycle_id))
    return render_template("observations/form.html", cycles=cycles, staff=staff, obs=o, form=ObservationForm())

@app.route("/observations/<int:oid>/delete")
@login_required
def observation_delete(oid):
    o = Observation.query.get_or_404(oid)
    cid = o.cycle_id
    db.session.delete(o)
    db.session.commit()
    flash("Observation deleted", "success")
    return redirect(url_for('observations_index', cycle_id=cid))

# ---------------- Extended Observations (rich form + report/email) -----------
def _deserialize_checklist(prefix, form):
    data = {}
    plen = len(prefix) + 1
    for k in form.keys():
        if not k.startswith(prefix + '_') or k.endswith('_comment'):
            continue
        key = k[plen:]
        # Normalise any accidental duplicated prefix (e.g. weekly_test_weekly_test_marked_on_time)
        if key.startswith(prefix + '_'):
            key = key[len(prefix) + 1:]
        data[key] = form.get(k) == '1'
    return data

@app.route('/observations/extended/new', methods=['GET','POST'])
@login_required
def observation_extended_new():
    cycles = ObservationCycle.query.order_by(ObservationCycle.start_date.desc().nullslast()).all()
    staff = Staff.query.order_by(Staff.name.asc()).all()
    if request.method == 'POST':
        errors = []
        staff_raw = request.form.get('staff_id')
        cycle_raw = request.form.get('cycle_id')
        date_raw = request.form.get('date')
        score_raw = request.form.get('score')
        timeslot = request.form.get('timeslot')
        # Validate staff
        try:
            staff_id = int(staff_raw) if staff_raw else None
        except Exception:
            staff_id = None
        if not staff_id:
            errors.append('Tutor is required.')
        # Validate cycle (fallback to first available)
        try:
            cycle_id = int(cycle_raw) if cycle_raw else (cycles[0].id if cycles else None)
        except Exception:
            cycle_id = None
        if not cycle_id:
            errors.append('Cycle is required.')
        # Date
        try:
            date_val = datetime.strptime(date_raw, '%Y-%m-%d').date() if date_raw else date.today()
        except Exception:
            date_val = date.today(); errors.append('Invalid date value.')
        # Score
        try:
            score = float(score_raw)
            if score < 0 or score > 10:
                errors.append('Score must be between 0 and 10.')
        except Exception:
            score = None; errors.append('Observation score is required.')
        # Timeslot
        if timeslot not in ['9-11','11-1','2-4','4-6','5-7']:
            errors.append('Invalid timeslot.')
        if errors:
            # Rebuild user-entered state so the form is NOT wiped
            weekly_test_data = _deserialize_checklist('weekly_test', request.form)
            homework_data = _deserialize_checklist('homework', request.form)
            classwork_data = _deserialize_checklist('classwork', request.form)
            org_mgmt_data = _deserialize_checklist('org_mgmt', request.form)
            # These are raw; convert to normalized mapping for template consistency
            from checklist_utils import normalize_mapping as _norm_map
            weekly_test_data = _norm_map('weekly_test', weekly_test_data)
            homework_data = _norm_map('homework', homework_data)
            classwork_data = _norm_map('classwork', classwork_data)
            org_mgmt_data = _norm_map('org_mgmt', org_mgmt_data)
            # Build a lightweight stub for detail-like attributes accessed in template
            from types import SimpleNamespace
            detail_stub = SimpleNamespace(
                timeslot=timeslot if timeslot in ['9-11','11-1','2-4','4-6','5-7'] else '',
                weekly_test_comment=request.form.get('weekly_test_comment') or None,
                homework_comment=request.form.get('homework_comment') or None,
                classwork_comment=request.form.get('classwork_comment') or None,
                org_mgmt_comment=request.form.get('org_mgmt_comment') or None,
                target_set=request.form.get('target_set') or '',
                actions_taken=request.form.get('actions_taken') or '',
                notes=request.form.get('notes') or '',
                next_review_date=request.form.get('next_review_date') or None,
            )
            # Observation stub for score & staff selection
            obs_stub = SimpleNamespace(
                staff_id=staff_id,
                score=score,
                date=date_val,
            )
            # Dynamic lists
            def _safe_load_list(raw):
                try:
                    return json.loads(raw) if raw else []
                except Exception:
                    return []
            positives_list = _safe_load_list(request.form.get('positives_json'))
            improvements_list = _safe_load_list(request.form.get('improvements_json'))
            flash('\n'.join(errors), 'danger')
            return render_template(
                'observations/extended_form.html',
                staff=staff,
                cycles=cycles,
                obs=obs_stub,
                detail=detail_stub,
                today=date.today(),
                weekly_test_data=weekly_test_data,
                homework_data=homework_data,
                classwork_data=classwork_data,
                org_mgmt_data=org_mgmt_data,
                positives_json=positives_list,
                improvements_json=improvements_list,
                form_errors=errors,
            )
        # Create observation
        obs = Observation(cycle_id=cycle_id, staff_id=staff_id, observer_id=current_user.id, date=date_val, score=score)
        db.session.add(obs); db.session.flush()
        from models import ObservationDetail

        # Targets & actions now provided as JSON arrays (targets_json / actions_json)
        def _join_list(raw):
            try:
                arr = json.loads(raw) if raw else []
                if not isinstance(arr, list):
                    return None
                # Filter out empty/whitespace-only entries
                cleaned = [x.strip() for x in arr if isinstance(x,str) and x.strip()]
                return "\n".join(cleaned) if cleaned else None
            except Exception:
                return None
        target_set_joined = _join_list(request.form.get('targets_json'))
        actions_joined = _join_list(request.form.get('actions_json'))
        detail = ObservationDetail(
            observation_id=obs.id,
            timeslot=timeslot,
            weekly_test=json.dumps(_deserialize_checklist('weekly_test', request.form)),
            weekly_test_comment=request.form.get('weekly_test_comment') or None,
            homework=json.dumps(_deserialize_checklist('homework', request.form)),
            homework_comment=request.form.get('homework_comment') or None,
            classwork=json.dumps(_deserialize_checklist('classwork', request.form)),
            classwork_comment=request.form.get('classwork_comment') or None,
            org_mgmt=json.dumps(_deserialize_checklist('org_mgmt', request.form)),
            org_mgmt_comment=request.form.get('org_mgmt_comment') or None,
            positives=request.form.get('positives_json'),
            improvements=request.form.get('improvements_json'),
            target_set=target_set_joined,
            actions_taken=actions_joined,
            notes=request.form.get('notes') or None,
            next_review_date=datetime.strptime(request.form.get('next_review_date'), '%Y-%m-%d').date() if request.form.get('next_review_date') else None
        )
        db.session.add(detail); db.session.commit()
        flash('Observation created','success')
        return redirect(url_for('observations_index', cycle_id=cycle_id))
    # GET -> blank form
    return render_template('observations/extended_form.html', staff=staff, cycles=cycles, obs=None, detail=None,
                           today=date.today(), weekly_test_data={}, homework_data={}, classwork_data={}, org_mgmt_data={},
                           positives_json=[], improvements_json=[])

@app.route('/observations/extended/<int:oid>/edit', methods=['GET','POST'])
@login_required
def observation_extended_edit(oid):
    from models import ObservationDetail
    obs = Observation.query.get_or_404(oid)
    detail = obs.detail
    if not detail:
        detail = ObservationDetail(observation_id=obs.id)
        db.session.add(detail)
        db.session.commit()
    if request.method == 'POST':
        # Permission: only original observer or superadmin may edit
        if current_user.id != obs.observer_id and not current_user.is_superadmin:
            abort(403)
        errors = []
        try:
            obs.staff_id = int(request.form.get('staff_id'))
        except Exception:
            errors.append('Invalid tutor.')
        try:
            obs.date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
        except Exception:
            errors.append('Invalid date.')
        try:
            score_val = float(request.form.get('score'))
            if score_val < 0 or score_val > 10:
                errors.append('Score must be 0-10.')
            obs.score = score_val
        except Exception:
            errors.append('Score is required.')
        ts = request.form.get('timeslot')
        if ts not in ['9-11','11-1','2-4','4-6','5-7']:
            errors.append('Invalid timeslot.')
        detail.timeslot = ts
        detail.weekly_test = json.dumps(_deserialize_checklist('weekly_test', request.form))
        detail.weekly_test_comment = request.form.get('weekly_test_comment') or None
        detail.homework = json.dumps(_deserialize_checklist('homework', request.form))
        detail.homework_comment = request.form.get('homework_comment') or None
        detail.classwork = json.dumps(_deserialize_checklist('classwork', request.form))
        detail.classwork_comment = request.form.get('classwork_comment') or None
        detail.org_mgmt = json.dumps(_deserialize_checklist('org_mgmt', request.form))
        detail.org_mgmt_comment = request.form.get('org_mgmt_comment') or None
        detail.positives = request.form.get('positives_json')
        detail.improvements = request.form.get('improvements_json')
        # New JSON-based targets/actions lists
        try:
            tgt_list = json.loads(request.form.get('targets_json') or '[]')
            if isinstance(tgt_list, list):
                detail.target_set = "\n".join([t.strip() for t in tgt_list if isinstance(t,str) and t.strip()]) or None
        except Exception:
            pass
        try:
            act_list = json.loads(request.form.get('actions_json') or '[]')
            if isinstance(act_list, list):
                detail.actions_taken = "\n".join([t.strip() for t in act_list if isinstance(t,str) and t.strip()]) or None
        except Exception:
            pass
        detail.notes = request.form.get('notes') or None
        detail.next_review_date = datetime.strptime(request.form.get('next_review_date'), '%Y-%m-%d').date() if request.form.get('next_review_date') else None
        if errors:
            for e in errors: flash(e,'danger')
        else:
            db.session.commit()
            flash('Observation updated','success')
            return redirect(url_for('observation_extended_edit', oid=obs.id))
    def load_json(text, attr):
        try:
            raw = json.loads(text) if text else {}
        except Exception:
            raw = {}
        from checklist_utils import normalize_mapping as _norm_map
        return _norm_map(attr, raw)
    def load_list(text):
        try: return json.loads(text) if text else []
        except Exception: return []
    cycles = ObservationCycle.query.order_by(ObservationCycle.start_date.desc().nullslast()).all()
    staff = Staff.query.order_by(Staff.name.asc()).all()
    return render_template('observations/extended_form.html', obs=obs, detail=detail, staff=staff, cycles=cycles,
                           today=date.today(), weekly_test_data=load_json(detail.weekly_test,'weekly_test'), homework_data=load_json(detail.homework,'homework'),
                           classwork_data=load_json(detail.classwork,'classwork'), org_mgmt_data=load_json(detail.org_mgmt,'org_mgmt'),
                           positives_json=load_list(detail.positives), improvements_json=load_list(detail.improvements))

@app.route('/observations/<int:oid>/report')
@login_required
def observation_report(oid):
    obs = Observation.query.get_or_404(oid)
    detail = obs.detail
    if not detail:
        abort(404)
    # Embed logo as data URI for portability in PDF/email rendering
    logo_data_uri = None
    try:
        logo_path = os.path.join(app.root_path, 'static', 'img', 'excel tutors logo 2023.png')
        with open(logo_path, 'rb') as lf:
            b64 = base64.b64encode(lf.read()).decode('utf-8')
            logo_data_uri = f"data:image/png;base64,{b64}"
    except Exception:
        pass
    # Render freshly rebuilt PDF template
    html = render_template('observations/report_pdf.html', obs=obs, detail=detail, data=detail.serialize_all(), logo_data_uri=logo_data_uri, generated_at=datetime.now(timezone.utc))
    try:
        from io import BytesIO

        from xhtml2pdf import pisa
        pdf_io = BytesIO(); pisa.CreatePDF(html, dest=pdf_io); pdf_io.seek(0)
        return send_file(pdf_io, mimetype='application/pdf', as_attachment=True, download_name=f'observation_{oid}.pdf')
    except Exception:
        # Fallback: return raw HTML if PDF generation fails
        return html

@app.route('/observations/<int:oid>/email')
@login_required
def observation_email(oid):
    obs = Observation.query.get_or_404(oid)
    detail = obs.detail
    if not detail:
        abort(404)
    logo_data_uri = None
    try:
        logo_path = os.path.join(app.root_path, 'static', 'img', 'excel tutors logo 2023.png')
        with open(logo_path, 'rb') as lf:
            b64 = base64.b64encode(lf.read()).decode('utf-8')
            logo_data_uri = f"data:image/png;base64,{b64}"
    except Exception:
        pass
    # Render PDF template (for attachment) and an email-friendly template (static check symbols instead of form inputs)
    pdf_html = render_template('observations/report_pdf.html', obs=obs, detail=detail, data=detail.serialize_all(), logo_data_uri=logo_data_uri, generated_at=datetime.now(timezone.utc))
    email_html = render_template('observations/report_email.html', obs=obs, detail=detail, data=detail.serialize_all(), logo_data_uri=logo_data_uri, generated_at=datetime.now(timezone.utc))
    tutor_email = obs.staff.email
    ajax = request.args.get('ajax') or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept','')
    if not tutor_email:
        msg = 'Tutor has no email on record'
        if ajax:
            return jsonify({'status':'error','message': msg}), 400
        flash(msg,'warning')
        return redirect(url_for('observation_extended_edit', oid=obs.id))
    pdf_bytes = None
    try:
        from io import BytesIO

        from xhtml2pdf import pisa

        # Generate PDF from the same HTML we are embedding in the email body
        pdf_io = BytesIO(); pisa.CreatePDF(pdf_html, dest=pdf_io); pdf_io.seek(0); pdf_bytes = pdf_io.read()
    except Exception:
        pdf_bytes = None
    # Email body = standalone HTML (already includes styling & summary)
    body = email_html
    try:
        if pdf_bytes:
            import smtplib
            from email.message import EmailMessage

            from email_utils import (FROM_EMAIL, FROM_NAME, SMTP_HOST,
                                     SMTP_PASSWORD, SMTP_PORT, SMTP_USERNAME)
            msg = EmailMessage(); msg['Subject'] = f"Observation Report - {obs.staff.name} ({obs.date})"; msg['From'] = f"{FROM_NAME} <{FROM_EMAIL}>"; msg['To'] = tutor_email
            msg.set_content('Observation report attached (HTML + PDF).')
            msg.add_alternative(body, subtype='html')
            msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=f'observation_{oid}.pdf')
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.starttls(); server.login(SMTP_USERNAME, SMTP_PASSWORD); server.send_message(msg)
        else:
            send_email(tutor_email, f"Observation Report - {obs.staff.name} ({obs.date})", body)
        success_msg = 'Observation emailed to tutor'
        if ajax:
            return jsonify({'status':'ok','message': success_msg})
        flash(success_msg,'success')
    except Exception as e:
        err_msg = f'Email failed: {e}'
        if ajax:
            return jsonify({'status':'error','message': err_msg}), 500
        flash(err_msg,'danger')
    return redirect(url_for('observation_extended_edit', oid=obs.id))

@app.route('/observations/<int:oid>/debug_checklist')
@login_required
def observation_debug_checklist(oid):
    obs = Observation.query.get_or_404(oid)
    if not obs.detail:
        return jsonify({'error':'no detail'}), 404
    data = obs.detail.serialize_all()
    # Show counts of true flags per group
    def true_keys(mapping):
        return sorted([k for k,v in (mapping or {}).items() if v])
    return jsonify({
        'weekly_test_true': true_keys(data.get('weekly_test')),
        'homework_true': true_keys(data.get('homework')),
        'classwork_true': true_keys(data.get('classwork')),
        'org_mgmt_true': true_keys(data.get('org_mgmt')),
    })

# ---------------- Invoicing ---------------- #
from sqlalchemy.exc import IntegrityError


def _parse_date(val):
    try:
        if not val:
            return None
        return datetime.strptime(val.strip(), '%Y-%m-%d').date()
    except Exception:
        try:
            return datetime.strptime(val.strip(), '%d/%m/%Y').date()
        except Exception:
            return None

@app.route('/companies', methods=['GET'])
@login_required
@permission_required('manage_invoices')
def companies_index():
    form = CompanyForm()
    companies = Company.query.order_by(Company.name.asc()).all()
    from sqlalchemy import func
    total_companies = len(companies)
    total_invoices = db.session.query(func.count(Invoice.id)).scalar() or 0
    unpaid_total = db.session.query(func.coalesce(func.sum(Invoice.total), 0)).filter(Invoice.status=='UNPAID').scalar() or 0
    paid_total = db.session.query(func.coalesce(func.sum(Invoice.total), 0)).filter(Invoice.status=='PAID').scalar() or 0
    latest_invoice_date = db.session.query(func.max(Invoice.invoice_date)).scalar()
    company_stats = {}
    # per-company quick aggregates
    for c in companies:
        counts = db.session.query(func.count(Invoice.id), func.coalesce(func.sum(Invoice.total),0)).filter(Invoice.company_id==c.id).first()
        unpaid_sum = db.session.query(func.coalesce(func.sum(Invoice.total),0)).filter(Invoice.company_id==c.id, Invoice.status=='UNPAID').scalar()
        company_stats[c.id] = {
            'count': counts[0] if counts else 0,
            'total_sum': float(counts[1]) if counts else 0.0,
            'unpaid_sum': float(unpaid_sum) if unpaid_sum else 0.0,
        }
    return render_template('invoices/companies.html', form=form, companies=companies,
                           total_companies=total_companies, total_invoices=total_invoices,
                           unpaid_total=unpaid_total, paid_total=paid_total,
                           latest_invoice_date=latest_invoice_date, company_stats=company_stats)

@app.route('/companies/<int:company_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_invoices')
def company_delete(company_id):
    company = Company.query.get_or_404(company_id)
    reassign_id = request.form.get('reassign_company_id', type=int)
    force = bool(request.form.get('force')) and getattr(current_user, 'is_superadmin', False)
    delete_invoices = bool(request.form.get('delete_invoices')) and getattr(current_user, 'is_superadmin', False)
    invoices = Invoice.query.filter_by(company_id=company.id).all()
    if invoices:
        if reassign_id:
            target = Company.query.get(reassign_id)
            if not target:
                flash('Reassign target company not found.', 'danger')
                return redirect(url_for('companies_index'))
            for inv in invoices:
                inv.company_id = target.id
            db.session.commit()
            flash(f'Reassigned {len(invoices)} invoice(s) to {target.name}.', 'success')
        elif force:
            # If superadmin forced deletion, treat delete_invoices flag as implied to avoid orphan invoices.
            count = len(invoices)
            for inv in invoices:
                db.session.delete(inv)
            db.session.flush()
            flash(f'Superadmin destructive delete: removed {count} invoice(s).', 'danger')
        elif getattr(current_user, 'is_superadmin', False):
            # Convenience: superadmin pressed Delete without selecting options—auto destructive delete.
            count = len(invoices)
            for inv in invoices:
                db.session.delete(inv)
            db.session.flush()
            flash(f'Superadmin auto-delete: removed {count} invoice(s).', 'warning')
        else:
            flash('Cannot delete company with existing invoices (choose reassign or superadmin force).', 'warning')
            return redirect(url_for('companies_index'))
    # After reassignment (or if no invoices) safe to delete
    if Invoice.query.filter_by(company_id=company.id).count() == 0:
        db.session.delete(company)
        db.session.commit()
        flash('Company deleted', 'success')
    else:
        flash('Company not deleted (invoices still attached).', 'warning')
    return redirect(url_for('companies_index'))

@app.route('/companies/bulk-delete', methods=['POST'])
@login_required
@permission_required('manage_invoices')
def companies_bulk_delete():
    ids_param = request.args.get('ids','').strip()
    if not ids_param:
        flash('No companies selected.', 'warning')
        return redirect(url_for('companies_index'))
    id_list = []
    for part in ids_param.split(','):
        try:
            id_list.append(int(part))
        except Exception:
            continue
    if not id_list:
        flash('No valid company IDs provided.', 'warning')
        return redirect(url_for('companies_index'))
    reassign_id = request.form.get('reassign_company_id', type=int)
    force = bool(request.form.get('force')) and getattr(current_user, 'is_superadmin', False)
    delete_invoices = bool(request.form.get('delete_invoices')) and getattr(current_user, 'is_superadmin', False)
    reassigned = 0
    deleted = 0
    skipped = 0
    target = Company.query.get(reassign_id) if reassign_id else None
    for cid in id_list:
        company = Company.query.get(cid)
        if not company:
            continue
        invoices = Invoice.query.filter_by(company_id=company.id).all()
        if invoices:
            if target:
                for inv in invoices:
                    inv.company_id = target.id
                reassigned += len(invoices)
                db.session.flush()
            elif force:
                # Force implies destructive invoice deletion for bulk consistency
                for inv in invoices:
                    db.session.delete(inv)
            else:
                skipped += 1
                continue
        # Delete only if no remaining invoices
        if Invoice.query.filter_by(company_id=company.id).count() == 0:
            db.session.delete(company)
            deleted += 1
    db.session.commit()
    msg = f"Bulk action complete: {deleted} deleted"
    if reassigned:
        msg += f", {reassigned} invoices reassigned"
    if skipped:
        msg += f", {skipped} skipped"
    flash(msg+".", 'success')
    return redirect(url_for('companies_index'))

@app.route('/companies/<int:company_id>/json', methods=['GET'])
@login_required
@permission_required('manage_invoices')
def company_json(company_id):
    """Return a JSON representation of a company for the inline edit modal.

    The invoices/companies.html Alpine component fetches this endpoint to
    populate the edit form. Keep the field names in sync with the front-end
    formData keys.
    """
    company = Company.query.get_or_404(company_id)
    payload = {
        'id': company.id,
        'name': company.name or '',
        'invoice_prefix': company.invoice_prefix or '',
        'next_invoice_seq': company.next_invoice_seq or 1,
        'payment_footer': company.payment_footer or '',
        'tagline': company.tagline or '',
        'ofsted_reg_no': company.ofsted_reg_no or '',
        'address': company.address or '',
        'phone': company.phone or '',
        'email': company.email or '',
        'website': company.website or '',
        'logo_path': company.logo_path or '',
    }
    return jsonify(payload)

# -------- Student autocomplete (meetings) -------- #
@app.route('/api/students/suggest')
@login_required
def api_student_suggest():
    """Return up to 20 students matching q in id or name.

    Response format: [{id, label, name, student_id}]
    Label is formatted as "<student_id>-<name>" for direct insertion.
    """
    q = (request.args.get('q') or '').strip().lower()
    limit = min(int(request.args.get('limit') or 20), 50)
    if not q:
        return jsonify([])
    students = (Student.query
                .filter(
                    and_(
                        Student.is_active.is_(True),
                        or_(
                            Student.name.ilike(f"%{q}%"),
                            Student.student_id.ilike(f"%{q}%")
                        )
                    )
                )
                .order_by(Student.name.asc())
                .limit(limit)
                .all())
    out = []
    for s in students:
        sid = (s.student_id or '').strip()
        name = (s.name or '').strip()
        label = f"{sid}-{name}" if sid else name
        out.append({
            'id': s.id,
            'student_id': sid,
            'name': name,
            'label': label,
        })
    return jsonify(out)

def _save_company_logo(file, company_name):
    if not file or not file.filename:
        return None
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in {'.png', '.jpg', '.jpeg'}:
        return None
    safe_name = f"company_{company_name.lower().replace(' ','_')}{ext}"
    upload_dir = os.path.join(app.root_path, 'static', 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, safe_name)
    file.save(path)
    rel = os.path.relpath(path, os.path.join(app.root_path, 'static'))
    return rel

@app.route('/companies/create', methods=['POST'])
@login_required
@permission_required('manage_invoices')
def company_create():
    form = CompanyForm()
    if form.validate_on_submit():
        name = form.name.data.strip()
        if Company.query.filter_by(name=name).first():
            flash('Company with that name already exists','danger')
            return redirect(url_for('companies_index'))
        company = Company(name=name)
        company.invoice_prefix = form.invoice_prefix.data or 'INV-'
        try:
            if form.next_invoice_seq.data:
                company.next_invoice_seq = int(form.next_invoice_seq.data)
        except Exception:
            pass
        company.payment_footer = form.payment_footer.data or company.payment_footer
        company.tagline = form.tagline.data or company.tagline
        company.ofsted_reg_no = form.ofsted_reg_no.data or company.ofsted_reg_no
        company.address = form.address.data or company.address
        company.phone = form.phone.data or company.phone
        company.email = form.email.data or company.email
        company.website = form.website.data or company.website
        logo_rel = _save_company_logo(request.files.get('logo'), name)
        if logo_rel:
            company.logo_path = logo_rel
        db.session.add(company)
        db.session.commit()
        flash('Company created','success')
    else:
        flash('Failed to create company','danger')
    return redirect(url_for('companies_index'))

@app.route('/companies/<int:company_id>/update', methods=['POST'])
@login_required
@permission_required('manage_invoices')
def company_update(company_id):
    company = Company.query.get_or_404(company_id)
    form = CompanyForm()
    if form.validate_on_submit():
        company.name = form.name.data.strip()
        company.invoice_prefix = form.invoice_prefix.data or company.invoice_prefix
        try:
            if form.next_invoice_seq.data:
                company.next_invoice_seq = int(form.next_invoice_seq.data)
        except Exception:
            pass
        if form.payment_footer.data:
            company.payment_footer = form.payment_footer.data
        company.tagline = form.tagline.data or company.tagline
        company.ofsted_reg_no = form.ofsted_reg_no.data or company.ofsted_reg_no
        company.address = form.address.data or company.address
        company.phone = form.phone.data or company.phone
        company.email = form.email.data or company.email
        company.website = form.website.data or company.website
        logo_rel = _save_company_logo(request.files.get('logo'), company.name)
        if logo_rel:
            company.logo_path = logo_rel
        db.session.commit()
        flash('Company updated','success')
    else:
        flash('Failed to update company','danger')
    return redirect(url_for('companies_index'))


@app.route('/invoices')
@login_required
@permission_required('manage_invoices')
def invoices_index():
    # Safety: ensure new column exists for legacy databases (after deployment without restart)
    try:
        from sqlalchemy import text as _text2
        with db.engine.connect() as _c:
            cols = {r[1] for r in _c.execute(_text2("PRAGMA table_info(invoice)"))}
            if 'created_by_id' not in cols:
                try:
                    _c.execute(_text2("ALTER TABLE invoice ADD COLUMN created_by_id INTEGER"))
                except Exception:
                    pass
    except Exception:
        pass
    q = (request.args.get('q') or '').strip()
    company_filter = request.args.get('company', type=int)
    month_filter = (request.args.get('month') or '').strip()
    status_filter = (request.args.get('status') or '').strip().upper()
    sort_key = (request.args.get('sort') or 'created_at').strip()
    direction = (request.args.get('direction') or 'desc').strip().lower()
    page = max(1, int(request.args.get('page', 1) or 1))
    per_page = min(200, max(5, int(request.args.get('per_page', 50) or 50)))

    base_query = Invoice.query.options(joinedload(Invoice.company), joinedload(Invoice.created_by)).join(Company)

    if q:
        like = f"%{q.lower()}%"
        base_query = base_query.filter(
            or_(
                db.func.lower(Invoice.invoice_no).like(like),
                db.func.lower(Invoice.parent_name).like(like),
                db.func.lower(Invoice.child_name).like(like),
            )
        )

    if company_filter:
        base_query = base_query.filter(Invoice.company_id == company_filter)

    if status_filter in {'PAID', 'UNPAID'}:
        base_query = base_query.filter(Invoice.status == status_filter)

    if month_filter:
        try:
            month_dt = datetime.strptime(month_filter, '%Y-%m')
            start_date = month_dt.replace(day=1).date()
            if month_dt.month == 12:
                next_month = month_dt.replace(year=month_dt.year + 1, month=1, day=1)
            else:
                next_month = month_dt.replace(month=month_dt.month + 1, day=1)
            end_date = (next_month - timedelta(days=1)).date()
            base_query = base_query.filter(Invoice.invoice_date >= start_date, Invoice.invoice_date <= end_date)
        except ValueError:
            pass

    sort_map = {
        'invoice_no': Invoice.invoice_no,
        'company': Company.name,
        'invoice_date': Invoice.invoice_date,
        'due_date': Invoice.due_date,
        'total': Invoice.total,
        'parent_name': Invoice.parent_name,
        'child_name': Invoice.child_name,
        'status': Invoice.status,
        'created_at': Invoice.created_at,
    }
    sort_column = sort_map.get(sort_key, Invoice.created_at)
    if direction == 'asc':
        base_query = base_query.order_by(sort_column.asc())
    else:
        base_query = base_query.order_by(sort_column.desc())

    # For stats we need the full filtered set; for page we limit
    all_invoices = base_query.all()
    total = len(all_invoices)
    pages = (total // per_page) + (1 if total % per_page else 0) or 1
    if page > pages:
        page = pages
    invoices = all_invoices[(page-1)*per_page: page*per_page]
    paid_count = sum(1 for inv in all_invoices if (inv.status or '').upper() == 'PAID')
    unpaid_count = sum(1 for inv in all_invoices if (inv.status or '').upper() == 'UNPAID')
    total_amount = float(sum((inv.total or 0) for inv in all_invoices))
    unpaid_amount = float(sum((inv.total or 0) for inv in all_invoices if (inv.status or '').upper() == 'UNPAID'))

    inv_stats = SimpleNamespace(
        count=total,
        paid=paid_count,
        unpaid=unpaid_count,
        total_amount=total_amount,
        unpaid_amount=unpaid_amount,
    ) if all_invoices else None

    company_rollup = {}
    for inv in all_invoices:
        bucket = company_rollup.setdefault(inv.company_id, {
            'name': inv.company.name if inv.company else 'Unknown',
            'count': 0,
            'paid': 0,
            'unpaid': 0,
            'total_amount': 0.0,
            'unpaid_amount': 0.0,
        })
        bucket['count'] += 1
        bucket['total_amount'] += float(inv.total or 0)
        if (inv.status or '').upper() == 'PAID':
            bucket['paid'] += 1
        else:
            bucket['unpaid'] += 1
            bucket['unpaid_amount'] += float(inv.total or 0)

    company_stats = sorted(
        (
            SimpleNamespace(
                id=cid,
                name=data['name'],
                count=data['count'],
                paid=data['paid'],
                unpaid=data['unpaid'],
                total_amount=data['total_amount'],
                unpaid_amount=data['unpaid_amount'],
            )
            for cid, data in company_rollup.items()
        ),
        key=lambda item: item.name.lower(),
    )

    companies = Company.query.order_by(Company.name.asc()).all()

    return render_template(
        'invoices/index.html',
        invoices=invoices,
        companies=companies,
        inv_stats=inv_stats,
        company_stats=company_stats,
        page=page,
        pages=pages,
        per_page=per_page,
        total=total,
    )


@app.route('/invoices/new', methods=['GET', 'POST'])
@login_required
@permission_required('manage_invoices')
def invoices_new():
    form = InvoiceForm()
    # Ensure column exists (defensive; may have been added after process start)
    try:
        from sqlalchemy import text as _text2
        with db.engine.connect() as _c:
            if 'created_by_id' not in {r[1] for r in _c.execute(_text2("PRAGMA table_info(invoice)"))}:
                try: _c.execute(_text2("ALTER TABLE invoice ADD COLUMN created_by_id INTEGER"))
                except Exception: pass
    except Exception:
        pass
    companies = Company.query.order_by(Company.name.asc()).all()
    form.company_id.choices = [(c.id, c.name) for c in companies]

    if form.validate_on_submit():
        company = Company.query.get(form.company_id.data)
        if not company:
            flash('Selected company was not found','danger')
            return redirect(url_for('invoices_new'))
        invoice_no = company.generate_invoice_no()
        invoice = Invoice(
            invoice_no=invoice_no,
            company_id=company.id,
            created_by_id=current_user.id,
            invoice_date=form.invoice_date.data,
            due_date=form.due_date.data,
            parent_name=form.parent_name.data,
            parent_phone=form.parent_phone.data,
            parent_email=form.parent_email.data,
            parent_address=form.parent_address.data,
            child_name=form.child_name.data,
            period_start=form.period_start.data,
            period_end=form.period_end.data,
            sub_total=form.sub_total.data,
            total=form.total.data,
            status=form.status.data,
            notes=form.notes.data,
        )
        db.session.add(invoice)
        company.next_invoice_seq = (company.next_invoice_seq or 1) + 1
        db.session.commit()
        flash(f'Invoice {invoice.invoice_no} created','success')
        return redirect(url_for('invoice_detail', invoice_id=invoice.id))

    if request.method == 'GET':
        today = date.today()
        form.invoice_date.data = today
        form.due_date.data = today
        form.period_start.data = today
        form.period_end.data = today
        form.status.data = form.status.data or 'PAID'

    return render_template('invoices/form.html', form=form, mode='new')

@app.route('/invoices/import', methods=['POST'])
@login_required
@permission_required('manage_invoices')
def invoices_import():
    """Import invoices from an uploaded CSV file.
    Expected headers (case-insensitive): company, invoice_date, due_date, parent_name, parent_phone, parent_email, parent_address, child_name, period_start, period_end, sub_total, total, status, notes, invoice_no(optional)
    Dates accepted as YYYY-MM-DD or DD-MM-YYYY.
    Missing invoice_no -> generated. Missing status -> UNPAID.
    If company (by exact name) not found row skipped. Existing invoice_no skipped.
    """
    upload = request.files.get('file')
    if not upload:
        flash('No file provided','danger')
        return redirect(url_for('invoices_index'))
    try:
        content = upload.read().decode('utf-8', errors='ignore')
    except Exception as e:
        flash(f'Failed to read file: {e}','danger')
        return redirect(url_for('invoices_index'))
    reader = csv.DictReader(io.StringIO(content))
    companies_cache = {c.name.strip().lower(): c for c in Company.query.all()}
    existing_numbers = {n for (n,) in db.session.query(Invoice.invoice_no).all()}
    added = 0
    skipped = 0

    def parse_date(val):
        if not val:
            return None
        val = val.strip()
        for fmt in ('%Y-%m-%d','%d-%m-%Y','%Y/%m/%d','%d/%m/%Y'):
            try:
                return datetime.strptime(val, fmt).date()
            except Exception:
                continue
        return None

    for raw_row in reader:
        row = { (k or '').strip().lower(): (v or '').strip() for k,v in raw_row.items() }
        company_name = row.get('company')
        if not company_name:
            skipped += 1
            continue
        company = companies_cache.get(company_name.lower())
        if not company:
            skipped += 1
            continue
        invoice_no = row.get('invoice_no') or company.generate_invoice_no()
        if invoice_no in existing_numbers:
            skipped += 1
            continue
        try:
            invoice_date = parse_date(row.get('invoice_date')) or date.today()
            due_date = parse_date(row.get('due_date')) or (invoice_date + timedelta(days=14))
            period_start = parse_date(row.get('period_start')) or invoice_date.replace(day=1)
            period_end = parse_date(row.get('period_end')) or invoice_date
            sub_total = Decimal(row.get('sub_total') or '0')
            total = Decimal(row.get('total') or '0')
            status = (row.get('status') or 'UNPAID').upper()
            notes = row.get('notes') or None
            inv = Invoice(
                invoice_no=invoice_no,
                company_id=company.id,
                created_by_id=current_user.id if current_user.is_authenticated else None,
                invoice_date=invoice_date,
                due_date=due_date,
                parent_name=row.get('parent_name') or 'Parent',
                parent_phone=row.get('parent_phone') or None,
                parent_email=row.get('parent_email') or None,
                parent_address=row.get('parent_address') or None,
                child_name=row.get('child_name') or 'Child',
                period_start=period_start,
                period_end=period_end,
                sub_total=sub_total,
                total=total,
                status=status if status in ('PAID','UNPAID') else 'UNPAID',
                notes=notes,
            )
            db.session.add(inv)
            existing_numbers.add(invoice_no)
            if invoice_no.startswith(company.invoice_prefix or 'INV-'):
                company.next_invoice_seq = (company.next_invoice_seq or 1) + 1
            added += 1
        except Exception:
            skipped += 1
            continue

    if added:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('Database error committing imported invoices','danger')
            return redirect(url_for('invoices_index'))
    flash(f'Imported {added} invoices, skipped {skipped}', 'success' if added else 'warning')
    return redirect(url_for('invoices_index'))


@app.route('/invoices/<int:invoice_id>')
@login_required
@permission_required('manage_invoices')
def invoice_detail(invoice_id):
    invoice = Invoice.query.options(joinedload(Invoice.company)).get_or_404(invoice_id)
    return render_template('invoices/detail.html', invoice=invoice)

@app.route('/invoices/<int:invoice_id>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_invoices')
def invoice_edit(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    form = InvoiceForm(obj=inv)
    companies = Company.query.order_by(Company.name.asc()).all()
    form.company_id.choices = [(c.id, c.name) for c in companies]
    if form.validate_on_submit():
        company = Company.query.get(form.company_id.data)
        if not company:
            flash('Company not found','danger')
            return redirect(url_for('invoice_edit', invoice_id=invoice_id))
        inv.company_id = company.id
        inv.invoice_date = form.invoice_date.data
        inv.due_date = form.due_date.data
        inv.parent_name = form.parent_name.data
        inv.parent_phone = form.parent_phone.data
        inv.parent_email = form.parent_email.data
        inv.parent_address = form.parent_address.data
        inv.child_name = form.child_name.data
        inv.period_start = form.period_start.data
        inv.period_end = form.period_end.data
        inv.sub_total = form.sub_total.data
        inv.total = form.total.data
        inv.status = form.status.data
        inv.notes = form.notes.data
        db.session.commit()
        flash('Invoice updated','success')
        return redirect(url_for('invoice_detail', invoice_id=inv.id))
    return render_template('invoices/form.html', form=form, mode='edit', invoice=inv)

@app.route('/invoices/<int:invoice_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_invoices')
def invoice_delete(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    if (inv.status or '').upper() == 'PAID':
        flash('Cannot delete a PAID invoice','warning')
        return redirect(url_for('invoice_detail', invoice_id=inv.id))
    db.session.delete(inv)
    db.session.commit()
    flash(f'Invoice {inv.invoice_no} deleted','success')
    return redirect(url_for('invoices_index'))

@app.route('/invoices/export')
@login_required
@permission_required('manage_invoices')
def invoices_export():
    """Export filtered invoices as CSV (uses same filters as index)."""
    # Reuse filtering logic via an internal request context mimic
    q = (request.args.get('q') or '').strip()
    company_filter = request.args.get('company', type=int)
    month_filter = (request.args.get('month') or '').strip()
    status_filter = (request.args.get('status') or '').strip().upper()
    sort_key = (request.args.get('sort') or 'created_at').strip()
    direction = (request.args.get('direction') or 'desc').strip().lower()

    query = Invoice.query.options(joinedload(Invoice.company)).join(Company)
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(or_(db.func.lower(Invoice.invoice_no).like(like), db.func.lower(Invoice.parent_name).like(like), db.func.lower(Invoice.child_name).like(like)))
    if company_filter:
        query = query.filter(Invoice.company_id == company_filter)
    if status_filter in {'PAID','UNPAID'}:
        query = query.filter(Invoice.status == status_filter)
    if month_filter:
        try:
            month_dt = datetime.strptime(month_filter, '%Y-%m')
            start_date = month_dt.replace(day=1).date()
            if month_dt.month == 12:
                next_month = month_dt.replace(year=month_dt.year + 1, month=1, day=1)
            else:
                next_month = month_dt.replace(month=month_dt.month + 1, day=1)
            end_date = (next_month - timedelta(days=1)).date()
            query = query.filter(Invoice.invoice_date >= start_date, Invoice.invoice_date <= end_date)
        except ValueError:
            pass
    sort_map = {
        'invoice_no': Invoice.invoice_no,
        'company': Company.name,
        'invoice_date': Invoice.invoice_date,
        'due_date': Invoice.due_date,
        'total': Invoice.total,
        'parent_name': Invoice.parent_name,
        'child_name': Invoice.child_name,
        'status': Invoice.status,
        'created_at': Invoice.created_at,
    }
    sort_column = sort_map.get(sort_key, Invoice.created_at)
    if direction == 'asc':
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())
    rows = query.all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['invoice_no','company','invoice_date','due_date','parent_name','child_name','period_start','period_end','sub_total','total','status','created_at'])
    for r in rows:
        writer.writerow([
            r.invoice_no,
            (r.company.name if r.company else ''),
            r.invoice_date.isoformat() if r.invoice_date else '',
            r.due_date.isoformat() if r.due_date else '',
            r.parent_name,
            r.child_name,
            r.period_start.isoformat() if r.period_start else '',
            r.period_end.isoformat() if r.period_end else '',
            f"{r.sub_total:.2f}" if r.sub_total is not None else '',
            f"{r.total:.2f}" if r.total is not None else '',
            r.status,
            r.created_at.isoformat() if r.created_at else ''
        ])
    csv_data = output.getvalue()
    resp = make_response(csv_data)
    resp.headers['Content-Type'] = 'text/csv'
    resp.headers['Content-Disposition'] = 'attachment; filename=invoices_export.csv'
    return resp


@app.route('/invoices/<int:invoice_id>/pdf')
@login_required
@permission_required('manage_invoices')
def invoice_pdf(invoice_id):
    """Printable invoice document.

    We no longer generate a binary PDF server-side (previous xhtml2pdf approach
    removed due to rendering issues). Instead we return a print‑optimized HTML
    that users can print or "Save as PDF" via the browser dialog.

    Query params:
      ?print=1  -> auto-open print dialog
      (legacy) ?download=1 treated the same as print=1 for backward compatibility
    """
    invoice = Invoice.query.get_or_404(invoice_id)
    legacy_download = str(request.args.get('download','0')).lower() in ('1','true','yes','y')
    print_flag = legacy_download or (str(request.args.get('print','0')).lower() in ('1','true','yes','y'))

    # Inline CSS to ensure the print view is self-contained (robust if user saves page)
    css_path = os.path.join(app.root_path, 'static', 'css', 'invoice.css')
    inline_css = ''
    try:
        with open(css_path, 'r', encoding='utf-8') as f:
            inline_css = f.read()
    except Exception:
        pass

    resp = make_response(render_template(
        'invoices/invoice_document.html',
        invoice=invoice,
        inline_css=inline_css,
        print_view=print_flag,
        logo_abs=None  # use web path for browser rendering
    ))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

# ---------------- Invoice Emailing ---------------- #
INVOICE_SMTP_HOST = "smtp.gmail.com"
INVOICE_SMTP_PORT = 587
INVOICE_SMTP_USER = "info@brightstarkidsclub.org.uk"
INVOICE_SMTP_PASS = "txxi aajf fcug hcia"
INVOICE_SMTP_USE_TLS = True
EMAIL_FROM_NAME = "BrightStar Kids Club"
EMAIL_FROM_ADDR = INVOICE_SMTP_USER
EMAIL_SUBJECT_PREFIX = "[Invoice] "

def send_invoice_email(inv: Invoice):
    import smtplib
    from email.message import EmailMessage
    if not inv.parent_email:
        raise ValueError("Recipient email missing (parent_email)")
    css_path = os.path.join(app.root_path, 'static', 'css', 'invoice.css')
    inline_css = ''
    try:
        with open(css_path, 'r', encoding='utf-8') as f:
            inline_css = f.read()
    except Exception:
        pass
    html = render_template('invoices/invoice_document.html', invoice=inv, inline_css=inline_css, print_view=False, logo_abs=None)
    msg = EmailMessage()
    msg['Subject'] = f"{EMAIL_SUBJECT_PREFIX}{inv.invoice_no}"
    msg['From'] = f"{EMAIL_FROM_NAME} <{EMAIL_FROM_ADDR}>"
    msg['To'] = inv.parent_email
    msg.set_content('HTML invoice attached. If you cannot view HTML, contact support.')
    msg.add_alternative(html, subtype='html')
    with smtplib.SMTP(INVOICE_SMTP_HOST, INVOICE_SMTP_PORT) as server:
        if INVOICE_SMTP_USE_TLS:
            server.starttls()
        server.login(INVOICE_SMTP_USER, INVOICE_SMTP_PASS)
        server.send_message(msg)
    return True

@app.route('/invoices/<int:invoice_id>/email', methods=['POST'])
@login_required
@permission_required('manage_invoices')
def invoice_email(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    try:
        send_invoice_email(inv)
        flash(f"Invoice {inv.invoice_no} emailed to {inv.parent_email}", 'success')
    except Exception as e:
        flash(f"Email failed: {e}", 'danger')
    return redirect(url_for('invoice_detail', invoice_id=inv.id))

# ---------------- Error Handlers ----------------
@app.errorhandler(403)
def forbidden(e):  # noqa: D401
    return render_template("errors/403.html", description=getattr(e, 'description', None)), 403

@app.errorhandler(404)
def not_found(e):  # noqa: D401
    return render_template("errors/404.html"), 404

@app.errorhandler(500)
def server_error(e):  # noqa: D401
    # In case a DB transaction is mid-flight, roll it back.
    try:
        db.session.rollback()
    except Exception:
        pass
    # Cache traceback info in session for optional reporting
    import sys as _sys
    import traceback as _tb
    exc_type, exc_value, exc_tb = _sys.exc_info()
    trace_text = ''.join(_tb.format_exception(exc_type, exc_value, exc_tb)) if exc_type else None
    # Truncate extremely large tracebacks to 20k chars to avoid oversized session blobs
    if trace_text and len(trace_text) > 20000:
        trace_text = trace_text[:20000] + '\n... [truncated]'  # safe truncation marker
    if session is not None:
        session['__last_error__'] = {
            'type': getattr(exc_type, '__name__', None) if exc_type else None,
            'message': str(exc_value) if exc_value else None,
            'traceback': trace_text,
            'path': request.path,
            'method': request.method,
            'agent': request.headers.get('User-Agent','')[:380],
        }
    return render_template("errors/500.html"), 500

# ---------------- Error Reporting ----------------
def _save_error_screenshot(file_storage):
    if not file_storage or not file_storage.filename:
        return None
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in {'.png','.jpg','.jpeg'}:
        return None
    fname = f"error_{uuid4().hex}{ext}"
    upload_dir = os.path.join(app.root_path, 'static', 'uploads')
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, fname)
    file_storage.save(path)
    return f"uploads/{fname}"

@app.route('/errors/report', methods=['POST'])
@login_required
def error_report_create():
    title = (request.form.get('title') or 'Application Error').strip()
    comment = (request.form.get('comment') or '').strip() or None
    # Pull cached traceback info if user clicked from 500 page
    cached = session.pop('__last_error__', None)
    screenshot_rel = _save_error_screenshot(request.files.get('screenshot'))
    # Derive fingerprint for de-duplication (type + message + first line of traceback)
    import hashlib
    fp_source_parts = []
    if cached:
        if cached.get('type'): fp_source_parts.append(cached.get('type'))
        if cached.get('message'): fp_source_parts.append(cached.get('message'))
        if cached.get('traceback'):
            first_line = cached.get('traceback').splitlines()[0][:300]
            fp_source_parts.append(first_line)
    fp_source = '||'.join(fp_source_parts) if fp_source_parts else None
    fingerprint = hashlib.sha256(fp_source.encode('utf-8')).hexdigest() if fp_source else None
    # If fingerprint exists, check for existing recent open report (same fingerprint) to soft-dedupe
    existing = None
    if fingerprint:
        existing = (ErrorReport.query
                    .filter(ErrorReport.fingerprint==fingerprint)
                    .order_by(ErrorReport.created_at.desc())
                    .first())
    if existing and not existing.is_resolved():
        flash(f'A similar error (#{existing.id}) already exists and is {existing.status}. Added your comment.', 'warning')
        if comment:
            # Append comment (simple newline join)
            existing.reporter_comment = (existing.reporter_comment or '') + (('\n---\n'+comment) if existing.reporter_comment else comment)
            existing.updated_at = datetime.now(timezone.utc)
            db.session.commit()
        return redirect(url_for('error_report_detail', rid=existing.id))
    er = ErrorReport(
        title=title[:255] or 'Application Error',
        reporter_id=current_user.id,
        reporter_comment=comment,
        error_type=(cached or {}).get('type'),
        error_message=(cached or {}).get('message'),
        traceback=(cached or {}).get('traceback'),
        request_path=(cached or {}).get('path'),
        request_method=(cached or {}).get('method'),
        user_agent=(cached or {}).get('agent'),
        screenshot_path=screenshot_rel,
        fingerprint=fingerprint,
    )
    db.session.add(er)
    db.session.commit()
    flash('Error report submitted','success')
    return redirect(url_for('error_reports_index'))

@app.route('/error-reports')
@login_required
@permission_required('manage_issues', any=True)
def error_reports_index():
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 25, type=int), 100)
    q = ErrorReport.query.order_by(ErrorReport.created_at.desc())
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('errors/reports_index.html', reports=pagination.items, pagination=pagination)

@app.route('/error-reports/<int:rid>')
@login_required
@permission_required('manage_issues', any=True)
def error_report_detail(rid):
    rep = ErrorReport.query.get_or_404(rid)
    return render_template('errors/report_detail.html', report=rep)

@app.route('/error-reports/<int:rid>/status', methods=['POST'])
@login_required
@permission_required('manage_issues', any=True)
def error_report_status(rid):
    rep = ErrorReport.query.get_or_404(rid)
    new_status = (request.form.get('status') or '').strip() or rep.status
    rep.status = new_status
    if rep.is_resolved() and not rep.resolved_at:
        rep.resolved_at = datetime.now(timezone.utc)
        rep.resolved_by_id = current_user.id
        # Notify reporter via email if available
        try:
            if rep.reporter and rep.reporter.email:
                subj = f"Error Report #{rep.id} Resolved"
                html = f"<p>Your reported issue '<strong>{rep.title}</strong>' has been marked as resolved.</p>"
                send_email(rep.reporter.email, subj, html)
        except Exception as _exc:  # pragma: no cover
            print(f"[WARN] Error notification failed: {_exc}")
    db.session.commit()
    flash('Status updated','success')
    return redirect(url_for('error_report_detail', rid=rep.id))

if __name__ == "__main__":
    # Centralised static configuration (edit config.py to change)
    try:
        from config import RUN_DEBUG, RUN_HOST, RUN_PORT
    except Exception:
        # Fallbacks if config.py missing or incomplete
        RUN_HOST, RUN_PORT, RUN_DEBUG = "127.0.0.1", 5000, False
    import os
    import socket

    # Allow environment override
    env_port = os.getenv('PORT') or os.getenv('APP_PORT')
    if env_port and env_port.isdigit():
        RUN_PORT = int(env_port)
    # If port in use, try next 10 ports automatically to ease dev collisions
    base_port = RUN_PORT
    for i in range(0, 10):
        test_port = base_port + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex((RUN_HOST, test_port)) != 0:  # free
                RUN_PORT = test_port
                break
    if RUN_PORT != base_port:
        print(f"[INFO] Selected available port {RUN_PORT} (requested {base_port} was busy)")
    # Lightweight favicon route to suppress error spam if not served
    @app.route('/favicon.ico')
    def _favicon():
        from flask import abort, send_from_directory
        try:
            return send_from_directory(os.path.join(app.root_path, 'static', 'img'), 'excel tutors logo 2023.png')
        except Exception:
            # Return 204 No Content to silence browsers
            from flask import Response
            return Response(status=204)
    app.run(host=RUN_HOST, port=RUN_PORT, debug=RUN_DEBUG)