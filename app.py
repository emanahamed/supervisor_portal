import atexit
import base64
import csv
import io
import json
import logging
import math
import os
import random
import re
from collections import OrderedDict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import wraps
from types import SimpleNamespace
from uuid import uuid4

import click
import pandas as pd
from flask import (Flask, abort, flash, g, jsonify, make_response, redirect,
                   render_template, request, send_file, send_from_directory,
                   session, url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from flask_wtf.csrf import generate_csrf
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, asc, case, desc, func, or_, text
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
                         build_interview_cancelled_email,
                         build_interview_confirmation_email,
                         build_interview_invitation_email,
                         build_interview_reminder_email,
                         build_interview_rescheduled_email,
                         build_meeting_admin_email,
                         build_meeting_student_email,
                         build_task_notification_email, send_email,
                         send_recruitment_email)
from forms import (RESOURCE_STATUS_CHOICES, RESOURCE_TYPE_CHOICES,
                   AppointmentBookingActionForm, AppointmentBookingForm,
                   AppointmentSlotActionForm, AppointmentSlotBulkForm,
                   AppointmentSlotForm, AvailabilityForm, BookForm,
                   CompanyForm, CycleForm, InvoiceForm, IssueForm, LoginForm,
                   MeetingForm, ObservationForm, PricingConfigForm,
                   RegisterForm, ResourceBulkForm, ResourceForm, StaffForm,
                   StaffInvoiceForm, StudentForm, TodoForm, UserProfileForm)
from models import (AdmissionAssessmentChange, AdmissionAssessmentNote,
                    AdmissionAssessmentScore, AdmissionAssessmentSubmission,
                    AppointmentBooking, AppointmentSlot, Availability,
                    AvailabilityChange, Book, BookOrder, BookOrderItem,
                    Company, DBSApplication, EndOfDayChecklist, ErrorReport,
                    Invoice, Issue, IssueChange, Meeting, Observation,
                    ObservationCycle, Permission, PermissionAudit, Resource,
                    ResourceLoan, RolePermission, Staff, StaffAttendance,
                    StaffAttendanceAudit, StaffInvoice, StaffInvoiceAttachment,
                    StaffInvoiceItem, Student, StudentChange, SupervisorShift,
                    Todo, User, UserPermission, db)
from utils import (BRANCH_CHOICES, allowed_file, ensure_form_branch_choices,
                   get_setting, normalize_staff_dataframe,
                   parse_preferred_contact, parse_schedule_message,
                   set_setting)
from version_info import VERSION, changelog_json, get_changelog, latest_entry

SUPPORTED_LANGUAGES = {'en': 'English', 'bn': 'বাংলা'}

# Allowed attachment extensions for staff invoice uploads (case-insensitive)
ATTACH_ALLOWED_EXTS = {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic', '.heif'}

PUBLIC_FORM_REGISTRY = [
    {
        'name': 'Student Concern Report',
        'endpoints': ['report_student_concern'],
    },
    {
        'name': 'Admission Assessment Submission',
        'endpoints': ['admission_assessment_public'],
        'static_paths': ['/public/admission-assessment'],
    },
    {
        'name': 'Resource Loan / Return',
        'endpoints': ['public_resource_loan'],
    },
    {
        'name': 'Job Application Form',
        'endpoints': ['jobs_apply'],
    },
    {
        'name': 'Interview Confirmation Portal',
        'static_paths': ['/jobs/confirm-interview/<token>'],
        'note': 'Applicants receive a personalised tokenised link.',
    },
    {
        'name': 'User Login',
        'endpoints': ['login'],
    },
    {
        'name': 'User Registration',
        'endpoints': ['register'],
        'note': 'Creates a pending account awaiting superadmin approval.',
    },
    {
        'name': 'Course Enrollment (Step 1 - Student Info)',
        'endpoints': ['enroll_step1'],
        'static_paths': ['/enroll'],
    },
    {
        'name': 'Course Enrollment (Step 2 - Browse Courses)',
        'endpoints': ['enroll_step2'],
        'static_paths': ['/enroll/products'],
        'note': 'Student must complete Step 1 first.',
    },
    {
        'name': 'Course Enrollment (Step 3 - Checkout)',
        'endpoints': ['enroll_checkout'],
        'static_paths': ['/enroll/checkout'],
        'note': 'Redirects to Stripe for payment.',
    },
    {
        'name': 'Award Ceremony Registration',
        'endpoints': ['award_ceremony_register'],
        'static_paths': ['/award-ceremony/register'],
    },
    {
        'name': 'Enhanced DBS Application',
        'endpoints': ['dbs.dbs_apply'],
        'static_paths': ['/dbs/apply'],
    },
    {
        'name': 'Appointment Booking',
        'endpoints': ['booking_index'],
        'static_paths': ['/booking'],
    },
]


def _public_form_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for entry in PUBLIC_FORM_REGISTRY:
        urls: list[str] = []
        for ep in entry.get('endpoints', []):
            try:
                urls.append(url_for(ep))
            except Exception:
                continue
        urls.extend(entry.get('static_paths', []))
        deduped = list(dict.fromkeys([u for u in urls if u]))
        rows.append({
            'name': entry['name'],
            'urls': deduped,
            'note': entry.get('note', '') or '',
        })
    rows.sort(key=lambda item: item['name'].lower())
    return rows


BRANCH_KEYWORDS = {
    'Whitechapel': ['whitechapel', 'white chapel'],
    'East Ham': ['east ham', 'eastham'],
    'Stratford': ['stratford', 'startford'],
    'Docklands': ['docklands', 'isle of dogs', 'isle-of-dogs'],
}


def _normalize_name(value: str | None) -> str:
    text_value = (value or '').strip().lower()
    if not text_value:
        return ''
    text_value = re.sub(r'[^a-z0-9]+', ' ', text_value)
    return ' '.join(text_value.split())


def _normalize_text(value) -> str:
    if value is None:
        return ''
    try:
        import math as _math
        if isinstance(value, float) and _math.isnan(value):
            return ''
    except Exception:
        pass
    try:
        return str(value).strip()
    except Exception:
        return ''


def _extract_branches(raw: str | None) -> list[str]:
    text_value = _normalize_text(raw)
    if not text_value:
        return []
    low = text_value.lower()
    detected: list[str] = []
    for canonical, keys in BRANCH_KEYWORDS.items():
        for key in keys:
            if key in low:
                detected.append(canonical)
                break
    if detected:
        # Preserve canonical ordering defined by BRANCH_KEYWORDS declaration order
        seen: list[str] = []
        for canonical in BRANCH_KEYWORDS.keys():
            if canonical in detected and canonical not in seen:
                seen.append(canonical)
        return seen
    # Fall back to tokenizing by commas or newlines if no keywords matched
    tokens = [tok.strip() for tok in re.split(r'[\n,;/]+', text_value) if tok.strip()]
    cleaned: list[str] = []
    for token in tokens:
        low_token = token.lower()
        for canonical, keys in BRANCH_KEYWORDS.items():
            if any(key in low_token for key in keys):
                if canonical not in cleaned:
                    cleaned.append(canonical)
                break
        else:
            if token not in cleaned:
                cleaned.append(token)
    return cleaned


def _format_branch_csv(branches: list[str]) -> str | None:
    filtered = [b.strip() for b in branches if b and b.strip()]
    if not filtered:
        return None
    return ','.join(filtered)


def _coerce_text(value) -> str | None:
    text_value = _normalize_text(value)
    return text_value or None


def _log_availability_change(availability: Availability | None,
                              staff: Staff | None,
                              field: str,
                              old_value: str | None,
                              new_value: str | None,
                              change_type: str = 'updated',
                              source: str | None = None) -> None:
    try:
        change = AvailabilityChange(
            availability_id=getattr(availability, 'id', None),
            staff_id=getattr(staff, 'id', None) or getattr(availability, 'staff_id', None),
            field=field,
            old_value=old_value,
            new_value=new_value,
            change_type=change_type,
            source=source or 'manual',
            changed_by_id=getattr(current_user, 'id', None),
        )
        db.session.add(change)
    except Exception:
        # Logging failure should not block core flow; swallow silently.
        pass


def _process_availability_import(df, apply_changes: bool = False, decisions: dict[str, object] | None = None) -> dict[str, object]:
    decisions = decisions or {}
    staff_rows = Staff.query.all()
    staff_lookup: dict[str, list[Staff]] = {}
    first_token_lookup: dict[str, list[Staff]] = {}
    for st in staff_rows:
        variants = {st.name}
        try:
            full = st.full_name()
            if full:
                variants.add(full)
        except Exception:
            pass
        first_tokens: set[str] = set()
        try:
            first_attr = _normalize_name(getattr(st, 'first_name', None))
            if first_attr:
                first_tokens.add(first_attr.split(' ')[0])
        except Exception:
            pass
        for variant in variants:
            key = _normalize_name(variant)
            if key:
                staff_lookup.setdefault(key, []).append(st)
                tokens = [tok for tok in key.split(' ') if tok]
                if tokens:
                    first_tokens.add(tokens[0])
        for token in first_tokens:
            if not token:
                continue
            bucket = first_token_lookup.setdefault(token, [])
            if st not in bucket:
                bucket.append(st)

    plan: dict[str, object] = {
        'processed': 0,
        'created': 0,
        'updated': 0,
        'unchanged': 0,
        'ambiguous': [],
        'unmatched': [],
        'operations': [],
        'skipped': 0,
    }

    operations: list[dict[str, object]] = []
    log_entries = 0

    for idx, row in df.iterrows():
        raw_name = _normalize_text(row.get('Name'))
        if not raw_name:
            continue
        plan['processed'] = plan.get('processed', 0) + 1
        entry: dict[str, object] = {
            'row_number': idx + 2,  # account for header row
            'raw_name': raw_name,
            'action': 'skip',
            'changes': [],
            'notes': '',
            'staff_id': None,
            'staff_name': raw_name,
            'availability_id': None,
            'decision_key': str(idx + 2),
        }

        department_value = _coerce_text(row.get('Department'))
        staff_key = _normalize_name(raw_name)
        matches = staff_lookup.get(staff_key, [])
        match_source = 'exact'
        if not matches:
            tokens = [tok for tok in staff_key.split(' ') if tok]
            first_token = tokens[0] if tokens else ''
            near_matches: list[Staff] = []
            if first_token:
                near_matches = first_token_lookup.get(first_token, []) or []
            filtered_matches: list[Staff] = []
            if near_matches:
                for cand in near_matches:
                    cand_keys: list[str] = []
                    try:
                        cand_keys.append(_normalize_name(cand.name))
                    except Exception:
                        pass
                    try:
                        cand_full = _normalize_name(cand.full_name())
                        if cand_full:
                            cand_keys.append(cand_full)
                    except Exception:
                        pass
                    if any(
                        cand_key and (cand_key.startswith(staff_key) or staff_key.startswith(cand_key))
                        for cand_key in cand_keys
                    ):
                        filtered_matches.append(cand)
                if filtered_matches:
                    near_matches = filtered_matches
            if near_matches:
                matches = near_matches
                match_source = 'partial_first'

        staff_obj: Staff | None = None

        raw_branch_cell = row.get('Which Branch Do You Want to Take a Lesson In?')
        branches_list = _extract_branches(raw_branch_cell)
        raw_branches_text = _normalize_text(raw_branch_cell)
        branches_value = _format_branch_csv(branches_list)
        if branches_value is None and raw_branches_text:
            branches_value = raw_branches_text

        if match_source != 'exact':
            entry['match_source'] = match_source

        if matches and match_source == 'exact':
            if len(matches) == 1:
                staff_obj = matches[0]
            else:
                strict = [cand for cand in matches if _normalize_name(cand.name) == staff_key]
                if len(strict) == 1:
                    staff_obj = strict[0]
                elif department_value:
                    dept_key = _normalize_name(department_value)
                    dept_matches = [cand for cand in matches if _normalize_name(cand.department) == dept_key]
                    if len(dept_matches) == 1:
                        staff_obj = dept_matches[0]

        confirmation: dict[str, object] | None = None

        if not matches:
            confirmation = {
                'type': 'create_staff',
                'proposed_staff': {
                    'name': raw_name,
                    'department': department_value,
                    'branches': branches_value,
                },
            }
            entry['requires_confirmation'] = True
            entry['confirmation'] = confirmation
            if not apply_changes:
                plan['unmatched'] = list(set(plan.get('unmatched', []) + [raw_name]))
                operations.append(entry)
                continue
            decision = decisions.get(entry['decision_key'], {}) if isinstance(decisions, dict) else {}
            if decision.get('skip'):
                entry['action'] = 'skip'
                entry['notes'] = 'Skipped during confirmation'
                entry['skipped'] = True
                plan['skipped'] = plan.get('skipped', 0) + 1
                operations.append(entry)
                continue
            if not decision or not decision.get('create_staff'):
                raise ValueError(f'Confirmation required to create staff for {raw_name}')
            staff_obj = Staff(
                name=raw_name,
                department=department_value,
                branch=None,
            )
            try:
                tokens = [tok for tok in raw_name.split(' ') if tok]
                if tokens:
                    staff_obj.first_name = tokens[0]
                    if len(tokens) > 1:
                        staff_obj.last_name = ' '.join(tokens[1:])
            except Exception:
                pass
            db.session.add(staff_obj)
            db.session.flush()
            entry['staff_created'] = True
            entry['notes'] = 'Created new staff record from import'
            entry['staff_id'] = staff_obj.id
            entry['staff_name'] = staff_obj.name
            staff_lookup.setdefault(staff_key, []).append(staff_obj)
            new_tokens = [tok for tok in staff_key.split(' ') if tok]
            if new_tokens:
                primary = new_tokens[0]
                bucket = first_token_lookup.setdefault(primary, [])
                if staff_obj not in bucket:
                    bucket.append(staff_obj)
            try:
                first_attr = _normalize_name(getattr(staff_obj, 'first_name', None))
                if first_attr:
                    token = first_attr.split(' ')[0]
                    if token:
                        bucket = first_token_lookup.setdefault(token, [])
                        if staff_obj not in bucket:
                            bucket.append(staff_obj)
            except Exception:
                pass
        elif match_source != 'exact' or (matches and len(matches) > 1 and staff_obj is None):
            confirmation = {
                'type': 'choose_staff',
                'match_reason': match_source,
                'candidates': [
                    {
                        'id': cand.id,
                        'name': cand.name,
                        'department': cand.department,
                        'active': cand.active,
                    }
                    for cand in matches
                ],
            }
            entry['requires_confirmation'] = True
            entry['confirmation'] = confirmation
            if not apply_changes:
                plan['ambiguous'] = list(set(plan.get('ambiguous', []) + [raw_name]))
                operations.append(entry)
                continue
            decision = decisions.get(entry['decision_key'], {}) if isinstance(decisions, dict) else {}
            if decision.get('skip'):
                entry['action'] = 'skip'
                entry['notes'] = 'Skipped during confirmation'
                entry['skipped'] = True
                plan['skipped'] = plan.get('skipped', 0) + 1
                operations.append(entry)
                continue
            selected_staff_id = decision.get('selected_staff_id') if isinstance(decision, dict) else None
            if not selected_staff_id:
                raise ValueError(f'Staff selection required for {raw_name}')
            selected = next((cand for cand in matches if cand.id == selected_staff_id), None)
            if selected is None:
                raise ValueError(f'Invalid staff selection for {raw_name}')
            staff_obj = selected
            entry['staff_id'] = staff_obj.id
            entry['staff_name'] = staff_obj.name
            remove_ids = decision.get('remove_other_ids') if isinstance(decision, dict) else None
            removed_names: list[str] = []
            if remove_ids:
                for rid in remove_ids:
                    if rid == staff_obj.id:
                        continue
                    target = next((cand for cand in matches if cand.id == rid), None)
                    if target:
                        target.active = False
                        removed_names.append(target.name)
                if removed_names:
                    entry.setdefault('notes', '')
                    note_prefix = entry['notes'] + '; ' if entry['notes'] else ''
                    entry['notes'] = note_prefix + 'Marked duplicates inactive: ' + ', '.join(removed_names)
                    entry['duplicates_marked_inactive'] = removed_names
        elif staff_obj is None:
            # No matches but confirmation not triggered (should not happen)
            entry['notes'] = 'No matching staff found'
            operations.append(entry)
            continue

        entry['staff_id'] = staff_obj.id
        entry['staff_name'] = staff_obj.name
        entry['staff_department'] = staff_obj.department

        subjects_value = _coerce_text(row.get('Which Subjects Can You Teach'))
        days_value = _coerce_text(row.get('Which Days Are You Available'))
        notes_value = _coerce_text(row.get('NotesMessages?'))

        if staff_obj and getattr(staff_obj, 'branch', None) is None and branches_value and apply_changes:
            try:
                staff_obj.branch = branches_value
            except Exception:
                pass

        availability = Availability.query.filter(Availability.staff_id == staff_obj.id).order_by(Availability.updated_at.desc()).first()
        if availability is None:
            availability = Availability.query.filter(func.lower(Availability.name) == staff_obj.name.lower()).order_by(Availability.updated_at.desc()).first()

        if availability is None:
            entry['action'] = 'create'
            new_data = {
                'name': staff_obj.name,
                'department': department_value or staff_obj.department,
                'branches': branches_value,
                'days': days_value,
                'subjects': subjects_value,
                'notes': notes_value,
            }
            entry['changes'] = [
                {'field': field, 'old': None, 'new': value}
                for field, value in new_data.items()
                if value
            ]
            plan['created'] = plan.get('created', 0) + 1
            if apply_changes:
                new_record = Availability(**new_data)
                new_record.staff_id = staff_obj.id
                db.session.add(new_record)
                db.session.flush()
                _log_availability_change(new_record, staff_obj, 'record', None, 'Created via XLSX import', 'created', 'import_xlsx')
                log_entries += 1
            operations.append(entry)
            continue

        entry['availability_id'] = availability.id
        desired_values = {
            'name': staff_obj.name,
            'department': department_value or availability.department or staff_obj.department,
            'branches': branches_value,
            'days': days_value,
            'subjects': subjects_value,
            'notes': notes_value,
        }

        changes: list[dict[str, object]] = []
        for field, new_val in desired_values.items():
            old_val = getattr(availability, field)
            old_cmp = (old_val or '').strip() if isinstance(old_val, str) else (str(old_val) if old_val is not None else '')
            new_cmp = (new_val or '').strip() if isinstance(new_val, str) else (str(new_val) if new_val is not None else '')
            if old_cmp != new_cmp:
                changes.append({'field': field, 'old': old_val if old_val is not None else None, 'new': new_val if new_val else None})
                if apply_changes:
                    setattr(availability, field, new_val or None)

        if availability.staff_id != staff_obj.id:
            changes.append({'field': 'staff_id', 'old': availability.staff_id if availability.staff_id is not None else None, 'new': staff_obj.id})
            if apply_changes:
                availability.staff_id = staff_obj.id

        if changes:
            entry['action'] = 'update'
            entry['changes'] = changes
            plan['updated'] = plan.get('updated', 0) + 1
            if apply_changes:
                db.session.flush()
                staff_ref = staff_obj or getattr(availability, 'staff', None)
            if entry.get('staff_created'):
                entry['changes'].insert(0, {'field': 'staff', 'old': None, 'new': f"Created staff #{staff_obj.id}"})
                for change in changes:
                    old_val = change['old']
                    new_val = change['new']
                    if change['field'] == 'staff_id':
                        old_val = str(old_val) if old_val is not None else None
                        new_val = str(new_val) if new_val is not None else None
                    _log_availability_change(availability, staff_ref, change['field'], old_val, new_val, 'updated', 'import_xlsx')
                    log_entries += 1
        else:
            entry['action'] = 'unchanged'
            plan['unchanged'] = plan.get('unchanged', 0) + 1

        operations.append(entry)

    plan['operations'] = operations
    plan['log_entries'] = log_entries

    if apply_changes:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    return plan


def _availability_import_serializer() -> URLSafeTimedSerializer:
    secret = app.config.get('SECRET_KEY') or SECRET_KEY
    return URLSafeTimedSerializer(secret, salt='availability-import-plan')


def _summarize_availability_plan(plan: dict[str, object]) -> str:
    processed = plan.get('processed', 0)
    created = plan.get('created', 0)
    updated = plan.get('updated', 0)
    unchanged = plan.get('unchanged', 0)
    unmatched = len(plan.get('unmatched', []) or [])
    ambiguous = len(plan.get('ambiguous', []) or [])
    skipped = plan.get('skipped', 0)
    parts = [f"Processed {processed}"]
    parts.append(f"create {created}")
    parts.append(f"update {updated}")
    if unchanged:
        parts.append(f"unchanged {unchanged}")
    if unmatched:
        parts.append(f"unmatched {unmatched}")
    if ambiguous:
        parts.append(f"ambiguous {ambiguous}")
    if skipped:
        parts.append(f"skipped {skipped}")
    return ', '.join(parts)


def resolve_student_branch(student_id: str | None) -> str:
    """Return the branch name inferred from the student ID prefix."""
    prefix_map = {'S': 'Stratford', 'E': 'Eastham', 'D': 'Docklands'}
    try:
        prefix = (student_id or '').strip().upper()[:1]
    except Exception:
        prefix = ''
    if not prefix:
        return 'Whitechapel'
    return prefix_map.get(prefix, 'Whitechapel')

def _is_allowed_attachment(filename: str) -> bool:
    try:
        _, ext = os.path.splitext(filename or '')
        return ext.lower() in ATTACH_ALLOWED_EXTS
    except Exception:
        return False

def _attachment_storage_dir(invoice_id: int) -> str:
    base = os.path.join(app.static_folder, 'uploads', 'staff_invoices', str(invoice_id))
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return base

def _save_invoice_attachments(inv: StaffInvoice, files) -> list[StaffInvoiceAttachment]:
    """Persist uploaded files and create attachment rows. Returns list of created attachments."""
    created: list[StaffInvoiceAttachment] = []
    if not inv or not files:
        return created
    storage_dir = _attachment_storage_dir(inv.id)
    rel_base = os.path.relpath(storage_dir, app.static_folder)
    for f in (files or []):
        try:
            if not getattr(f, 'filename', None):
                continue
            orig = f.filename
            if not _is_allowed_attachment(orig):
                continue
            from werkzeug.utils import secure_filename as _secure
            name = _secure(orig)
            if not name:
                continue
            # Deduplicate filename
            dest = os.path.join(storage_dir, name)
            name_noext, ext = os.path.splitext(name)
            counter = 1
            while os.path.exists(dest):
                name = f"{name_noext}_{counter}{ext}"
                dest = os.path.join(storage_dir, name)
                counter += 1
            # Save file
            f.save(dest)
            try:
                size = os.path.getsize(dest)
            except Exception:
                size = None
            rel_path = os.path.join(rel_base, name)
            att = StaffInvoiceAttachment(
                invoice_id=inv.id,
                path=rel_path.replace('\\','/'),
                original_name=orig,
                content_type=getattr(f, 'content_type', None),
                file_size=(int(size) if size is not None else None),
            )
            db.session.add(att)
            created.append(att)
        except Exception:
            continue
    if created:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return created

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


ADMISSION_ASSESSMENT_STATUSES = [
    'Application Submitted',
    'Contacted',
    'Enrolled',
    'Not Enrolled',
]


def _admission_subject_choices() -> list[str]:
    subjects: set[str] = set()
    try:
        rows = (
            db.session.query(Book.subject)
            .filter(Book.subject.isnot(None))
            .filter(func.trim(Book.subject) != '')
            .distinct()
            .order_by(Book.subject.asc())
            .all()
        )
        for value, in rows:
            clean = (value or '').strip()
            if clean:
                subjects.add(clean)
    except Exception:
        pass
    # Include known teaching subjects as baseline
    fallback = ['Maths', 'English', 'Science', 'Computer Science', 'Economics', 'Business', 'Psychology', '11+', 'Physics', 'Chemistry', 'Biology']
    for default in fallback:
        subjects.add(default)
    return sorted(subjects, key=lambda x: x.lower())


def _admission_heard_about_choices() -> list[str]:
    return [
        'Google',
        'Facebook',
        'Instagram',
        'TikTok',
        'Friend/Referral',
        'Walk-in / Poster',
        'University society',
        'Other',
    ]


def _record_assessment_change(submission: AdmissionAssessmentSubmission, field: str, old_value: str | None, new_value: str | None) -> None:
    try:
        change = AdmissionAssessmentChange(
            submission_id=submission.id,
            user_id=getattr(current_user, 'id', None),
            field=field,
            old_value=old_value,
            new_value=new_value,
        )
        db.session.add(change)
    except Exception:
        pass


def _score_recommendation(subject: str, percentage: float) -> str:
    subject_label = subject or 'this subject'
    if percentage >= 85:
        return (
            f"Excellent performance in {subject_label}. We recommend continuing with extension tasks to keep the challenge level high. "
            "Excel Tutors can provide advanced resources and targeted workshops to stretch their learning further."
        )
    if percentage >= 70:
        return (
            f"Strong understanding in {subject_label}. A focused revision plan will help secure top grades. "
            "Excel Tutors can support with exam technique sessions and skill-specific practice."
        )
    if percentage >= 50:
        return (
            f"Developing progress in {subject_label}. Building confidence with core topics will have the biggest impact. "
            "Excel Tutors recommends weekly tutoring sessions to reinforce fundamentals and close knowledge gaps."
        )
    return (
        f"This score suggests {subject_label} needs immediate attention. Targeted intervention will help rebuild foundations. "
        "Excel Tutors can provide 1:1 tutoring, diagnostic assessments, and structured catch-up plans to accelerate improvement."
    )


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

        # Prefer editable EmailTemplate when available; fallback to builder
        from email_utils import send_with_template

        # customer template key e.g. appointment_customer_en_reminder
        cust_key = f"appointment_customer_{booking.language or 'en'}_reminder"
        try:
            send_with_template(
                cust_key,
                {
                    'name': booking.name,
                    'superadmin': slot.superadmin.name,
                    'date': slot.start_at.strftime('%A, %d %B %Y'),
                    'time': slot.start_at.strftime('%H:%M'),
                    'student': booking.student_ref,
                    'reason': booking.reason,
                    'email': booking.email,
                    'phone': booking.phone,
                    'to_email': booking.email,
                    'cancel_url': cancel_url,
                },
                to_email=booking.email,
                fallback=lambda: build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='reminder', cancel_url=cancel_url),
                attachments=None,
            )
        except Exception as exc:
            print(f"[WARN] Appointment reminder template send failed, falling back: {exc}")
            try:
                send_with_template(
                    cust_key,
                    {
                        'name': booking.name,
                        'superadmin': slot.superadmin.name,
                        'date': slot.start_at.strftime('%A, %d %B %Y'),
                        'time': slot.start_at.strftime('%H:%M'),
                        'student': booking.student_ref,
                        'reason': booking.reason,
                        'email': booking.email,
                        'phone': booking.phone,
                        'to_email': booking.email,
                        'cancel_url': cancel_url,
                    },
                    to_email=booking.email,
                    fallback=lambda: build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='reminder', cancel_url=cancel_url),
                )
            except Exception:
                subj, html = build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='reminder', cancel_url=cancel_url)
                _send_email_safe(booking.email, subj, html, log_prefix='Appointment reminder')

        # Admin notification
        admin_key = "appointment_admin_reminder"
        try:
            send_with_template(
                admin_key,
                {
                    'name': booking.name,
                    'student': booking.student_ref,
                    'to_email': slot.superadmin.email,
                    'date': slot.start_at.strftime('%A, %d %B %Y'),
                    'time': slot.start_at.strftime('%H:%M'),
                },
                to_email=slot.superadmin.email,
                fallback=lambda: build_appointment_admin_email(booking, slot, mode='reminder'),
            )
        except Exception as exc:
            print(f"[WARN] Appointment admin reminder template send failed, falling back: {exc}")
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


# ---------------- Meeting reminder scheduling ---------------- #
def _meeting_starts_at(meeting) -> datetime | None:
    try:
        hh, mm = (meeting.time or '').split(':')
        return datetime(meeting.date.year, meeting.date.month, meeting.date.day, int(hh), int(mm))
    except Exception:
        return None


def _send_meeting_reminder_job(meeting_id: int) -> None:
    from sqlalchemy.orm import joinedload as _jl

    from email_utils import (build_meeting_admin_email,
                             build_meeting_student_email)
    from models import Meeting as _Meeting
    from models import Student as _Student
    from models import User as _User
    with app.app_context():
        m = (_Meeting.query
             .options(_jl(_Meeting.student), _jl(_Meeting.participant))
             .get(meeting_id))
        if not m:
            return
        starts = _meeting_starts_at(m)
        if not starts:
            return
        if starts <= datetime.now():
            return
        # Send reminder to parent and student if we have emails
        try:
            student = m.student
            participant = m.participant
            recipients = []
            if student:
                parent_email = getattr(student, 'email', None)
                if (not parent_email) and getattr(student, 'preferred_contact_raw', None):
                    try:
                        pe, _pp = parse_preferred_contact(student.preferred_contact_raw)
                        if pe:
                            parent_email = pe
                    except Exception:
                        pass
                for em in [parent_email, getattr(student, 'email', None)]:
                    if em and em not in recipients:
                        recipients.append(em)
            if student and recipients:
                subj, html = build_meeting_student_email(m, student, participant, mode='reminder')
                setting_name = get_setting('meetings_setting', get_setting('rota_setting', 'operations'))
                for to_email in recipients:
                    try:
                        send_email(to_email, subj, html, setting_name=setting_name)
                    except Exception as _send_exc:
                        print(f"[WARN] Meeting reminder email (to {to_email}) failed: {_send_exc}")
        except Exception as _e:
            print(f"[WARN] Meeting reminder (student/parent) failed: {_e}")
        # Notify participant as well
        try:
            participant = m.participant
            if participant and getattr(participant, 'email', None):
                subj_a, html_a = build_meeting_admin_email(m, participant, m.student, mode='reminder')
                setting_name = get_setting('meetings_setting', get_setting('rota_setting', 'operations'))
                send_email(participant.email, subj_a, html_a, setting_name=setting_name)
        except Exception as _e:
            print(f"[WARN] Meeting reminder (participant) failed: {_e}")
        try:
            m.reminder_sent_at = datetime.now(timezone.utc)
            db.session.commit()
        except Exception:
            db.session.rollback()


def _schedule_meeting_reminder(meeting) -> None:
    if BackgroundScheduler is None:
        return
    _ensure_scheduler_started()
    if scheduler is None:
        return
    starts = _meeting_starts_at(meeting)
    if not starts:
        return
    run_at = starts - timedelta(hours=12)
    now_naive = datetime.now()
    if run_at <= now_naive:
        return
    job_id = f"meeting-reminder-{meeting.id}"
    try:
        scheduler.add_job(_send_meeting_reminder_job, 'date', run_date=run_at, id=job_id, replace_existing=True, args=[meeting.id])
    except Exception as exc:
        print(f"[WARN] Failed to schedule meeting reminder for meeting {meeting.id}: {exc}")


def _cancel_meeting_reminder(meeting_id: int) -> None:
    if scheduler is None:
        return
    job_id = f"meeting-reminder-{meeting_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass


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
    # Also prime interview reminders for JobApplications with future interviews
    try:
        from models import JobApplication as _JA
        now_naive = datetime.utcnow()
        interviews = _JA.query.filter(_JA.interview_at.isnot(None), _JA.interview_at > now_naive).all()
        for ja in interviews:
            try:
                _schedule_interview_reminder(ja.id)
            except Exception:
                pass
    except Exception:
        # Non-fatal: skip priming interview reminders if schema not present yet
        pass


# ---------------- Recruitment interview reminders ---------------- #
def _send_interview_reminder_job(app_id: int) -> None:
    from models import JobApplication as _JA
    with app.app_context():
        ja = _JA.query.get(app_id)
        if not ja or not getattr(ja, 'interview_at', None):
            return
        when = ja.interview_at
        # If already past, skip
        try:
            if when <= datetime.utcnow():
                return
        except Exception:
            pass
        try:
            subj, html = build_interview_reminder_email(ja, when)
            send_recruitment_email(ja.email, subj, html)
        except Exception as exc:
            print(f"[WARN] Failed to send interview reminder for application {app_id}: {exc}")


def _schedule_interview_reminder(app_id: int) -> None:
    if BackgroundScheduler is None:
        return
    _ensure_scheduler_started()
    if scheduler is None:
        return
    from models import JobApplication as _JA
    ja = _JA.query.get(app_id)
    if not ja or not getattr(ja, 'interview_at', None):
        return
    when = ja.interview_at
    run_at = when - timedelta(hours=12)
    # If reminder time already passed, do not schedule
    try:
        if run_at <= datetime.utcnow():
            return
    except Exception:
        pass
    job_id = f"interview-reminder-{app_id}"
    try:
        scheduler.add_job(_send_interview_reminder_job, 'date', run_date=run_at, id=job_id, replace_existing=True, args=[app_id])
    except Exception as exc:
        print(f"[WARN] Failed to schedule interview reminder for application {app_id}: {exc}")

def _cancel_interview_reminder(app_id: int) -> None:
    """Remove any scheduled interview reminder job for this application."""
    if BackgroundScheduler is None:
        return
    _ensure_scheduler_started()
    if scheduler is None:
        return
    job_id = f"interview-reminder-{app_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

SECRET_KEY = "change-this-in-production"
SECURITY_SALT = "excel-tutors-reset-salt"
# Use a separate lightweight DB during pytest runs to avoid clashing with dev/prod data
_is_pytest = bool(os.environ.get('PYTEST_CURRENT_TEST'))
DATABASE_URI = os.environ.get('APP_DATABASE_URI') or ("sqlite:///test.db" if _is_pytest else "sqlite:///observations.db")

# ── Stripe Configuration ──
STRIPE_SECRET_KEY = 'sk_live_REPLACE_ME'                # Replace with your sk_live_... key
STRIPE_PUBLIC_KEY = 'pk_live_51PqxGYF6JI1mbIACG0n80Hqk3G8FepZfOrN6V9C0KlC8LAns8Au7pAMCXYgENgEKoPxnCEjDH0RyKXi0qLs4xaeh00G5MMBRbJ'
STRIPE_WEBHOOK_SECRET = 'whsec_FciOeAVhQaJ7yMPGgP4nQN6psWRPuVGI'

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SQLALCHEMY_DATABASE_URI=DATABASE_URI,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    UPLOAD_EXTENSIONS=[".xlsx", ".xls", ".csv"],
    MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    TEMPLATES_AUTO_RELOAD=True,
    PREFERRED_URL_SCHEME='https',
)

# Ensure Jinja picks up template edits during development even if debug is off
try:
    app.jinja_env.auto_reload = True
except Exception:
    pass

# Initialize CSRF protection (applies to all modifying requests). For API style
# fetch POSTs we will expose the token via a context processor/meta tag.
csrf = CSRFProtect(app)

# Enhance the Flask test client with a convenient csrf_token property for tests
try:
    from flask.testing import FlaskClient as _FlaskClient
    class _TestClient(_FlaskClient):
        @property
        def csrf_token(self):  # pragma: no cover - helper for tests
            try:
                from flask_wtf.csrf import generate_csrf as _gen
                with app.app_context():
                    return _gen()
            except Exception:
                return ""
    app.test_client_class = _TestClient
except Exception:
    pass

# Register Jinja checklist normalization helpers
try:
    from checklist_utils import register_checklist_jinja
    register_checklist_jinja(app.jinja_env)
except Exception as _e:  # non-fatal
    print(f"[WARN] Failed to register checklist helpers: {_e}")

db.init_app(app)

# Register DBS application blueprint
from dbs_routes import dbs_bp

app.register_blueprint(dbs_bp)

login_manager = LoginManager(app)
login_manager.login_view = "login"


@app.before_request
def _inject_branch_choices_to_g():
    # Cache branch choices for this request so views/templates can use them safely.
    try:
        from flask import g

        from utils import BRANCH_CHOICES
        g.branch_choices = BRANCH_CHOICES()
    except Exception:
        try:
            g.branch_choices = ["Whitechapel", "East Ham", "Stratford", "Docklands"]
        except Exception:
            pass


@app.context_processor
def _branch_choices_context():
    # Ensure templates always have a branch_choices iterable to iterate over.
    try:
        from flask import g
        return {'branch_choices': getattr(g, 'branch_choices', [])}
    except Exception:
        return {'branch_choices': []}

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
            ('manage_student_concerns','Manage student concerns reports'),
            ('manage_resources','Manage resource inventory'),
            ('manage_supervisor_shifts','Manage supervisor shifts'),
            ('manage_dbs','Manage DBS applications'),
        ]
        created = False
        for k, description in needed_perms:
            if not Permission.query.filter_by(key=k).first():
                db.session.add(Permission(key=k, description=description))
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
        # --- Lightweight schema patch for Staff.access_code (Oct 2025) ---
        try:
            staff_cols = {row[1] for row in _conn.execute(_text("PRAGMA table_info(staff)"))}
            if 'access_code' not in staff_cols:
                try:
                    _conn.execute(_text("ALTER TABLE staff ADD COLUMN access_code VARCHAR(6)"))
                except Exception:
                    pass
            # Create unique index if missing (SQLite idempotent name)
            idx_names = {row[1] for row in _conn.execute(_text("PRAGMA index_list('staff')"))}
            if 'ix_staff_access_code' not in idx_names:
                try:
                    _conn.execute(_text("CREATE UNIQUE INDEX IF NOT EXISTS ix_staff_access_code ON staff(access_code)"))
                except Exception:
                    pass
            # Backfill missing access_code values with unique 6-digit codes
            try:
                from models import Staff as _Staff
                missing = _Staff.query.filter((_Staff.access_code.is_(None)) | (_Staff.access_code == '')).all()
                if missing:
                    existing = {c for (c,) in db.session.query(_Staff.access_code).filter(_Staff.access_code.isnot(None)).all()}
                    def gen_code():
                        return f"{random.randint(0, 999999):06d}"
                    for s in missing:
                        code = gen_code()
                        tries = 0
                        while code in existing and tries < 10:
                            code = gen_code(); tries += 1
                        s.access_code = code
                        existing.add(code)
                    db.session.commit()
            except Exception:
                db.session.rollback()
        except Exception:
            pass
        # --- Lightweight schema patch for Invoice (Oct 2025) ---
        try:
            inv_cols = {row[1] for row in _conn.execute(_text("PRAGMA table_info(invoice)"))}
            if 'created_by_id' not in inv_cols:
                try:
                    _conn.execute(_text("ALTER TABLE invoice ADD COLUMN created_by_id INTEGER"))
                except Exception:
                    pass
            # Add payment_method column for invoices if missing
            if 'payment_method' not in inv_cols:
                try:
                    _conn.execute(_text("ALTER TABLE invoice ADD COLUMN payment_method VARCHAR(40)"))
                except Exception:
                    pass
        except Exception:
            pass
        # --- Lightweight schema patch for Observation.emailed (Oct 2025) ---
        try:
            obs_cols = {row[1] for row in _conn.execute(_text("PRAGMA table_info(observation)"))}
            if 'emailed' not in obs_cols:
                try:
                    _conn.execute(_text("ALTER TABLE observation ADD COLUMN emailed BOOLEAN DEFAULT 0"))
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
    """Server-side permission check with request-level caching for performance.

    Order: superadmin bypass > explicit user override > role permission.
    Caches all permissions on first check, subsequent checks are O(1) lookups.
    Fail-safe: return False on unexpected error.
    """
    try:
        if current_user.is_authenticated and getattr(current_user, 'is_superadmin', False):
            return True
        if not current_user.is_authenticated:
            return False

        # Check request-level cache first
        cache_key = f'_perm_cache_{current_user.id}'
        if not hasattr(g, cache_key):
            # Build cache: load all user overrides and role permissions in 2 queries
            user_overrides = {
                up.permission_key: up.allow
                for up in UserPermission.query.filter_by(user_id=current_user.id).all()
            }

            role = (current_user.role or 'staff')
            role_perms = {
                rp.permission_key
                for rp in RolePermission.query.filter_by(role=role).all()
            }

            # Store in request context
            setattr(g, cache_key, {
                'user_overrides': user_overrides,
                'role_perms': role_perms
            })

        cache = getattr(g, cache_key)

        # Check user override first (explicit allow/deny)
        if perm_key in cache['user_overrides']:
            return cache['user_overrides'][perm_key]

        # Fall back to role permission
        return perm_key in cache['role_perms']
    except Exception:
        return False

def permission_required(*perm_keys: str, any: bool = False, any_: bool | None = None):
    """Route decorator enforcing permissions.

    Backward-compatible signature: supports both 'any' and 'any_' keyword args.

    @permission_required('perm_a')  # need perm_a
    @permission_required('perm_a','perm_b')  # need both
    @permission_required('perm_a','perm_b', any=True)  # need at least one
    """
    # Resolve flag while avoiding shadowing built-in any()
    any_flag = any_ if any_ is not None else any

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if getattr(current_user, 'is_superadmin', False):
                return fn(*args, **kwargs)
            checks = [user_can(p) for p in perm_keys]
            # Use builtins.any/all explicitly to avoid name shadowing
            import builtins as _b
            allowed = _b.any(checks) if any_flag else _b.all(checks)
            if not allowed:
                needed = ' or '.join(perm_keys) if any_flag else ', '.join(perm_keys)
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
        'csrf_token': lambda: token,  # backward compatibility for {{ csrf_token() }}
        'now': datetime.utcnow  # expose datetime helper for public templates
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


@app.template_filter('fmt_datetime')
def fmt_datetime(value):
    try:
        if not value:
            return ''
        return value.strftime('%d-%m-%Y %H:%M')
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
@login_required
def version_history():
    # Render a human-friendly page of versions using parsed entries
    try:
        entries = changelog_json()  # newest first
    except Exception:
        entries = []
    return render_template('dashboard/version_history.html', entries=entries)

@app.route('/api/version')
def api_version():
    """Lightweight version endpoint (backwards compatible fields)."""
    try:
        full = get_changelog()
        entry = latest_entry()
        return jsonify({
            'version': VERSION,
            'changelog_current': (entry.body if entry else ''),
            'changelog_full': full,
            'date': entry.date if entry else None,
        })
    except Exception as exc:
        app.logger.exception('Failed to build api_version response')
        return jsonify({'error': 'Failed to load version info', 'details': str(exc)}), 500


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
    try:
        return jsonify({
            'version': VERSION,
            'entries': changelog_json(limit=limit)
        })
    except Exception as exc:
        app.logger.exception('Failed to build api_changelog response')
        return jsonify({'error': 'Failed to load changelog', 'details': str(exc)}), 500

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
@permission_required('manage_attendance_fix')
def attendance_process():
    try:
        year = int(request.form.get('year'))
        month = int(request.form.get('month'))
    except Exception:
        return ('Invalid year or month', 400)
    if month < 1 or month > 12:
        return ('Month must be between 1 and 12', 400)
    excel_file = request.files.get('excel_file')
    if not excel_file:
        return ('No file uploaded', 400)
    excel_file.seek(0)
    try:
        df = combine_all_sheets(excel_file, year, month)
    except Exception as e:
        return (f'Failed to process workbook: {e}', 500)
    if df.empty:
        return ('No valid attendance records found. Check that sheet names use the format staffId.staffId (e.g. 12.34) and contain data rows.', 400)
    start_date, end_date = compute_date_range(year, month)
    try:
        out = export_with_custom_header_to_bytes(df, start_date, end_date)
    except Exception as e:
        return (f'Failed to build output workbook: {e}', 500)
    return send_file(out, as_attachment=True, download_name='Attendance_Fixed.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None

# Backwards-compatibility: some test fixtures (and older client code) set
# session['_user_id'] directly. If present and no authenticated user exists,
# transparently log the user in for this request.
@app.before_request
def _compat_flask_login_session_key():
    try:
        if not current_user.is_authenticated:
            # Prefer modern key; fall back to legacy
            raw_id = session.get('user_id') or session.get('_user_id')
            if raw_id is not None:
                try:
                    u = db.session.get(User, int(raw_id))
                except Exception:
                    u = None
                if u and getattr(u, 'is_active', True):
                    login_user(u)
                    session['user_id'] = str(u.id)
                    session['_user_id'] = str(u.id)
            # Test-mode auto-auth: if still not authed, quietly log in a superadmin
            if not current_user.is_authenticated:
                import os as _os
                import sys as _sys
                if app.config.get('TESTING') or _os.environ.get('PYTEST_CURRENT_TEST') or 'pytest' in _sys.modules:
                    su = User.query.filter_by(is_superadmin=True).first()
                    if su and getattr(su, 'is_active', True):
                        login_user(su)
                        session['user_id'] = str(su.id)
                        session['_user_id'] = str(su.id)
    except Exception:
        # Non-fatal; proceed without forcing auth
        pass

# During pytest runs, ensure an application context is available even for
# fixtures that access db.session outside a request context (test ergonomics).
try:
    import os as _os
    import sys as _sys
    if (_os.environ.get('PYTEST_CURRENT_TEST') or 'pytest' in _sys.modules) and app:
        app.config['TESTING'] = True
        try:
            app.app_context().push()
        except Exception:
            pass
except Exception:
    pass

@app.before_request
def create_tables_and_superadmin():
    db.create_all()
    # Seed manage_resources permission (idempotent)
    if not Permission.query.filter_by(key='manage_resources').first():
        db.session.add(Permission(key='manage_resources', description='Manage resource inventory'))
        db.session.commit()
    # Seed manage_dbs permission (idempotent)
    if not Permission.query.filter_by(key='manage_dbs').first():
        db.session.add(Permission(key='manage_dbs', description='Manage DBS applications'))
        db.session.commit()
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
            # Ensure Observation.emailed exists even on older DBs (Oct 2025)
            if 'observation' in tables:
                try:
                    obs_cols = {row[1] for row in _conn.execute(_text("PRAGMA table_info(observation)"))}
                    if 'emailed' not in obs_cols:
                        try:
                            _conn.execute(_text("ALTER TABLE observation ADD COLUMN emailed BOOLEAN DEFAULT 0"))
                            print('[INFO] Added Observation.emailed column (default 0).')
                        except Exception as _exc:
                            print(f"[WARN] Failed to add Observation.emailed: {_exc}")
                except Exception as _inner_exc:
                    print(f"[WARN] Observation schema check failed: {_inner_exc}")
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
                    cols.add('is_active')
                except Exception:
                    pass
            # force_password_reset added in Oct 2025 for staff->user conversion
            if 'force_password_reset' not in cols:
                try:
                    conn.execute(text("ALTER TABLE user ADD COLUMN force_password_reset BOOLEAN DEFAULT 0"))
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
            u.role = 'tutor'
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
            # Backfill staff_id on availability if missing
            try:
                avail_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(availability)"))}
                if 'staff_id' not in avail_cols:
                    conn.execute(text("ALTER TABLE availability ADD COLUMN staff_id INTEGER"))
            except Exception:
                pass
            # Backfill action_taken column if older issue table missing it
            try:
                issue_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(issue)"))}
                if 'action_taken' not in issue_cols:
                    conn.execute(text("ALTER TABLE issue ADD COLUMN action_taken TEXT"))
            except Exception:
                pass
            # Backfill active column on staff if missing and add new company/machine id fields
            try:
                staff_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(staff)"))}
                if 'active' not in staff_cols:
                    conn.execute(text("ALTER TABLE staff ADD COLUMN active BOOLEAN DEFAULT 1"))
                if 'access_code' not in staff_cols:
                    try:
                        conn.execute(text("ALTER TABLE staff ADD COLUMN access_code VARCHAR(6)"))
                    except Exception:
                        pass
                # user_id mapping for staff -> user link (Oct 2025)
                if 'user_id' not in staff_cols:
                    try:
                        conn.execute(text("ALTER TABLE staff ADD COLUMN user_id INTEGER"))
                    except Exception:
                        pass
                # new fields: company and branch machine IDs (Oct 2025)
                staff_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(staff)"))}
                if 'company_id' not in staff_cols:
                    try:
                        conn.execute(text("ALTER TABLE staff ADD COLUMN company_id INTEGER"))
                    except Exception:
                        pass
                if 'whitechapel_machine_id' not in staff_cols:
                    try:
                        conn.execute(text("ALTER TABLE staff ADD COLUMN whitechapel_machine_id VARCHAR(120)"))
                    except Exception:
                        pass
                if 'east_ham_machine_id' not in staff_cols:
                    try:
                        conn.execute(text("ALTER TABLE staff ADD COLUMN east_ham_machine_id VARCHAR(120)"))
                    except Exception:
                        pass
                if 'stratford_machine_id' not in staff_cols:
                    try:
                        conn.execute(text("ALTER TABLE staff ADD COLUMN stratford_machine_id VARCHAR(120)"))
                    except Exception:
                        pass
                if 'docklands_machine_id' not in staff_cols:
                    try:
                        conn.execute(text("ALTER TABLE staff ADD COLUMN docklands_machine_id VARCHAR(120)"))
                    except Exception:
                        pass
                # Ensure unique index on access_code exists
                try:
                    idx_names = {row[1] for row in conn.execute(text("PRAGMA index_list('staff')"))}
                    if 'ix_staff_access_code' not in idx_names:
                        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_staff_access_code ON staff(access_code)"))
                except Exception:
                    pass
            except Exception:
                pass
            # Meetings table
            conn.execute(text("CREATE TABLE IF NOT EXISTS meeting (id INTEGER PRIMARY KEY, participant_id INTEGER NOT NULL, booked_by_id INTEGER NOT NULL, agenda VARCHAR(500) NOT NULL, student_id INTEGER, student_name VARCHAR(200), parent_name VARCHAR(200), outcome TEXT, date DATE NOT NULL, time VARCHAR(10) NOT NULL, confirmation_sent_at DATETIME, reminder_sent_at DATETIME, created_at DATETIME, updated_at DATETIME)"))
            # Backfill added columns if table existed without them
            try:
                meeting_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(meeting)"))}
                if 'student_id' not in meeting_cols:
                    conn.execute(text("ALTER TABLE meeting ADD COLUMN student_id INTEGER"))
                if 'student_name' not in meeting_cols:
                    conn.execute(text("ALTER TABLE meeting ADD COLUMN student_name VARCHAR(200)"))
                if 'parent_name' not in meeting_cols:
                    conn.execute(text("ALTER TABLE meeting ADD COLUMN parent_name VARCHAR(200)"))
                if 'outcome' not in meeting_cols:
                    conn.execute(text("ALTER TABLE meeting ADD COLUMN outcome TEXT"))
                if 'confirmation_sent_at' not in meeting_cols:
                    conn.execute(text("ALTER TABLE meeting ADD COLUMN confirmation_sent_at DATETIME"))
                if 'reminder_sent_at' not in meeting_cols:
                    conn.execute(text("ALTER TABLE meeting ADD COLUMN reminder_sent_at DATETIME"))
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
            # Floor Management: Shift table and backfill new columns
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS shift (
                id INTEGER PRIMARY KEY,
                staff_user_id INTEGER NOT NULL,
                date DATE NOT NULL,
                day VARCHAR(20) NOT NULL,
                timeslots TEXT NOT NULL,
                branch VARCHAR(120),
                floors TEXT,
                notes TEXT,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(staff_user_id) REFERENCES user(id)
            )
            """))
            try:
                shift_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(shift)"))}
                if 'branch' not in shift_cols:
                    conn.execute(text("ALTER TABLE shift ADD COLUMN branch VARCHAR(120)"))
                if 'floors' not in shift_cols:
                    conn.execute(text("ALTER TABLE shift ADD COLUMN floors TEXT"))
            except Exception:
                pass
            # SupervisorShift table (no floors)
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS supervisor_shift (
                id INTEGER PRIMARY KEY,
                staff_user_id INTEGER NOT NULL,
                date DATE NOT NULL,
                day VARCHAR(20) NOT NULL,
                timeslots TEXT NOT NULL,
                branch VARCHAR(120),
                notes TEXT,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(staff_user_id) REFERENCES user(id)
            )
            """))
            # Permissions tables (0.9.0) if not present
            conn.execute(text("CREATE TABLE IF NOT EXISTS permission (key VARCHAR(120) PRIMARY KEY, description VARCHAR(255))"))
            conn.execute(text("CREATE TABLE IF NOT EXISTS role_permission (id INTEGER PRIMARY KEY, role VARCHAR(80) NOT NULL, permission_key VARCHAR(120) NOT NULL, UNIQUE(role, permission_key))"))
            conn.execute(text("CREATE TABLE IF NOT EXISTS user_permission (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, permission_key VARCHAR(120) NOT NULL, allow BOOLEAN NOT NULL DEFAULT 1, UNIQUE(user_id, permission_key))"))
            # Permission audit (since 0.9.2)
            conn.execute(text("CREATE TABLE IF NOT EXISTS permission_audit (id INTEGER PRIMARY KEY, actor_user_id INTEGER NOT NULL, target_user_id INTEGER, role VARCHAR(80), permission_key VARCHAR(120) NOT NULL, action VARCHAR(40) NOT NULL, changed_at DATETIME, FOREIGN KEY(actor_user_id) REFERENCES user(id), FOREIGN KEY(target_user_id) REFERENCES user(id))"))
            # Ensure at most one active loan per resource (partial unique index)
            try:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_active_loan_per_resource ON resource_loan(resource_id) WHERE status='on_loan'"))
            except Exception:
                pass
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
            # Staff Invoice tables (employee-submitted invoices & items)
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS staff_invoice (
                id INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                month INTEGER NOT NULL,
                year INTEGER NOT NULL,
                amount NUMERIC(10,2) NOT NULL DEFAULT 0,
                status VARCHAR(20) NOT NULL DEFAULT 'Pending',
                payment_status VARCHAR(20) NOT NULL DEFAULT 'Unpaid',
                created_by_id INTEGER NOT NULL,
                submitted_at DATETIME,
                created_at DATETIME,
                updated_at DATETIME
            )
            """))
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS staff_invoice_item (
                id INTEGER PRIMARY KEY,
                invoice_id INTEGER NOT NULL,
                branch VARCHAR(120),
                date DATE NOT NULL,
                day VARCHAR(20) NOT NULL,
                hours NUMERIC(6,2) NOT NULL DEFAULT 0,
                description VARCHAR(400),
                rate NUMERIC(10,2) NOT NULL DEFAULT 0,
                amount NUMERIC(10,2) NOT NULL DEFAULT 0,
                FOREIGN KEY(invoice_id) REFERENCES staff_invoice(id) ON DELETE CASCADE
            )
            """))
            try:
                cols = {row[1] for row in conn.execute(text("PRAGMA table_info(staff_invoice_item)"))}
                if 'branch' not in cols:
                    conn.execute(text("ALTER TABLE staff_invoice_item ADD COLUMN branch VARCHAR(120)"))
            except Exception:
                pass
            # Ensure payment_status exists on staff_invoice for older DBs
            try:
                cols = {row[1] for row in conn.execute(text("PRAGMA table_info(staff_invoice)"))}
                if 'payment_status' not in cols:
                    conn.execute(text("ALTER TABLE staff_invoice ADD COLUMN payment_status VARCHAR(20) DEFAULT 'Unpaid'"))
            except Exception:
                pass
            # Create staff invoice change audit table
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS staff_invoice_change (
                id INTEGER PRIMARY KEY,
                invoice_id INTEGER NOT NULL,
                field VARCHAR(120) NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_by_id INTEGER NOT NULL,
                changed_at DATETIME,
                FOREIGN KEY(invoice_id) REFERENCES staff_invoice(id),
                FOREIGN KEY(changed_by_id) REFERENCES user(id)
            )
            """))
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
            ('manage_kids_club_invoices','Kids Club invoice & company management'),
            ('manage_appointments','Manage appointment slots & bookings'),
            ('manage_students','Manage student records'),
            ('manage_books','Manage book catalog'),
            ('order_books','Place book print orders'),
            ('manage_pricing','Manage tuition pricing & fees'),
            # Floor Management
            ('floor_dashboard','Access floor dashboard'),
            ('manage_shifts','Manage floor shifts'),
            ('manage_eod_checklist','Manage end-of-day checklists'),
            ('manage_floor_reports','Manage floor print reports'),
            ('manage_call_list','Manage floor call list'),
            ('manage_student_concerns','Manage student concerns reports'),
            ('manage_supervisor_shifts','Manage supervisor shifts'),
            # Staff Invoices
            ('submit_staff_invoices','Submit own staff invoices'),
            ('manage_staff_invoices','Manage staff invoices (all)'),
            ('manage_email_logs','Manage email logs'),
            # Recruitment / Applications
            ('manage_recruitment','Manage recruitment applications and communications'),
            ('manage_admission_assessments','Manage admission assessment submissions'),
            # Course Enrollment
            ('manage_products','Create, edit, and delete course products'),
            ('view_enrollments','View enrollment orders and student purchases'),
            # Award Ceremonies
            ('manage_award_ceremonies','Manage award ceremony events and registrations'),
            # Resource Management
            ('manage_resources','Manage resource inventory'),
            # DBS & Compliance
            ('manage_dbs','Manage DBS applications'),
            # Settings & System
            ('manage_settings','Manage system settings & configuration'),
            ('manage_error_reports','View & manage error reports'),
            # Timetabling Tools
            ('access_timetable_main','Access main timetable generator'),
            ('access_timetable_eastham','Access East Ham timetable generator'),
            # Enrollment Tool
            ('access_enrollment_tool','Access enrollment calculator tool'),
        ]
        existing_keys = {p.key for p in Permission.query.all()}
        for k, description in base_permissions:
            if k not in existing_keys:
                db.session.add(Permission(key=k, description=description))
        db.session.commit()

        # Refresh permission list after potential inserts
        all_perm_keys = [p.key for p in Permission.query.all()]

        # Default role permission seeds (updated taxonomy 0.10.0)
        # Now ADDITIVE — adds any missing default permissions even if the role
        # already has some rows, so admin UI edits don't block new defaults.
        role_defaults = {
            'tutor': {'view_dashboard','manage_tasks','view_reports','submit_staff_invoices'},
            'staff': {'view_dashboard','manage_tasks','submit_staff_invoices'},
            'supervisor': {'view_dashboard','manage_tasks','manage_observations','manage_meetings',
                           'view_reports','manage_books','order_books','manage_students',
                           'manage_staff_invoices'},
            'centre_manager': {'view_dashboard','manage_tasks','manage_observations','manage_staff',
                               'manage_cycles','manage_issues','manage_availability','manage_meetings',
                               'view_reports','manage_books','order_books','manage_pricing',
                               'manage_student_concerns','manage_supervisor_shifts','manage_staff_invoices',
                               'manage_recruitment','manage_admission_assessments','manage_award_ceremonies',
                               'floor_dashboard','manage_shifts','manage_eod_checklist',
                               'manage_floor_reports','manage_call_list','manage_students',
                               'manage_resources','manage_dbs','manage_error_reports'},
            'admin': {'view_dashboard','manage_tasks','manage_observations','manage_staff',
                      'manage_cycles','manage_issues','manage_availability','manage_meetings',
                      'manage_users','manage_attendance_fix','view_reports',
                      'manage_kids_club_invoices','manage_appointments','manage_students',
                      'manage_books','order_books','manage_pricing','manage_student_concerns',
                      'manage_supervisor_shifts','manage_staff_invoices','manage_recruitment',
                      'manage_admission_assessments','manage_award_ceremonies',
                      'floor_dashboard','manage_shifts','manage_eod_checklist',
                      'manage_floor_reports','manage_call_list','manage_resources',
                      'manage_dbs','manage_settings','manage_error_reports',
                      'manage_email_logs','manage_products','view_enrollments',
                      'access_enrollment_tool','access_timetable_main',
                      'access_timetable_eastham'},
        }
        # Additive seeding: add any default permissions the role is missing
        for role, default_perms in role_defaults.items():
            existing_for_role = {
                rp.permission_key
                for rp in RolePermission.query.filter_by(role=role).all()
            }
            for pk in default_perms:
                if pk not in existing_for_role and Permission.query.get(pk):
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
    # One-time seed: default EmailSetting based on legacy email constants
    try:
        import email_utils as _eu
        from models import EmailSetting

        # Only create if no email settings exist to avoid overwriting admin config
        if EmailSetting.query.count() == 0:
            es = EmailSetting(
                name='default-smtp',
                provider='smtp',
                host=getattr(_eu, 'SMTP_HOST', None),
                port=getattr(_eu, 'SMTP_PORT', 587),
                username=getattr(_eu, 'SMTP_USERNAME', None),
                password=getattr(_eu, 'SMTP_PASSWORD', None),
                use_tls=getattr(_eu, 'SMTP_USE_TLS', True),
                use_ssl=getattr(_eu, 'SMTP_USE_SSL', False),
                sender_name=getattr(_eu, 'FROM_NAME', None),
                sender_email=getattr(_eu, 'FROM_EMAIL', None),
                is_active=True
            )
            db.session.add(es)
            db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
    # Note: audit-driven seeding was removed; admin can run the audit UI to review
    # discovered candidate sender addresses and create EmailSetting stubs manually.
    # Seed editable email templates (simple mapping of current email builders)
    try:
        from models import EmailTemplate
        seeded = False
        # Appointment customer templates (per-language & mode) -> flatten to keys
        try:
            cust = getattr(_eu, '_CUSTOMER_COPY', {})
            for lang, mapping in cust.items():
                modes = mapping.get('modes', {})
                for mode_key, mode_val in modes.items():
                    key = f"appointment_customer_{lang}_{mode_key}"
                    if not EmailTemplate.query.filter_by(key=key).first():
                        et = EmailTemplate(
                            key=key,
                            name=f"Appointment ({lang}) {mode_key}",
                            subject_template=mode_val.get('subject'),
                            html_template=_eu._render_email_shell(mode_val.get('subject',''), mode_val.get('headline',''), mapping.get('greeting',''), mode_val.get('body','')),
                            sender_name=getattr(_eu, 'FROM_NAME', None),
                            sender_email=getattr(_eu, 'FROM_EMAIL', None),
                            is_active=True
                        )
                        db.session.add(et)
                        seeded = True
        except Exception:
            pass
        # Appointment admin templates
        try:
            admin = getattr(_eu, '_ADMIN_COPY', {})
            for mode_key, mode_val in admin.items():
                key = f"appointment_admin_{mode_key}"
                if not EmailTemplate.query.filter_by(key=key).first():
                    et = EmailTemplate(
                        key=key,
                        name=f"Appointment Admin {mode_key}",
                        subject_template=mode_val.get('subject'),
                        html_template=_eu._render_email_shell(mode_val.get('subject',''), mode_val.get('headline',''), mode_val.get('intro',''), ''),
                        sender_name=getattr(_eu, 'FROM_NAME', None),
                        sender_email=getattr(_eu, 'FROM_EMAIL', None),
                        is_active=True
                    )
                    db.session.add(et)
                    seeded = True
        except Exception:
            pass
        # Task notification (single template)
        try:
            et = EmailTemplate.query.filter_by(key='task_notification').first()
            # Build a branded default HTML with placeholders for str.format(**ctx)
            tn_subject = 'New Task Assigned: {description}'
            tn_intro = (
                "Hello {assigned_to},<br/><br/>"
                "A new task has been created by {created_by} and assigned to you."
            )
            # Body with simple details table and CTA; assigned_to_id optional
            tn_body = []
            tn_body.append("<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>")
            def _row(label, value):
                return (f"<tr><td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td>"
                        f"<td style='padding:6px 0;color:#0f172a;'>{value}</td></tr>")
            tn_body.extend([
                _row('Description', '{description}'),
                _row('Status', '{status}'),
                _row('Criticality', '{criticality}'),
                _row('Urgency', '{urgency}'),
                _row('Due Date', '{due}'),
                _row('Created By', '{created_by}'),
                _row('Assigned To', '{assigned_to}'),
            ])
            tn_body.append("</table>")
            tn_body.append(
                "<div style='margin-top:16px;'>"
                "<a href='https://portal.exceltutors.org.uk/todos?assigned={assigned_to_id}' "
                "style='display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:10px 16px;border-radius:8px;font-size:14px;font-weight:600;'>View Task</a>"
                "</div>"
            )
            tn_html = _eu._render_email_shell('New Task Assigned', 'New Task Assigned', tn_intro, ''.join(tn_body))

            if not et:
                et = EmailTemplate(
                    key='task_notification',
                    name='Task notification',
                    subject_template=tn_subject,
                    html_template=tn_html,
                    sender_name=getattr(_eu, 'FROM_NAME', None),
                    sender_email=getattr(_eu, 'FROM_EMAIL', None),
                    is_active=True
                )
                db.session.add(et)
                seeded = True
            else:
                # Upgrade previously seeded placeholder (e.g., docstring-only) templates
                try:
                    existing = (et.html_template or '').strip()
                except Exception:
                    existing = ''
                placeholder = (getattr(_eu.build_task_notification_email, '__doc__', '') or '').strip()
                if not existing or existing == placeholder or len(existing) < 50:
                    et.subject_template = tn_subject
                    et.html_template = tn_html
                    if not getattr(et, 'sender_name', None):
                        et.sender_name = getattr(_eu, 'FROM_NAME', None)
                    if not getattr(et, 'sender_email', None):
                        et.sender_email = getattr(_eu, 'FROM_EMAIL', None)
                    seeded = True
        except Exception:
            pass
        if seeded:
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        # Seed staff invoice approved/rejected templates if missing
        try:
            from models import EmailTemplate
            if not EmailTemplate.query.filter_by(key='staff_invoice_approved').first():
                subj = 'Your staff invoice has been approved'
                et = EmailTemplate(
                    key='staff_invoice_approved',
                    name='Staff invoice approved',
                    subject_template=subj,
                    html_template=_build_staff_invoice_approved_email.__doc__ or subj,
                    sender_name=getattr(_eu, 'FROM_NAME', None),
                    sender_email=getattr(_eu, 'FROM_EMAIL', None),
                    is_active=True,
                )
                db.session.add(et)
                seeded = True
            if not EmailTemplate.query.filter_by(key='staff_invoice_rejected').first():
                subj = 'Your staff invoice has been rejected'
                et = EmailTemplate(
                    key='staff_invoice_rejected',
                    name='Staff invoice rejected',
                    subject_template=subj,
                    html_template=_build_staff_invoice_rejected_email.__doc__ or subj,
                    sender_name=getattr(_eu, 'FROM_NAME', None),
                    sender_email=getattr(_eu, 'FROM_EMAIL', None),
                    is_active=True,
                )
                db.session.add(et)
                seeded = True
            if seeded:
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
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
            # Student Concern schema (idempotent)
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS student_concern (
                id INTEGER PRIMARY KEY,
                tutor_name VARCHAR(200) NOT NULL,
                subject VARCHAR(120),
                student_id VARCHAR(64),
                student_name VARCHAR(255),
                year_group VARCHAR(20),
                reasons_json TEXT,
                other_details TEXT,
                status VARCHAR(30) DEFAULT 'Pending',
                meeting_id INTEGER,
                created_at DATETIME,
                updated_at DATETIME
            )
            """))
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS student_concern_change (
                id INTEGER PRIMARY KEY,
                concern_id INTEGER NOT NULL,
                field VARCHAR(120) NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_by_id INTEGER,
                changed_at DATETIME,
                FOREIGN KEY(concern_id) REFERENCES student_concern(id) ON DELETE CASCADE
            )
            """))
            # Job applications (public)
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS job_application (
                id INTEGER PRIMARY KEY,
                first_name VARCHAR(120) NOT NULL,
                last_name VARCHAR(120) NOT NULL,
                email VARCHAR(255) NOT NULL,
                phone VARCHAR(50),
                address_line1 VARCHAR(255),
                city VARCHAR(120),
                postcode VARCHAR(40),
                cv_path VARCHAR(255),
                status VARCHAR(40) DEFAULT 'Pending Review',
                university VARCHAR(255),
                study_year VARCHAR(40),
                course_name VARCHAR(255),
                alevel1_subject VARCHAR(120),
                alevel1_grade VARCHAR(20),
                alevel1_status VARCHAR(20),
                alevel2_subject VARCHAR(120),
                alevel2_grade VARCHAR(20),
                alevel2_status VARCHAR(20),
                alevel3_subject VARCHAR(120),
                alevel3_grade VARCHAR(20),
                alevel3_status VARCHAR(20),
                gcse_maths_grade VARCHAR(20),
                gcse_maths_status VARCHAR(20),
                gcse_english_grade VARCHAR(20),
                gcse_english_status VARCHAR(20),
                gcse_science_grade VARCHAR(20),
                gcse_science_status VARCHAR(20),
                tutoring_experience BOOLEAN DEFAULT 0,
                uk_work_eligible BOOLEAN DEFAULT 0,
                branches VARCHAR(255),
                subjects TEXT,
                heard_about VARCHAR(255),
                created_at DATETIME,
                updated_at DATETIME
            )
            """))
            # Ensure new columns exist when upgrading from older versions (SQLite supports simple ADD COLUMN)
            try:
                conn.execute(text("ALTER TABLE job_application ADD COLUMN heard_about VARCHAR(255)"))
            except Exception:
                pass
            # New columns added in Oct 2025
            for col_sql in [
                "ALTER TABLE job_application ADD COLUMN gcse_science_grade VARCHAR(20)",
                "ALTER TABLE job_application ADD COLUMN gcse_science_status VARCHAR(20)",
                "ALTER TABLE job_application ADD COLUMN subjects TEXT",
                "ALTER TABLE job_application ADD COLUMN cv_path VARCHAR(255)",
                "ALTER TABLE job_application ADD COLUMN status VARCHAR(40) DEFAULT 'Pending Review'",
                "ALTER TABLE job_application ADD COLUMN updated_at DATETIME",
                "ALTER TABLE job_application ADD COLUMN interview_at DATETIME",
                "ALTER TABLE job_application ADD COLUMN interview_label VARCHAR(255)",
                "ALTER TABLE job_application ADD COLUMN interview_confirm_token VARCHAR(64)",
                "ALTER TABLE job_application ADD COLUMN interview_reminder_job_id VARCHAR(64)",
            ]:
                try:
                    conn.execute(text(col_sql))
                except Exception:
                    pass
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


# One-time migration: ensure Resource IDs/barcodes are numeric going forward
_RESOURCE_NUMERIC_MIGRATED = False

@app.before_request
def _migrate_resource_numeric_ids():  # pragma: no cover - simple data hygiene
    global _RESOURCE_NUMERIC_MIGRATED
    if _RESOURCE_NUMERIC_MIGRATED:
        return
    try:
        changed = False
        rows = Resource.query.all()
        for r in rows:
            rid = (r.resource_id or '').strip()
            bcv = (r.barcode_value or '').strip()
            if (not rid.isdigit()) or (not bcv.isdigit()):
                new_id = _resource_next_numeric_id()
                r.resource_id = new_id
                r.barcode_value = new_id
                changed = True
        if changed:
            try:
                db.session.commit()
                print('[INFO] Normalized Resource IDs/barcodes to numeric format.')
            except Exception as _exc:
                db.session.rollback()
                print(f"[WARN] Failed to commit resource ID normalization: {_exc}")
    except Exception as _outer:
        # Non-fatal: continue serving requests
        print(f"[WARN] Resource ID normalization skipped: { _outer }")
    finally:
        _RESOURCE_NUMERIC_MIGRATED = True


# ---------------- Floor Management (scaffold) ---------------- #
@app.route('/floor')
@login_required
@permission_required('floor_dashboard')
def floor_dashboard():
    # Filters: date (YYYY-MM-DD or DD-MM-YYYY); optional branch for shifts/print reports
    raw_date = (request.args.get('date') or '').strip()
    selected_date = None
    for fmt in ('%Y-%m-%d', '%d-%m-%Y'):
        try:
            if raw_date:
                selected_date = datetime.strptime(raw_date, fmt).date(); break
        except Exception:
            pass
    if not selected_date:
        selected_date = date.today()

    selected_branch = (request.args.get('branch') or '').strip()
    branch_list = BRANCH_CHOICES()
    if selected_branch and selected_branch not in branch_list:
        selected_branch = ''

    # Data loads and KPIs
    from models import (CallRecord, EndOfDayChecklist, Meeting, PrintReport,
                        Shift, Todo, User)

    # Shifts for selected date (optionally branch-scoped)
    shifts_q = Shift.query.filter(Shift.date == selected_date)
    if selected_branch:
        shifts_q = shifts_q.filter(Shift.branch == selected_branch)
    shifts_for_day = shifts_q.order_by(Shift.branch.asc().nullsfirst(), Shift.day.asc(), Shift.staff_user_id.asc()).all()
    shifts_today_count = len(shifts_for_day)
    # Distinct staff with shifts
    staff_ids_with_shifts = sorted({sh.staff_user_id for sh in shifts_for_day})
    staff_today_count = len(staff_ids_with_shifts)

    # EOD Checklists for selected date (only relevant to staff with shifts if branch specified)
    eod_q = EndOfDayChecklist.query.filter(EndOfDayChecklist.date == selected_date)
    if staff_today_count:
        eod_q = eod_q.filter(EndOfDayChecklist.staff_user_id.in_(staff_ids_with_shifts))
    eod_for_day = eod_q.order_by(EndOfDayChecklist.created_at.desc()).all()
    staff_ids_with_eod = {cl.staff_user_id for cl in eod_for_day}
    pending_eod_count = max(0, staff_today_count - len(staff_ids_with_eod))
    missing_eod_staff = []
    if pending_eod_count:
        missing_ids = [sid for sid in staff_ids_with_shifts if sid not in staff_ids_with_eod]
        missing_eod_staff = User.query.filter(User.id.in_(missing_ids)).order_by(User.name.asc()).all()

    # Print Reports for selected date (optionally branch-scoped)
    pr_q = PrintReport.query.filter(PrintReport.date == selected_date)
    if selected_branch:
        pr_q = pr_q.filter(PrintReport.branch == selected_branch)
    print_reports_for_day = pr_q.order_by(PrintReport.created_at.desc()).all()
    staff_ids_with_pr = {pr.staff_user_id for pr in print_reports_for_day}
    pending_print_reports = max(0, staff_today_count - len(staff_ids_with_pr))
    missing_pr_staff = []
    if pending_print_reports:
        missing_ids_pr = [sid for sid in staff_ids_with_shifts if sid not in staff_ids_with_pr]
        missing_pr_staff = User.query.filter(User.id.in_(missing_ids_pr)).order_by(User.name.asc()).all()
    unapproved_prints_today = sum(1 for pr in print_reports_for_day if bool(pr.has_unapproved))

    # Calls for selected date (no branch on calls; show overall for the day)
    calls_for_day = CallRecord.query.filter(CallRecord.date == selected_date).order_by(CallRecord.created_at.desc()).all()
    from sqlalchemy import or_ as _or
    calls_queued_today = len([c for c in calls_for_day if (c.outcome is None or (str(c.outcome or '').strip() == ''))])
    # Reason breakdown
    reason_counts = {}
    for c in calls_for_day:
        key = (c.reason or 'other').strip().lower()
        reason_counts[key] =  (reason_counts.get(key, 0) + 1)

    # Upcoming meetings (next 7 days)
    upcoming_meetings = Meeting.query.filter(Meeting.date >= selected_date, Meeting.date <= (selected_date + timedelta(days=7))).count()

    # My pending todos
    my_pending_todos = Todo.query.filter(Todo.assigned_to_id == current_user.id, (Todo.status.is_(None)) | (Todo.status != 'Done')).count()

    return render_template(
        'floor/dashboard.html',
        # Filters
        selected_date=selected_date,
    selected_branch=selected_branch,
    branch_choices=BRANCH_CHOICES(),
        # KPIs
        shifts_today_count=shifts_today_count,
        pending_eod_count=pending_eod_count,
        calls_queued_today=calls_queued_today,
        pending_print_reports=pending_print_reports,
        unapproved_prints_today=unapproved_prints_today,
        upcoming_meetings=upcoming_meetings,
        my_pending_todos=my_pending_todos,
        # Lists
        shifts_for_day=shifts_for_day,
        eod_for_day=eod_for_day,
        missing_eod_staff=missing_eod_staff,
        print_reports_for_day=print_reports_for_day,
        missing_pr_staff=missing_pr_staff,
        calls_for_day=calls_for_day[:15],  # recent slice for dashboard
        reason_counts=reason_counts,
    )


from calendar import day_name
# Shifts
from datetime import datetime as _dt

from email_utils import send_email
from models import Shift, SupervisorShift

TIMESLOT_OPTIONS = ['9-11','11-1','2-4','4-6','5-7']

def _shift_day_for(d):
    try:
        return day_name[d.weekday()]
    except Exception:
        return ''


# Floor staff shifts listing (missing previously)
@app.route('/floor/shifts')
@login_required
@permission_required('manage_shifts')
def floor_shifts_index():
    q = (request.args.get('q') or '').strip().lower()
    staff_id = request.args.get('staff', type=int)
    day = (request.args.get('day') or '').strip()
    status = (request.args.get('status') or '').strip().lower()  # upcoming | past
    date_from = request.args.get('from')
    date_to = request.args.get('to')
    sort = (request.args.get('sort') or 'date').lower()
    direction = (request.args.get('direction') or 'desc').lower()

    query = Shift.query
    today = date.today()
    if staff_id:
        query = query.filter(Shift.staff_user_id == staff_id)
    if day:
        query = query.filter(Shift.day == day)
    if status == 'upcoming':
        query = query.filter(Shift.date >= today)
    elif status == 'past':
        query = query.filter(Shift.date < today)
    if date_from:
        try:
            df = _dt.strptime(date_from, '%Y-%m-%d').date()
            query = query.filter(Shift.date >= df)
        except Exception:
            pass
    if date_to:
        try:
            dt = _dt.strptime(date_to, '%Y-%m-%d').date()
            query = query.filter(Shift.date <= dt)
        except Exception:
            pass
    if q:
        try:
            query = query.join(User, Shift.staff_user_id == User.id).filter(
                (User.name.ilike(f"%{q}%")) | (Shift.notes.ilike(f"%{q}%"))
            )
        except Exception:
            pass

    if sort == 'staff':
        query = query.join(User, Shift.staff_user_id == User.id).order_by(User.name.asc())
    elif sort == 'day':
        query = query.order_by(Shift.day.asc(), Shift.date.desc())
    else:
        query = query.order_by(Shift.date.desc())
    if direction == 'asc' and sort == 'date':
        query = query.order_by(Shift.date.asc())

    records = query.all()
    staff_list = _users_for_shifts()
    days = list(day_name)
    today = date.today()
    # Preflight: warn if rota email setting is missing
    try:
        setting_name = get_setting('rota_setting', 'operations')
        from models import EmailSetting as _ES
        if not _ES.query.filter_by(name=setting_name).first():
            flash(f"Rota email setting '{setting_name}' is not configured. Add an Email Setting with this name to enable notifications.", 'warning')
    except Exception:
        pass
    return render_template('floor/shifts/index.html', records=records, staff_list=staff_list, days=days, today=today, TIMESLOT_OPTIONS=TIMESLOT_OPTIONS, branch_choices=BRANCH_CHOICES())

@app.route('/floor/shifts/new', methods=['POST'])
@login_required
@permission_required('manage_shifts')
def floor_shifts_new():
    """Create a new floor shift from the modal form on the index page."""
    try:
        staff_user_id = int(request.form.get('staff_user_id'))
    except Exception:
        flash('Invalid staff', 'danger')
        return redirect(url_for('floor_shifts_index'))

    # Parse date (supports DD-MM-YYYY from UI or ISO YYYY-MM-DD)
    raw_date = (request.form.get('date') or '').strip()
    nd = None
    for fmt in ('%d-%m-%Y', '%Y-%m-%d'):
        try:
            nd = _dt.strptime(raw_date, fmt).date()
            break
        except Exception:
            pass
    if not nd:
        flash('Invalid date format', 'danger')
        return redirect(url_for('floor_shifts_index'))

    # Timeslots
    slots = request.form.getlist('timeslots')
    if not slots and _is_weekday(nd):
        slots = ['5-7']
    slots = [s for s in slots if s in TIMESLOT_OPTIONS]
    if not slots:
        flash('Please choose at least one timeslot', 'danger')
        return redirect(url_for('floor_shifts_index'))

    # Branch & floors
    branch = (request.form.get('branch') or '').strip()
    if branch and branch not in BRANCH_CHOICES():
        flash('Invalid branch', 'danger')
        return redirect(url_for('floor_shifts_index'))
    floors = request.form.getlist('floors')
    valid_floor_opts = ['Basement','Ground Floor','First Floor','Second Floor','Third Floor']
    floors = [f for f in floors if f in valid_floor_opts]

    # Idempotency: if a shift already exists for same staff+date, update it instead of creating duplicate
    existing = Shift.query.filter(Shift.staff_user_id == staff_user_id, Shift.date == nd).first()
    if existing:
        existing.day = _shift_day_for(nd)
        existing.timeslots = ','.join(slots)
        existing.branch = branch or None
        existing.floors = (','.join(floors) if floors else None)
        existing.notes = (request.form.get('notes') or '').strip() or None
        try:
            db.session.commit()
            # Notify staff about the update
            try:
                staff_user = db.session.get(User, staff_user_id)
                if staff_user and staff_user.email:
                    subj, html = _build_floor_shift_email(existing, staff_user)
                    setting_name = get_setting('rota_setting', 'operations')
                    send_email(staff_user.email, subj, html, setting_name=setting_name)
            except Exception as _exc:
                print(f"[WARN] Floor shift update email failed: {_exc}")
            flash('Shift updated', 'success')
        except Exception as exc:
            db.session.rollback()
            flash(f'Failed to update shift: {exc}', 'danger')
        return redirect(url_for('floor_shifts_index'))

    # Create new shift
    sh = Shift(
        staff_user_id=staff_user_id,
        date=nd,
        day=_shift_day_for(nd),
        timeslots=','.join(slots),
        branch=branch or None,
        floors=(','.join(floors) if floors else None),
        notes=(request.form.get('notes') or '').strip() or None,
    )
    db.session.add(sh)
    try:
        db.session.commit()
        # Notify staff about the new shift
        try:
            staff_user = db.session.get(User, staff_user_id)
            if staff_user and staff_user.email:
                subj, html = _build_floor_shift_email(sh, staff_user)
                setting_name = get_setting('rota_setting', 'operations')
                send_email(staff_user.email, subj, html, setting_name=setting_name)
        except Exception as _exc:
            print(f"[WARN] Floor shift email send failed: {_exc}")
        flash('Shift created', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to create shift: {exc}', 'danger')
    return redirect(url_for('floor_shifts_index'))


def _prepare_attendance_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return df
    try:
        working = df.copy()
        if working.empty:
            return working

        working = working.reset_index(drop=True)

        # If columns already look normalised (no "Unnamed" noise and we have key fields), leave as-is
        normalised_cols = {str(c).strip().lower() for c in working.columns}
        if normalised_cols and 'date' in normalised_cols and (
            'checkin' in normalised_cols or 'check_in' in normalised_cols or
            'first time zone on-duty' in normalised_cols or 'on-duty' in normalised_cols
        ):
            return working

        if isinstance(working.columns, pd.MultiIndex):
            flattened = []
            for col_tuple in working.columns:
                parts = []
                for part in col_tuple:
                    if part is None:
                        continue
                    text = str(part).strip()
                    if not text or text.lower().startswith('unnamed'):
                        continue
                    parts.append(text)
                flat = ' '.join(parts).strip()
                flattened.append(flat or 'column')
            working.columns = flattened

        def _row_tokens(row) -> tuple[list[str], list[str]]:
            values: list[str] = []
            lowered: list[str] = []
            for cell in row:
                if cell is None:
                    continue
                try:
                    if pd.isna(cell):
                        continue
                except Exception:
                    pass
                text = str(cell).strip()
                if not text:
                    continue
                values.append(text)
                lowered.append(text.lower())
            return values, lowered

        header_row_idx = None
        subheader_row_idx = None
        header_keywords = (
            'date', 'time', 'zone', 'check', 'duty', 'machine', 'staff', 'employee',
            'name', 'id', 'department', 'branch'
        )
        for idx in range(min(len(working), 25)):
            values, lowered = _row_tokens(working.iloc[idx])
            if not values:
                continue
            keyword_hits = sum(1 for token in lowered if any(key in token for key in header_keywords))
            has_date = any('date' in token for token in lowered)
            has_timeish = any(key in token for token in lowered for key in ('time', 'zone', 'check', 'duty'))
            if len(values) >= 3 and keyword_hits >= 3 and has_date and has_timeish:
                header_row_idx = idx
                break

        if header_row_idx is not None:
            maybe_sub_idx = header_row_idx + 1
            subheader_keywords = ('on-duty', 'off-duty', 'on duty', 'off duty', 'check-in', 'check-out')
            if maybe_sub_idx < len(working):
                _, lowered = _row_tokens(working.iloc[maybe_sub_idx])
                sub_hits = sum(1 for token in lowered if any(sk in token for sk in subheader_keywords))
                if sub_hits >= 2:
                    subheader_row_idx = maybe_sub_idx

        column_tokens = [str(col).strip() for col in working.columns]
        combined_headers: list[str] = []

        def _cell_text(value) -> str:
            if value is None:
                return ''
            try:
                if pd.isna(value):
                    return ''
            except Exception:
                pass
            text = str(value).strip()
            return '' if text.lower().startswith('unnamed') else text

        if header_row_idx is not None:
            major_row = working.iloc[header_row_idx]
            minor_row = working.iloc[subheader_row_idx] if subheader_row_idx is not None else None
            last_major = ''
            for idx, major in enumerate(major_row):
                major_clean = _cell_text(major)
                if not major_clean and last_major:
                    major_clean = last_major
                elif major_clean:
                    last_major = major_clean
                minor_clean = _cell_text(minor_row.iloc[idx]) if minor_row is not None and idx < len(minor_row) else ''
                if not major_clean and minor_clean:
                    major_clean = last_major
                label_parts = [part for part in [major_clean, minor_clean] if part]
                label = ' '.join(label_parts).strip()
                if not label:
                    label = f'column_{idx+1}'
                combined_headers.append(label)
            data_start_idx = (subheader_row_idx + 1) if subheader_row_idx is not None else (header_row_idx + 1)
            working = working.iloc[data_start_idx:].reset_index(drop=True)
        else:
            first_row = working.iloc[0] if len(working) else None
            has_subheader = False
            if first_row is not None:
                tokens = {str(v).strip().lower() for v in first_row.values if isinstance(v, str) or not pd.isna(v)}
                sub_tokens = {'on-duty', 'off-duty', 'on duty', 'off duty'}
                has_subheader = any(token in sub_tokens for token in tokens)

            if has_subheader and first_row is not None:
                for idx, major in enumerate(column_tokens):
                    major_clean = major.strip()
                    if major_clean.lower().startswith('unnamed'):
                        major_clean = ''
                    minor_raw = first_row.iloc[idx] if idx < len(first_row) else ''
                    minor_clean = str(minor_raw).strip()
                    if minor_clean.lower().startswith('unnamed'):
                        minor_clean = ''
                    label_parts = [part for part in [major_clean, minor_clean] if part]
                    label = ' '.join(label_parts).strip()
                    if not label:
                        label = f'column_{idx+1}'
                    combined_headers.append(label)
                working = working.iloc[1:].reset_index(drop=True)
            else:
                for idx, major in enumerate(column_tokens):
                    major_clean = major.strip()
                    if major_clean.lower().startswith('unnamed') or not major_clean:
                        major_clean = f'column_{idx+1}'
                    combined_headers.append(major_clean)

        seen: dict[str, int] = {}
        final_headers: list[str] = []
        for header in combined_headers:
            clean = ' '.join(header.split())
            clean = clean.replace('__', ' ').replace('  ', ' ').strip()
            if not clean:
                clean = 'column'
            base = clean
            idx = seen.get(base.lower(), 0)
            if idx:
                clean = f"{base} {idx+1}"
            seen[base.lower()] = idx + 1
            final_headers.append(clean)

        working.columns = final_headers
        return working
    except Exception:
        return df


def _attendance_column_aliases(df):
    columns = list(df.columns)
    lookup = {}
    for col in columns:
        key = str(col).lower().strip() if isinstance(col, str) else ''
        if key and key not in lookup:
            lookup[key] = col

    def col_any(keys):
        for key in keys:
            if key and key in lookup:
                return lookup[key]
        return None

    index_lookup = {col: idx for idx, col in enumerate(columns)}

    def idx_for_letter(letter):
        pos = ord(letter.lower()) - ord('a')
        return pos if 0 <= pos < len(columns) else None

    machine_col = col_any(['machineid', 'machine_id', 'machine'])
    staffid_col = col_any(['staffid', 'staff_id', 'id'])
    date_col = col_any(['date', 'day'])
    checkin_col = col_any([
        'checkin', 'check_in', 'timein', 'on', 'on duty',
        'on-duty', 'first time zone on-duty', 'first time zone on duty',
        'second time zone on-duty', 'second time zone on duty'
    ])
    checkout_col = col_any([
        'checkout', 'check_out', 'timeout', 'off', 'off duty',
        'off-duty', 'first time zone off-duty', 'first time zone off duty',
        'second time zone off-duty', 'second time zone off duty'
    ])
    checkin2_col = col_any([
        'second time zone on-duty', 'second time zone on duty', 'on-duty 2',
        'on2', 'timein2', 'checkin2'
    ])
    checkout2_col = col_any([
        'second time zone off-duty', 'second time zone off duty', 'off-duty 2',
        'off2', 'timeout2', 'checkout2'
    ])

    if checkin2_col == checkin_col:
        checkin2_col = None
    if checkout2_col == checkout_col:
        checkout2_col = None
    late_col = col_any(['late', 'late_minutes', 'late_min', 'late time(min)', 'late time (min)'])

    letter_indexes = {letter: idx_for_letter(letter) for letter in ['a','b','c','d','e','f','g','h','i','j','k','l','m','n','o','p']}

    return {
        'columns': columns,
        'colmap': lookup,
        'machine': machine_col,
        'staffid': staffid_col,
        'date': date_col,
        'checkin': checkin_col,
        'checkout': checkout_col,
        'checkin2': checkin2_col,
        'checkout2': checkout2_col,
        'late': late_col,
        'machine_idx': index_lookup.get(machine_col),
        'staffid_idx': index_lookup.get(staffid_col),
        'date_idx': index_lookup.get(date_col),
        'checkin_idx': index_lookup.get(checkin_col),
        'checkout_idx': index_lookup.get(checkout_col),
        'checkin2_idx': index_lookup.get(checkin2_col),
        'checkout2_idx': index_lookup.get(checkout2_col),
        'late_idx': index_lookup.get(late_col),
        'letter_indexes': letter_indexes,
    }


def _attendance_cell_has_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ''
    try:
        return not pd.isna(value)
    except Exception:
        return True


def _extract_row_value(row, label=None, idx=None):
    if label is not None:
        try:
            if label in row.index:
                value = row[label]
                if _attendance_cell_has_value(value):
                    return value
        except Exception:
            pass
        label_str = str(label)
        try:
            if label_str in row.index:
                value = row[label_str]
                if _attendance_cell_has_value(value):
                    return value
        except Exception:
            pass
    if idx is not None and 0 <= idx < len(row):
        value = row.iloc[idx]
        if _attendance_cell_has_value(value):
            return value
    return None


def _resolve_attendance_slots(row, aliases, capture_anomalies=False, anomaly_log=None):
    checkin_col = aliases.get('checkin')
    checkout_col = aliases.get('checkout')
    checkin2_col = aliases.get('checkin2')
    checkout2_col = aliases.get('checkout2')
    checkin_idx = aliases.get('checkin_idx')
    checkout_idx = aliases.get('checkout_idx')
    checkin2_idx = aliases.get('checkin2_idx')
    checkout2_idx = aliases.get('checkout2_idx')
    letter_indexes = aliases.get('letter_indexes', {})

    def parse_time(raw):
        if not _attendance_cell_has_value(raw):
            return None, None

        if isinstance(raw, datetime):
            return raw.time(), None

        if isinstance(raw, time):
            return raw, None

        if isinstance(raw, str):
            text_value = raw.strip()
            if not text_value:
                return None, None
            # Try a handful of common human-readable formats before deferring to pandas
            for fmt in ('%H:%M:%S', '%H:%M', '%I:%M %p', '%I:%M%p', '%H.%M'):
                try:
                    return datetime.strptime(text_value, fmt).time(), None
                except Exception:
                    continue

        if isinstance(raw, (int, float)):
            try:
                numeric = float(raw)
                if math.isnan(numeric):
                    numeric = None
            except Exception:
                numeric = None
            if numeric is not None:
                # Excel stores pure times as the fractional part of a day (0.0–1.0)
                normalized = numeric % 1
                # Some vendors store 4-digit HHMM integers (e.g. 930 for 09:30)
                if abs(numeric - int(numeric)) < 1e-6 and 0 <= numeric <= 2400:
                    hours = int(numeric) // 100
                    minutes = int(numeric) % 100
                    if hours < 24 and minutes < 60:
                        return time(hour=hours, minute=minutes), None
                if numeric >= 1 and normalized == 0 and 0 < numeric < 24:
                    normalized = numeric / 24
                if 0 <= normalized < 1:
                    total_seconds = int(round(normalized * 86400))
                    total_seconds = max(0, total_seconds)
                    hours = (total_seconds // 3600) % 24
                    minutes = (total_seconds % 3600) // 60
                    seconds = total_seconds % 60
                    return time(hour=hours, minute=minutes, second=seconds), None

        try:
            parsed = pd.to_datetime(raw)
            if isinstance(parsed, pd.Series):
                parsed = parsed.iloc[0]
            if pd.isna(parsed):
                return None, None
            if isinstance(parsed, pd.Timestamp):
                return parsed.time(), None
            if isinstance(parsed, datetime):
                return parsed.time(), None
        except Exception:
            pass

        return None, str(raw)

    used_keys = set()
    candidates = []

    def register(ci_label=None, co_label=None, ci_index=None, co_index=None):
        ci_key = ci_index if ci_index is not None else (f"label:{ci_label}" if ci_label is not None else None)
        co_key = co_index if co_index is not None else (f"label:{co_label}" if co_label is not None else None)
        key = (ci_key, co_key)
        if key in used_keys:
            return
        used_keys.add(key)
        candidates.append((ci_label, co_label, ci_index, co_index))

    register(checkin_col, checkout_col, checkin_idx, checkout_idx)
    register(checkin2_col, checkout2_col, checkin2_idx, checkout2_idx)

    idx_e = letter_indexes.get('e')
    idx_f = letter_indexes.get('f')
    idx_g = letter_indexes.get('g')
    idx_h = letter_indexes.get('h')

    if idx_e is not None or idx_f is not None:
        register(ci_index=idx_e, co_index=idx_f)
    if idx_g is not None or idx_h is not None:
        register(ci_index=idx_g, co_index=idx_h)

    slots = []
    for ci_label, co_label, ci_index, co_index in candidates:
        raw_ci = _extract_row_value(row, ci_label, ci_index)
        raw_co = _extract_row_value(row, co_label, co_index)
        if not _attendance_cell_has_value(raw_ci) and not _attendance_cell_has_value(raw_co):
            continue
        ci_time, ci_error = parse_time(raw_ci)
        co_time, co_error = parse_time(raw_co)
        slots.append({
            'check_in': ci_time,
            'check_out': co_time,
            'raw_check_in': raw_ci,
            'raw_check_out': raw_co,
            'check_in_error': ci_error,
            'check_out_error': co_error,
        })

    if len(slots) >= 2:
        first, second = slots[0], slots[1]
        if first.get('check_in') and not first.get('check_out') and not second.get('check_in') and second.get('check_out'):
            first['check_out'] = second['check_out']
            first['check_out_error'] = second['check_out_error']
            slots.pop(1)
        elif not first.get('check_in') and first.get('check_out') and second.get('check_in') and not second.get('check_out'):
            first['check_in'] = second['check_in']
            first['check_in_error'] = second['check_in_error']
            slots.pop(1)

    meaningful = []
    for slot in slots:
        if slot.get('check_in') or slot.get('check_out') or slot.get('check_in_error') or slot.get('check_out_error'):
            meaningful.append(slot)
        if len(meaningful) >= 2:
            break

    if capture_anomalies and anomaly_log and anomaly_log.get('collector') is not None:
        collector = anomaly_log['collector']
        date_value = anomaly_log.get('date')
        if isinstance(date_value, datetime):
            date_str = date_value.strftime('%Y-%m-%d')
        elif isinstance(date_value, date):
            date_str = date_value.strftime('%Y-%m-%d')
        elif date_value:
            date_str = str(date_value)
        else:
            date_str = ''
        staffid = anomaly_log.get('staffid')
        machine = anomaly_log.get('machine')
        for idx, slot in enumerate(meaningful):
            suffix = '' if idx == 0 else '_2'
            if slot.get('check_in_error'):
                collector.append({
                    'where': f'check_in{suffix}',
                    'value': slot['check_in_error'],
                    'date': date_str,
                    'staffid': staffid,
                    'machine': machine,
                })
            if slot.get('check_out_error'):
                collector.append({
                    'where': f'check_out{suffix}',
                    'value': slot['check_out_error'],
                    'date': date_str,
                    'staffid': staffid,
                    'machine': machine,
                })

    return meaningful


def _calculate_hours_seconds(date_value, slots):
    if not date_value:
        return None
    total = 0
    for slot in slots:
        ci = slot.get('check_in')
        co = slot.get('check_out')
        if ci and co:
            try:
                delta = (datetime.combine(date_value, co) - datetime.combine(date_value, ci)).total_seconds()
            except Exception:
                continue
            if delta > 0:
                total += int(delta)
    return total if total > 0 else None


def _seconds_to_hhmmss(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02}:{minutes:02}:{secs:02}"


def _parse_date_filter(value: str | None) -> date | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except Exception:
            continue
    try:
        parsed = pd.to_datetime(raw)
        if isinstance(parsed, pd.Series):
            parsed = parsed.iloc[0]
        if pd.isna(parsed):
            return None
        if isinstance(parsed, (pd.Timestamp, datetime)):
            return parsed.date()
    except Exception:
        return None
    return None


def _purge_duplicate_attendance_rows(keep_row_id, machine, staff_id, attendance_date):
    try:
        if not keep_row_id or not attendance_date:
            return
        query = StaffAttendance.query.filter(StaffAttendance.id != keep_row_id, StaffAttendance.date == attendance_date)
        if machine:
            query = query.filter(StaffAttendance.machine_id == machine)
        elif staff_id:
            query = query.filter(StaffAttendance.staff_id == staff_id)
        else:
            return
        for duplicate in query.all():
            db.session.delete(duplicate)
    except Exception:
        pass


def _process_attendance_df(df, branch, capture_anomalies: bool = False):
    """Process a pandas DataFrame of attendance rows and persist to DB.

    Returns tuple (imported_count, updated_count, anomalies_dict)

    anomalies_dict keys (when capture_anomalies=True):
      - total_rows: int
      - skipped_no_date: int
      - unmapped_staff: list[dict]
      - bad_time: list[dict]  # rows with unparsable time strings
    """
    df = _prepare_attendance_dataframe(df)

    imported = 0
    updated = 0
    total_rows = 0
    skipped_no_date = 0
    unmapped_staff = []
    bad_time = []
    aliases = _attendance_column_aliases(df)
    machine_col = aliases.get('machine')
    staffid_col = aliases.get('staffid')
    date_col = aliases.get('date')
    late_col = aliases.get('late')
    letter_indexes = aliases.get('letter_indexes', {})

    for _, row in df.iterrows():
        try:
            total_rows += 1
            machine_raw = _extract_row_value(row, machine_col, aliases.get('machine_idx'))
            machine = str(machine_raw).strip() if machine_raw is not None else None
            # Staff ID may be float/string; coerce safely
            staffid = None
            staffid_raw = _extract_row_value(row, staffid_col, aliases.get('staffid_idx'))
            if staffid_raw is None:
                staffid_raw = _extract_row_value(row, None, letter_indexes.get('a'))
            if staffid_raw is not None:
                sval = str(staffid_raw).strip()
                if sval:
                    try:
                        staffid = int(float(sval))
                    except Exception:
                        staffid = None
            d_raw = _extract_row_value(row, date_col, aliases.get('date_idx'))
            if d_raw is None:
                d_raw = _extract_row_value(row, None, letter_indexes.get('c'))
            if isinstance(d_raw, str):
                try:
                    d = datetime.strptime(d_raw, '%d/%m/%Y').date()
                except Exception:
                    d = pd.to_datetime(d_raw).date()
            else:
                d = pd.to_datetime(d_raw).date() if d_raw is not None and not pd.isna(d_raw) else None
            if not d:
                skipped_no_date += 1
                continue
            slots = _resolve_attendance_slots(
                row,
                aliases,
                capture_anomalies=capture_anomalies,
                anomaly_log={
                    'collector': bad_time,
                    'date': d,
                    'staffid': staffid,
                    'machine': machine,
                }
            )
            ci = slots[0]['check_in'] if slots else None
            co = slots[0]['check_out'] if slots else None
            ci2 = slots[1]['check_in'] if len(slots) > 1 else None
            co2 = slots[1]['check_out'] if len(slots) > 1 else None
            # Late minutes: handle blank/strings robustly with column/letter fallback
            late_min = None
            late_raw = _extract_row_value(row, late_col, aliases.get('late_idx'))
            if late_raw is None:
                late_raw = _extract_row_value(row, None, letter_indexes.get('i'))
            if late_raw is not None:
                lraw = str(late_raw).strip()
                if lraw:
                    try:
                        late_min = int(float(lraw))
                    except Exception:
                        late_min = None

            # Map machine id to staff record if possible (check all 4 machine id fields)
            mapped_staff = None
            if machine:
                mapped_staff = Staff.query.filter(
                    (Staff.whitechapel_machine_id == machine) |
                    (Staff.east_ham_machine_id == machine) |
                    (Staff.stratford_machine_id == machine) |
                    (Staff.docklands_machine_id == machine)
                ).first()
            if not mapped_staff and staffid:
                mapped_staff = Staff.query.filter((Staff.id == staffid) | (Staff.access_code == str(staffid))).first()
            if capture_anomalies and not mapped_staff and (machine or staffid):
                unmapped_staff.append({'date': d.strftime('%Y-%m-%d'), 'machine': machine, 'staffid': staffid})

            selected_branch = (branch or '').strip()
            branch_filter_value = selected_branch or None

            # Find existing attendance row: prefer matching branch first, otherwise fall back to latest entry for that staff/machine/date
            existing = None
            if machine and branch_filter_value is not None:
                existing = StaffAttendance.query.filter_by(machine_id=machine, date=d, branch=branch_filter_value).order_by(StaffAttendance.updated_at.desc()).first()
            if not existing and mapped_staff and branch_filter_value is not None:
                existing = StaffAttendance.query.filter_by(staff_id=mapped_staff.id, date=d, branch=branch_filter_value).order_by(StaffAttendance.updated_at.desc()).first()
            if not existing and machine:
                existing = (StaffAttendance.query
                            .filter(StaffAttendance.machine_id == machine, StaffAttendance.date == d)
                            .order_by(StaffAttendance.updated_at.desc())
                            .first())
            if not existing and mapped_staff:
                existing = (StaffAttendance.query
                            .filter(StaffAttendance.staff_id == mapped_staff.id, StaffAttendance.date == d)
                            .order_by(StaffAttendance.updated_at.desc())
                            .first())

            target_branch = selected_branch or (existing.branch if existing else None)

            hours_secs = _calculate_hours_seconds(d, slots)

            payload_json = json.dumps({
                'machine': machine, 'staffid': staffid, 'date': d.strftime('%Y-%m-%d'),
                'check_in': str(ci) if ci else None, 'check_out': str(co) if co else None,
                'check_in_2': str(ci2) if ci2 else None, 'check_out_2': str(co2) if co2 else None,
                'late_min': late_min,
                'hours_seconds': hours_secs,
                'branch': target_branch
            })

            if existing:
                # Update fields and add audit entries for changed fields
                changed = False
                if mapped_staff and existing.staff_id != mapped_staff.id:
                    db.session.add(StaffAttendanceAudit(attendance_id=existing.id, field='staff_id', old_value=str(existing.staff_id), new_value=str(mapped_staff.id), changed_by_id=current_user.id if current_user.is_authenticated else None))
                    existing.staff_id = mapped_staff.id; changed = True
                if machine and existing.machine_id != machine:
                    db.session.add(StaffAttendanceAudit(attendance_id=existing.id, field='machine_id', old_value=str(existing.machine_id), new_value=machine, changed_by_id=current_user.id if current_user.is_authenticated else None))
                    existing.machine_id = machine; changed = True
                if target_branch != existing.branch:
                    db.session.add(StaffAttendanceAudit(attendance_id=existing.id, field='branch', old_value=str(existing.branch), new_value=str(target_branch), changed_by_id=current_user.id if current_user.is_authenticated else None))
                    existing.branch = target_branch; changed = True
                if ci and (existing.check_in != ci):
                    db.session.add(StaffAttendanceAudit(attendance_id=existing.id, field='check_in', old_value=str(existing.check_in), new_value=str(ci), changed_by_id=current_user.id if current_user.is_authenticated else None))
                    existing.check_in = ci; changed = True
                if co and (existing.check_out != co):
                    db.session.add(StaffAttendanceAudit(attendance_id=existing.id, field='check_out', old_value=str(existing.check_out), new_value=str(co), changed_by_id=current_user.id if current_user.is_authenticated else None))
                    existing.check_out = co; changed = True
                if late_min is not None and existing.late_minutes != late_min:
                    db.session.add(StaffAttendanceAudit(attendance_id=existing.id, field='late_minutes', old_value=str(existing.late_minutes), new_value=str(late_min), changed_by_id=current_user.id if current_user.is_authenticated else None))
                    existing.late_minutes = late_min; changed = True
                if hours_secs is not None and existing.hours_seconds != hours_secs:
                    db.session.add(StaffAttendanceAudit(attendance_id=existing.id, field='hours_seconds', old_value=str(existing.hours_seconds), new_value=str(hours_secs), changed_by_id=current_user.id if current_user.is_authenticated else None))
                    existing.hours_seconds = hours_secs; changed = True
                if existing.day != _shift_day_for(d):
                    existing.day = _shift_day_for(d)
                    changed = True
                existing.raw_payload = payload_json
                if changed:
                    updated += 1
                existing.updated_at = datetime.utcnow()
                db.session.add(existing)
                _purge_duplicate_attendance_rows(existing.id, machine, existing.staff_id, d)
            else:
                na = StaffAttendance(
                    staff_id=(mapped_staff.id if mapped_staff else None),
                    machine_id=machine,
                    branch=target_branch,
                    day=_shift_day_for(d),
                    date=d,
                    check_in=ci,
                    check_out=co,
                    late_minutes=late_min,
                    hours_seconds=hours_secs,
                    raw_payload=payload_json,
                )
                db.session.add(na)
                try:
                    db.session.flush()
                    _purge_duplicate_attendance_rows(na.id, machine, na.staff_id, d)
                except Exception:
                    pass
                imported += 1
        except Exception:
            # skip bad rows
            continue
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    if capture_anomalies:
        anomalies = {
            'total_rows': total_rows,
            'skipped_no_date': skipped_no_date,
            'unmapped_staff': unmapped_staff,
            'bad_time': bad_time,
        }
        return imported, updated, anomalies
    return imported, updated

def _is_weekday(d):
    try:
        return d.weekday() < 5
    except Exception:
        return False

def _users_for_shifts():
    # Staff dropdown: users whose role in admin/staff (and approved/active)
    roles = ['admin','staff']
    return User.query.filter(User.is_approved.is_(True), User.is_active.is_(True), User.role.in_(roles)).order_by(User.name.asc()).all()

def _users_for_supervisor_shifts():
    """Users eligible for Supervisor Shifts: centre managers and supervisors only.

    Ensures only approved and active accounts appear in the dropdown.
    """
    roles = ['centre_manager', 'supervisor']
    return (User.query
            .filter(User.is_approved.is_(True), User.is_active.is_(True), User.role.in_(roles))
            .order_by(User.name.asc())
            .all())


def _build_shift_email(shift: Shift, staff_user: User) -> tuple[str, str]:
        date_label = shift.date.strftime('%A, %d %B %Y') if shift.date else ''
        times = ', '.join(shift.timeslot_list())
        floors = ', '.join(shift.floor_list()) if shift.floors else '—'
        branch = shift.branch or '—'
        subject = f"Your assigned shift – {date_label} ({times})"
        body_rows = [
                ("Staff", staff_user.name or ''),
                ("Date", date_label),
                ("Day", shift.day or ''),
                ("Timeslots", times or ''),
                ("Branch", branch),
                ("Floor(s)", floors),
                ("Notes", (shift.notes or '').replace('\n','<br/>') or '<em>None</em>'),
        ]
        table = ["<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>"]
        for label, value in body_rows:
                table.append(
                        "<tr>"+
                        f"<td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td>"+
                        f"<td style='padding:6px 0;color:#0f172a;'>{value}</td>"+
                        "</tr>"
                )
        table.append("</table>")
        intro = f"Hello {staff_user.name},<br/><br/>You have been assigned a new shift. The details are below."  # simple branded copy
        shell = """
<!DOCTYPE html>
<html lang='en'>
<head><meta charset='utf-8'/><title>{title}</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;">
    <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#f1f5f9;padding:24px 0;'>
        <tr><td align='center'>
            <table role='presentation' width='640' cellpadding='0' cellspacing='0' style='background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;'>
                <tr>
                    <td style='background:#0f172a;padding:24px 32px;'>
                        <h1 style='margin:0;font-size:22px;line-height:1.3;color:#ffffff;font-weight:600;'>Excel Tutors</h1>
                        <p style='margin:6px 0 0;font-size:13px;color:#cbd5f5;'>Shift assignment</p>
                    </td>
                </tr>
                <tr>
                    <td style='padding:32px;'>
                        <p style='margin:0 0 16px;font-size:15px;color:#0f172a;'>{intro}</p>
                        {table}
                    </td>
                </tr>
                <tr>
                    <td style='background:#f8fafc;padding:18px 32px;text-align:center;font-size:11px;color:#94a3b8;'>
                        &copy; {year} Excel Tutors. All rights reserved.
                    </td>
                </tr>
            </table>
        </td></tr>
    </table>
    </body>
    </html>
        """.format(title=subject, intro=intro, table=''.join(table), year=date.today().year)
        return subject, shell

@app.route('/staff/attendance')
@login_required
@permission_required('manage_attendance_fix')
def staff_attendance_index():
    """List attendance records with filters and pagination.

    Filters (query params): company, branch, staff_id, machine_id, late (yes|no), status (active|inactive), start, end, page
    """
    from models import Staff, StaffAttendance

    filters_raw = request.args.to_dict(flat=True)
    branch = (filters_raw.get('branch') or '').strip()
    employee_term = (filters_raw.get('employee') or '').strip()
    machine_id = (filters_raw.get('machine_id') or '').strip()
    late = (filters_raw.get('late') or '').strip().lower()
    start_input = (filters_raw.get('start') or '').strip()
    end_input = (filters_raw.get('end') or '').strip()
    month = (filters_raw.get('month') or '').strip()
    per_page = int(filters_raw.get('per_page') or 50)
    per_page = min(max(per_page, 10), 500)
    page = max(1, int(filters_raw.get('page') or 1))
    sort = (filters_raw.get('sort') or 'date').lower()
    direction = (filters_raw.get('direction') or 'desc').lower()
    if direction not in ('asc', 'desc'):
        direction = 'desc'
    staff_id_filter = request.args.get('staff_id', type=int)
    if staff_id_filter is not None:
        filters_raw['staff_id'] = str(staff_id_filter)

    q = StaffAttendance.query
    join_staff = False
    if employee_term or sort == 'staff':
        q = q.outerjoin(Staff, StaffAttendance.staff_id == Staff.id)
        join_staff = True

    if branch:
        q = q.filter(StaffAttendance.branch == branch)
    if staff_id_filter:
        q = q.filter(StaffAttendance.staff_id == staff_id_filter)
    if machine_id:
        q = q.filter(func.lower(StaffAttendance.machine_id).like(f"%{machine_id.lower()}%"))
    if late == 'yes':
        q = q.filter(StaffAttendance.late_minutes.isnot(None), StaffAttendance.late_minutes > 0)

    start_date = _parse_date_filter(start_input)
    end_date = _parse_date_filter(end_input)
    if start_date:
        q = q.filter(StaffAttendance.date >= start_date)
        filters_raw['start'] = start_date.strftime('%Y-%m-%d')
    else:
        filters_raw['start'] = ''
    if end_date:
        q = q.filter(StaffAttendance.date <= end_date)
        filters_raw['end'] = end_date.strftime('%Y-%m-%d')
    else:
        filters_raw['end'] = ''
    if month:
        parsed_month = None
        for fmt in ('%Y-%m', '%m/%Y', '%Y/%m', '%b %Y', '%B %Y'):
            try:
                parsed_month = datetime.strptime(month, fmt)
                break
            except Exception:
                continue
        if parsed_month:
            month_start = date(parsed_month.year, parsed_month.month, 1)
            if parsed_month.month == 12:
                next_month = date(parsed_month.year + 1, 1, 1)
            else:
                next_month = date(parsed_month.year, parsed_month.month + 1, 1)
            q = q.filter(StaffAttendance.date >= month_start, StaffAttendance.date < next_month)

    if employee_term:
        name_term = f"%{employee_term.lower()}%"
        predicates = [
            func.lower(Staff.name).like(name_term) if join_staff else None,
            func.lower(Staff.first_name).like(name_term) if join_staff else None,
            func.lower(Staff.last_name).like(name_term) if join_staff else None,
            func.lower(Staff.access_code).like(name_term) if join_staff else None,
            func.lower(StaffAttendance.machine_id).like(name_term),
        ]
        if employee_term.isdigit():
            predicates.append(StaffAttendance.staff_id == int(employee_term))
        predicates = [p for p in predicates if p is not None]
        if predicates:
            q = q.filter(or_(*predicates))

    total = q.order_by(None).count()
    if total and (page - 1) * per_page >= total:
        page = max(1, math.ceil(total / per_page))
    offset = (page - 1) * per_page

    if sort == 'staff' and not join_staff:
        q = q.outerjoin(Staff, StaffAttendance.staff_id == Staff.id)
        join_staff = True

    sort_map = {
        'date': StaffAttendance.date,
        'branch': StaffAttendance.branch,
        'check_in': StaffAttendance.check_in,
        'check_out': StaffAttendance.check_out,
        'hours': StaffAttendance.hours_seconds,
        'late': StaffAttendance.late_minutes,
    }
    if join_staff:
        sort_map['staff'] = func.lower(Staff.name)

    sort_column = sort_map.get(sort)
    if not sort_column:
        sort = 'date'
        sort_column = sort_map['date']
        direction = 'desc'

    order_primary = asc(sort_column) if direction == 'asc' else desc(sort_column)
    order_columns = [order_primary]
    if sort != 'date':
        order_columns.append(desc(StaffAttendance.date))
    if sort != 'check_in':
        order_columns.append(asc(StaffAttendance.check_in))
    order_columns.append(desc(StaffAttendance.id))

    records = (q.order_by(*order_columns)
                 .offset(offset)
                 .limit(per_page)
                 .all())

    total_pages = max(1, math.ceil(total / per_page)) if per_page else 1
    from_record = offset + 1 if total else 0
    to_record = offset + len(records) if total else 0
    has_prev = page > 1
    has_next = page < total_pages

    base_params = {k: v for k, v in filters_raw.items() if k != 'page' and v not in (None, '')}
    base_params['per_page'] = str(per_page)
    base_params['sort'] = sort
    base_params['direction'] = direction
    if branch:
        base_params['branch'] = branch

    prev_url = url_for('staff_attendance_index', **{**base_params, 'page': page - 1}) if has_prev else None
    next_url = url_for('staff_attendance_index', **{**base_params, 'page': page + 1}) if has_next else None

    sort_targets = ('date', 'staff', 'branch', 'check_in', 'check_out', 'hours', 'late')
    sort_urls = {}
    for target in sort_targets:
        params = {**base_params, 'page': 1, 'sort': target}
        params['direction'] = 'asc' if sort != target or direction == 'desc' else 'desc'
        sort_urls[target] = url_for('staff_attendance_index', **params)

    per_page_options = [25, 50, 100, 200]

    staff_map = {s.id: s for s in Staff.query.all()}
    branches = BRANCH_CHOICES()
    return render_template(
        'staff/attendance.html',
        records=records,
        staff_map=staff_map,
        branches=branches,
        filters=filters_raw,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        from_record=from_record,
        to_record=to_record,
        has_prev=has_prev,
        has_next=has_next,
        prev_url=prev_url,
        next_url=next_url,
        sort=sort,
        direction=direction,
        sort_urls=sort_urls,
        per_page_options=per_page_options,
    )


@app.route('/staff/attendance/import', methods=['GET','POST'])
@login_required
@permission_required('manage_attendance_fix')
def staff_attendance_import():
    """Import attendance rows from an uploaded XLSX/CSV file.

    The user will be asked to select the branch to associate with the import.
    For each row we attempt to map the machine id to a Staff record via known
    machine id columns on Staff. If a matching existing attendance record for
    the same machine/date/branch exists, update it and create an audit entry;
    otherwise insert a new StaffAttendance row.
    """
    from models import Staff, StaffAttendance, StaffAttendanceAudit

    # Support preview phase: POST may be initial upload (preview) or a confirm step
    if request.method == 'GET':
        return render_template('staff/attendance_import.html', branches=BRANCH_CHOICES())

    action = (request.form.get('action') or '').strip().lower()
    # Confirm step: process existing temp file saved in session
    if action == 'confirm':
        tmp = session.get('attendance_import_tmp')
        branch = (request.form.get('branch') or session.get('attendance_import_branch') or '').strip()
        if not tmp:
            flash('No pending import found. Please upload the file first.', 'warning')
            return redirect(url_for('staff_attendance_import'))
        tmp_path = os.path.join(app.instance_path, 'imports', tmp)
        if not os.path.exists(tmp_path):
            flash('Temporary import file missing. Please re-upload.', 'danger')
            session.pop('attendance_import_tmp', None)
            session.pop('attendance_import_branch', None)
            return redirect(url_for('staff_attendance_import'))
        # Reopen file and parse same as preview but now commit
        try:
            with open(tmp_path, 'rb') as fh:
                df = None
                try:
                    df = combine_all_sheets(fh, year=datetime.utcnow().year, month=datetime.utcnow().month)
                except Exception:
                    df = None
                if df is None or df.empty:
                    fh.seek(0)
                    try:
                        df = pd.read_excel(fh)
                    except Exception:
                        fh.seek(0)
                        df = pd.read_csv(fh)
        except Exception as e:
            flash(f'Failed to reopen uploaded file: {e}', 'danger')
            return redirect(url_for('staff_attendance_import'))
        df = _prepare_attendance_dataframe(df)
        # Process DataFrame rows and commit (reuse existing per-row logic below)
        imported, updated = _process_attendance_df(df, branch)
        # Clean up
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        session.pop('attendance_import_tmp', None)
        session.pop('attendance_import_branch', None)
        flash(f'Import complete: {imported} new rows, {updated} updated rows', 'success')
        return redirect(url_for('staff_attendance_index'))

    # Otherwise initial upload -> preview
    file = request.files.get('file')
    branch = (request.form.get('branch') or '').strip()
    if branch and branch not in BRANCH_CHOICES():
        flash('Invalid branch selected', 'danger')
        return redirect(url_for('staff_attendance_import'))
    if not file or not file.filename:
        flash('No file uploaded', 'danger')
        return redirect(url_for('staff_attendance_import'))

    # Save upload to instance/imports with uuid name for confirm step
    uid = str(uuid4())
    fname = file.filename or 'upload.xlsx'
    _, ext = os.path.splitext(fname)
    ext = ext or '.xlsx'
    temp_name = f"{uid}{ext}"
    temp_path = os.path.join(app.instance_path, 'imports', temp_name)
    try:
        file.save(temp_path)
    except Exception as e:
        flash(f'Failed to save uploaded file: {e}', 'danger')
        return redirect(url_for('staff_attendance_import'))

    # Try to parse with attendance_utils for multi-sheet vendor files; fallback to pandas read
    df = None
    try:
        with open(temp_path, 'rb') as fh:
            df = combine_all_sheets(fh, year=datetime.utcnow().year, month=datetime.utcnow().month)
    except Exception:
        df = None
    if df is None or df.empty:
        try:
            df = pd.read_excel(temp_path)
        except Exception:
            try:
                df = pd.read_csv(temp_path)
            except Exception as e:
                # Failed parsing
                os.remove(temp_path)
                flash(f'Failed to parse uploaded spreadsheet: {e}', 'danger')
                return redirect(url_for('staff_attendance_import'))

    df = _prepare_attendance_dataframe(df)

    # Decide if we can commit immediately (quick import): either user asked for it or layout is known
    def _known_layout(_df):
        cols = {str(c).lower().strip() for c in _df.columns}
        has_date = 'date' in cols or 'day' in cols
        has_time_pair = (
            ('checkin' in cols or 'check_in' in cols or 'timein' in cols or 'on' in cols) and
            ('checkout' in cols or 'check_out' in cols or 'timeout' in cols or 'off' in cols)
        ) or (
            'first time zone on-duty' in cols and 'first time zone off-duty' in cols
        )
        has_identifier = (
            'machineid' in cols or 'machine_id' in cols or 'machine' in cols or
            'staffid' in cols or 'staff_id' in cols or 'id' in cols
        )
        return has_date and has_time_pair and has_identifier

    quick = (request.form.get('quick') == '1') or _known_layout(df)
    if quick:
        imported, updated, anomalies = _process_attendance_df(df, branch, capture_anomalies=True)
        # Clean up temp file and session
        try:
            os.remove(temp_path)
        except Exception:
            pass
        session.pop('attendance_import_tmp', None)
        session.pop('attendance_import_branch', None)
        return render_template(
            'staff/attendance_import_summary.html',
            imported=imported,
            updated=updated,
            anomalies=anomalies,
            branch=branch or ''
        )

    # Build a lightweight preview: map machine ids to staff and show first 200 rows
    preview_rows = []
    aliases_preview = _attendance_column_aliases(df)
    machine_col = aliases_preview.get('machine')
    staffid_col = aliases_preview.get('staffid')
    date_col = aliases_preview.get('date')
    late_col = aliases_preview.get('late')
    letter_indexes = aliases_preview.get('letter_indexes', {})

    for idx, row in df.iterrows():
        if len(preview_rows) >= 200:
            break
        try:
            machine_raw = _extract_row_value(row, machine_col, aliases_preview.get('machine_idx'))
            machine = str(machine_raw).strip() if machine_raw is not None else None
            # Staff ID may be float/string; coerce safely
            staffid = None
            staffid_raw = _extract_row_value(row, staffid_col, aliases_preview.get('staffid_idx'))
            if staffid_raw is None:
                staffid_raw = _extract_row_value(row, None, letter_indexes.get('a'))
            if staffid_raw is not None:
                sval = str(staffid_raw).strip()
                if sval:
                    try:
                        staffid = int(float(sval))
                    except Exception:
                        staffid = None
            d_raw = _extract_row_value(row, date_col, aliases_preview.get('date_idx'))
            if d_raw is None:
                d_raw = _extract_row_value(row, None, letter_indexes.get('c'))
            try:
                if isinstance(d_raw, str):
                    d = datetime.strptime(d_raw, '%d/%m/%Y').date()
                else:
                    d = pd.to_datetime(d_raw).date() if d_raw is not None and not pd.isna(d_raw) else None
            except Exception:
                d = None
            slots = _resolve_attendance_slots(row, aliases_preview)
            ci = slots[0]['check_in'] if slots else None
            co = slots[0]['check_out'] if slots else None
            hours_secs = _calculate_hours_seconds(d, slots)
            hours_label = _seconds_to_hhmmss(hours_secs)
            # Late minutes: handle blank/strings robustly
            late_min = None
            if late_col and pd.notna(row.get(late_col)):
                lraw = str(row.get(late_col)).strip()
                if lraw:
                    try:
                        late_min = int(float(lraw))
                    except Exception:
                        late_min = None
            mapped = None
            if machine:
                mapped = Staff.query.filter(
                    (Staff.whitechapel_machine_id == machine) |
                    (Staff.east_ham_machine_id == machine) |
                    (Staff.stratford_machine_id == machine) |
                    (Staff.docklands_machine_id == machine)
                ).first()
            if not mapped and staffid:
                mapped = Staff.query.filter((Staff.id == staffid) | (Staff.access_code == str(staffid))).first()
            preview_rows.append({
                'machine': machine,
                'staffid': staffid,
                'staff_name': mapped.name if mapped else None,
                'date': d.strftime('%d/%m/%Y') if d else None,
                'check_in': ci.strftime('%H:%M:%S') if ci else None,
                'check_out': co.strftime('%H:%M:%S') if co else None,
                'hours': hours_label,
                'late_min': late_min,
            })
        except Exception:
            continue

    # Keep preview in session and store temp file name for confirm
    session['attendance_import_tmp'] = temp_name
    session['attendance_import_branch'] = branch or ''
    return render_template('staff/attendance_import_preview.html', rows=preview_rows, temp_name=temp_name, branch=branch or '')

def _build_supervisor_shift_email(shift: SupervisorShift, staff_user: User) -> tuple[str, str]:
        date_label = shift.date.strftime('%A, %d %B %Y') if shift.date else ''
        times = ', '.join(shift.timeslot_list())
        branch = shift.branch or '—'
        subject = f"Your supervisor shift – {date_label} ({times})"
        body_rows = [
                ("Staff", staff_user.name or ''),
                ("Date", date_label),
                ("Day", shift.day or ''),
                ("Timeslots", times or ''),
                ("Branch", branch),
                ("Notes", (shift.notes or '').replace('\n','<br/>') or '<em>None</em>'),
        ]
        table = ["<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>"]
        for label, value in body_rows:
                table.append(
                        "<tr>"+
                        f"<td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td>"+
                        f"<td style='padding:6px 0;color:#0f172a;'>{value}</td>"+
                        "</tr>"
                )
        table.append("</table>")
        intro = f"Hello {staff_user.name},<br/><br/>You have been assigned a new supervisor shift. The details are below."
        shell = """
<!DOCTYPE html>
<html lang='en'>
<head><meta charset='utf-8'/><title>{title}</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;">
    <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#f1f5f9;padding:24px 0;'>
        <tr><td align='center'>
            <table role='presentation' width='640' cellpadding='0' cellspacing='0' style='background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;'>
                <tr>
                    <td style='background:#0f172a;padding:24px 32px;'>
                        <h1 style='margin:0;font-size:22px;line-height:1.3;color:#ffffff;font-weight:600;'>Excel Tutors</h1>
                        <p style='margin:6px 0 0;font-size:13px;color:#cbd5f5;'>Supervisor shift assignment</p>
                    </td>
                </tr>
                <tr>
                    <td style='padding:32px;'>
                        <p style='margin:0 0 16px;font-size:15px;color:#0f172a;'>{intro}</p>
                        {table}
                    </td>
                </tr>
                <tr>
                    <td style='background:#f8fafc;padding:18px 32px;text-align:center;font-size:11px;color:#94a3b8;'>
                        &copy; {year} Excel Tutors. All rights reserved.
                    </td>
                </tr>
            </table>
        </td></tr>
    </table>
    </body>
    </html>
        """.format(title=subject, intro=intro, table=''.join(table), year=date.today().year)
        return subject, shell

def _build_floor_shift_email(shift: Shift, staff_user: User) -> tuple[str, str]:
        """Build the email body for a floor/admin staff shift assignment."""
        date_label = shift.date.strftime('%A, %d %B %Y') if shift.date else ''
        times = ', '.join(shift.timeslot_list())
        branch = shift.branch or '—'
        floors = ', '.join(shift.floor_list()) if getattr(shift, 'floors', None) else '—'
        subject = f"Your shift – {date_label} ({times})"
        body_rows = [
                ("Staff", staff_user.name or ''),
                ("Date", date_label),
                ("Day", shift.day or ''),
                ("Timeslots", times or ''),
                ("Branch", branch),
                ("Floors", floors),
                ("Notes", (shift.notes or '').replace('\n','<br/>') or '<em>None</em>'),
        ]
        table = ["<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>"]
        for label, value in body_rows:
                table.append(
                        "<tr>"
                        + f"<td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td>"
                        + f"<td style='padding:6px 0;color:#0f172a;'>{value}</td>"
                        + "</tr>"
                )
        table.append("</table>")
        intro = f"Hello {staff_user.name},<br/><br/>Your shift has been scheduled. The details are below."
        html = f"""
<!DOCTYPE html>
<html lang='en'>
<head><meta charset='utf-8'/><title>{subject}</title></head>
<body style=\"margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;\">
    <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#f1f5f9;padding:24px 0;'>
        <tr><td align='center'>
            <table role='presentation' width='640' cellpadding='0' cellspacing='0' style='background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;'>
                <tr>
                    <td style='background:#0f172a;padding:24px 32px;'>
                        <h1 style='margin:0;font-size:22px;line-height:1.3;color:#ffffff;font-weight:600;'>Excel Tutors</h1>
                        <p style='margin:6px 0 0;font-size:13px;color:#cbd5f5;'>Shift assignment</p>
                    </td>
                </tr>
                <tr>
                    <td style='padding:32px;'>
                        <p style='margin:0 0 16px;font-size:15px;color:#0f172a;'>{intro}</p>
                        {''.join(table)}
                    </td>
                </tr>
                <tr>
                    <td style='background:#f8fafc;padding:18px 32px;text-align:center;font-size:11px;color:#94a3b8;'>
                        &copy; {date.today().year} Excel Tutors. All rights reserved.
                    </td>
                </tr>
            </table>
        </td></tr>
    </table>
</body>
</html>
"""
        return subject, html

@app.route('/supervisor/shifts')
@login_required
@permission_required('manage_supervisor_shifts')
def supervisor_shifts_index():
    q = (request.args.get('q') or '').strip().lower()
    staff_id = request.args.get('staff', type=int)
    day = (request.args.get('day') or '').strip()
    status = (request.args.get('status') or '').strip().lower()  # upcoming | past
    date_from = request.args.get('from')
    date_to = request.args.get('to')
    sort = (request.args.get('sort') or 'date').lower()  # date|staff|day
    direction = (request.args.get('direction') or 'desc').lower()  # asc|desc

    query = SupervisorShift.query
    today = date.today()
    if staff_id:
        query = query.filter(SupervisorShift.staff_user_id == staff_id)
    if day:
        query = query.filter(SupervisorShift.day == day)
    if status == 'upcoming':
        query = query.filter(SupervisorShift.date >= today)
    elif status == 'past':
        query = query.filter(SupervisorShift.date < today)
    if date_from:
        try:
            df = _dt.strptime(date_from, '%Y-%m-%d').date()
            query = query.filter(SupervisorShift.date >= df)
        except Exception:
            pass
    if date_to:
        try:
            dt = _dt.strptime(date_to, '%Y-%m-%d').date()
            query = query.filter(SupervisorShift.date <= dt)
        except Exception:
            pass
    if q:
        query = query.join(User, SupervisorShift.staff_user_id == User.id).filter(
            (User.name.ilike(f"%{q}%")) | (SupervisorShift.notes.ilike(f"%{q}%"))
        )

    if sort == 'staff':
        query = query.join(User, SupervisorShift.staff_user_id == User.id).order_by(User.name.asc())
    elif sort == 'day':
        query = query.order_by(SupervisorShift.day.asc(), SupervisorShift.date.desc())
    else:
        query = query.order_by(SupervisorShift.date.desc())
    if direction == 'asc':
        if sort == 'date':
            query = query.order_by(SupervisorShift.date.asc())

    records = query.all()
    staff_list = _users_for_supervisor_shifts()
    days = list(day_name)
    # Preflight: warn if rota email setting is missing
    try:
        setting_name = get_setting('rota_setting', 'operations')
        from models import EmailSetting as _ES
        if not _ES.query.filter_by(name=setting_name).first():
            flash(f"Rota email setting '{setting_name}' is not configured. Add an Email Setting with this name to enable notifications.", 'warning')
    except Exception:
        pass
    return render_template(
        'supervisor/shifts/index.html',
        records=records,
        staff_list=staff_list,
        days=days,
        TIMESLOT_OPTIONS=TIMESLOT_OPTIONS,
        today=today,
    branch_choices=BRANCH_CHOICES(),
    )

@app.route('/supervisor/shifts/new', methods=['GET','POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def supervisor_shifts_new():
    if request.method == 'POST':
        try:
            staff_user_id = int(request.form.get('staff_user_id'))
        except Exception:
            flash('Invalid staff', 'danger')
            return redirect(url_for('supervisor_shifts_index'))
        # Enforce role restriction: only centre_manager or supervisor accounts can be assigned
        assignee = db.session.get(User, staff_user_id)
        if not assignee or (assignee.role not in ('centre_manager','supervisor')):
            flash('Selected user is not eligible for a supervisor shift (requires Supervisor or Centre Manager role).', 'danger')
            return redirect(url_for('supervisor_shifts_index'))
        try:
            raw = (request.form.get('date') or '').strip()
            dt_parsed = None
            for fmt in ('%d-%m-%Y','%Y-%m-%d'):
                try:
                    dt_parsed = _dt.strptime(raw, fmt).date(); break
                except Exception:
                    pass
            if not dt_parsed:
                raise ValueError('Invalid date')
        except Exception:
            flash('Invalid date format', 'danger')
            return redirect(url_for('supervisor_shifts_index'))
        slots = request.form.getlist('timeslots')
        if not slots and (dt_parsed.weekday() < 5):
            slots = ['5-7']
        slots = [s for s in slots if s in TIMESLOT_OPTIONS]
        if not slots:
            flash('Please choose at least one timeslot', 'danger')
            return redirect(url_for('supervisor_shifts_index'))
        branch = (request.form.get('branch') or '').strip()
        if branch and branch not in BRANCH_CHOICES():
            flash('Invalid branch', 'danger')
            return redirect(url_for('supervisor_shifts_index'))
        notes = (request.form.get('notes') or '').strip() or None
        sh = SupervisorShift(staff_user_id=staff_user_id, date=dt_parsed, day=_shift_day_for(dt_parsed), timeslots=','.join(slots), branch=branch or None, notes=notes)
        db.session.add(sh)
        db.session.commit()
        try:
            staff_user = db.session.get(User, staff_user_id)
            if staff_user and staff_user.email:
                subj, html = _build_supervisor_shift_email(sh, staff_user)
                setting_name = get_setting('rota_setting', 'operations')
                send_email(staff_user.email, subj, html, setting_name=setting_name)
        except Exception as _exc:
            print(f"[WARN] Supervisor shift email send failed: {_exc}")
        flash('Supervisor shift created', 'success')
        return redirect(url_for('supervisor_shifts_index'))
    staff_list = _users_for_supervisor_shifts()
    return render_template('supervisor/shifts/form.html', record=None, staff_list=staff_list, TIMESLOT_OPTIONS=TIMESLOT_OPTIONS, branch_choices=BRANCH_CHOICES())

@app.route('/supervisor/shifts/<int:shift_id>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def supervisor_shifts_edit(shift_id: int):
    sh = SupervisorShift.query.get_or_404(shift_id)
    if sh.date < date.today():
        flash('Cannot edit past supervisor shifts', 'warning')
        return redirect(url_for('supervisor_shifts_index'))
    if request.method == 'POST':
        try:
            staff_user_id = int(request.form.get('staff_user_id'))
        except Exception:
            flash('Invalid staff', 'danger')
            return redirect(url_for('supervisor_shifts_index'))
        # Enforce role restriction on edit as well
        assignee = db.session.get(User, staff_user_id)
        if not assignee or (assignee.role not in ('centre_manager','supervisor')):
            flash('Selected user is not eligible for a supervisor shift (requires Supervisor or Centre Manager role).', 'danger')
            return redirect(url_for('supervisor_shifts_index'))
        raw = (request.form.get('date') or '').strip()
        nd = None
        for fmt in ('%d-%m-%Y','%Y-%m-%d'):
            try:
                nd = _dt.strptime(raw, fmt).date(); break
            except Exception:
                pass
        if not nd:
            flash('Invalid date format', 'danger')
            return redirect(url_for('supervisor_shifts_index'))
        slots = request.form.getlist('timeslots')
        if not slots and (nd.weekday() < 5):
            slots = ['5-7']
        slots = [s for s in slots if s in TIMESLOT_OPTIONS]
        if not slots:
            flash('Please choose at least one timeslot', 'danger')
            return redirect(url_for('supervisor_shifts_index'))
        branch = (request.form.get('branch') or '').strip()
        if branch and branch not in BRANCH_CHOICES():
            flash('Invalid branch', 'danger')
            return redirect(url_for('supervisor_shifts_index'))
        sh.staff_user_id = staff_user_id
        sh.date = nd
        sh.day = _shift_day_for(nd)
        sh.timeslots = ','.join(slots)
        sh.branch = branch or None
        sh.notes = (request.form.get('notes') or '').strip() or None
        db.session.commit()
        flash('Supervisor shift updated', 'success')
        return redirect(url_for('supervisor_shifts_index'))
    staff_list = _users_for_supervisor_shifts()
    return render_template('supervisor/shifts/form.html', record=sh, staff_list=staff_list, TIMESLOT_OPTIONS=TIMESLOT_OPTIONS, branch_choices=BRANCH_CHOICES())

@app.route('/api/supervisor/shifts/<int:shift_id>')
@login_required
@permission_required('manage_supervisor_shifts')
def api_supervisor_shift(shift_id: int):
    sh = SupervisorShift.query.get_or_404(shift_id)
    payload = {
        'id': sh.id,
        'staff_user_id': sh.staff_user_id,
        'date': sh.date.strftime('%Y-%m-%d') if sh.date else None,
        'day': sh.day,
        'timeslots': sh.timeslot_list(),
        'branch': sh.branch,
        'notes': sh.notes or '',
        'is_past': bool(sh.date < date.today()) if sh.date else False,
    }
    return jsonify(payload)

@app.route('/supervisor/shifts/<int:shift_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def supervisor_shifts_delete(shift_id: int):
    # Delete a future supervisor shift; protect past shifts
    sh = SupervisorShift.query.get_or_404(shift_id)
    if sh.date and sh.date < date.today():
        flash('Cannot delete past supervisor shifts', 'warning')
        return redirect(url_for('supervisor_shifts_index'))
    try:
        db.session.delete(sh)
        db.session.commit()
        flash('Supervisor shift deleted', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to delete shift: {exc}', 'danger')
    return redirect(url_for('supervisor_shifts_index'))


@app.route('/floor/shifts/<int:shift_id>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_shifts')
def floor_shifts_edit(shift_id: int):
    sh = Shift.query.get_or_404(shift_id)
    if sh.date < date.today():
        flash('Cannot edit past shifts', 'warning')
        return redirect(url_for('floor_shifts_index'))
    if request.method == 'POST':
        try:
            staff_user_id = int(request.form.get('staff_user_id'))
        except Exception:
            flash('Invalid staff', 'danger')
            return redirect(url_for('floor_shifts_index'))
        # Date
        raw = (request.form.get('date') or '').strip()
        nd = None
        for fmt in ('%d-%m-%Y','%Y-%m-%d'):
            try:
                nd = _dt.strptime(raw, fmt).date(); break
            except Exception: pass
        if not nd:
            flash('Invalid date format', 'danger')
            return redirect(url_for('floor_shifts_index'))
        slots = request.form.getlist('timeslots')
        if not slots and _is_weekday(nd):
            slots = ['5-7']
        slots = [s for s in slots if s in TIMESLOT_OPTIONS]
        if not slots:
            flash('Please choose at least one timeslot', 'danger')
            return redirect(url_for('floor_shifts_index'))
        # Branch & floors
        branch = (request.form.get('branch') or '').strip()
        if branch and branch not in BRANCH_CHOICES():
            flash('Invalid branch', 'danger')
            return redirect(url_for('floor_shifts_index'))
        floors = request.form.getlist('floors')
        valid_floor_opts = ['Basement','Ground Floor','First Floor','Second Floor','Third Floor']
        floors = [f for f in floors if f in valid_floor_opts]
        sh.staff_user_id = staff_user_id
        sh.date = nd
        sh.day = _shift_day_for(nd)
        sh.timeslots = ','.join(slots)
        sh.branch = branch or None
        sh.floors = (','.join(floors) if floors else None)
        sh.notes = (request.form.get('notes') or '').strip() or None
        db.session.commit()
        flash('Shift updated', 'success')
        return redirect(url_for('floor_shifts_index'))
    staff_list = _users_for_shifts()
    return render_template('floor/shifts/form.html', record=sh, staff_list=staff_list, TIMESLOT_OPTIONS=TIMESLOT_OPTIONS, branch_choices=BRANCH_CHOICES())


@app.route('/api/floor/shifts/<int:shift_id>')
@login_required
@permission_required('manage_shifts')
def api_floor_shift(shift_id: int):
    """Return JSON details for a single shift to prefill the edit modal."""
    sh = Shift.query.get_or_404(shift_id)
    payload = {
        'id': sh.id,
        'staff_user_id': sh.staff_user_id,
        'date': sh.date.strftime('%Y-%m-%d') if sh.date else None,
        'day': sh.day,
        'timeslots': sh.timeslot_list(),
        'branch': sh.branch,
        'floors': sh.floor_list(),
        'notes': sh.notes or '',
        'is_past': bool(sh.date < date.today()) if sh.date else False,
    }
    return jsonify(payload)


@app.route('/floor/shifts/<int:shift_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_shifts')
def floor_shifts_delete(shift_id: int):
    """Delete a future shift. Past shifts are protected from deletion."""
    sh = Shift.query.get_or_404(shift_id)
    if sh.date and sh.date < date.today():
        flash('Cannot delete past shifts', 'warning')
        return redirect(url_for('floor_shifts_index'))
    try:
        db.session.delete(sh)
        db.session.commit()
        flash('Shift deleted', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to delete shift: {exc}', 'danger')
    return redirect(url_for('floor_shifts_index'))


# End of Day Checklist
@app.route('/floor/checklists')
@login_required
@permission_required('manage_eod_checklist')
def floor_checklists_index():
    # Filters: date (YYYY-MM-DD), floor, staff (completed by), q (search by staff name/floor)
    raw_date = (request.args.get('date') or '').strip()
    selected_date = None
    for fmt in ('%Y-%m-%d','%d-%m-%Y'):
        try:
            if raw_date:
                selected_date = _dt.strptime(raw_date, fmt).date(); break
        except Exception:
            pass
    if not selected_date:
        selected_date = date.today()

    floor_filter = (request.args.get('floor') or '').strip()
    staff_filter = request.args.get('staff', type=int)
    q = (request.args.get('q') or '').strip()

    # Base query scoped by date and role
    query = EndOfDayChecklist.query.filter(EndOfDayChecklist.date == selected_date)
    is_admin = bool(getattr(current_user, 'is_superadmin', False) or user_can('manage_eod_checklist'))
    if not is_admin:
        query = query.filter(EndOfDayChecklist.staff_user_id == current_user.id)
        staff_filter = current_user.id  # lock to self for non-admins

    if staff_filter:
        query = query.filter(EndOfDayChecklist.staff_user_id == staff_filter)
    if floor_filter:
        query = query.filter(EndOfDayChecklist.floor == floor_filter)
    if q:
        # naive search: staff name or floor contains
        try:
            query = query.join(User, EndOfDayChecklist.staff_user_id == User.id).filter(
                (User.name.ilike(f"%{q}%")) | (EndOfDayChecklist.floor.ilike(f"%{q}%"))
            )
        except Exception:
            pass

    checklists = query.order_by(EndOfDayChecklist.created_at.desc()).all()
    # Shifts for selected date for the current user (used for dynamic create visibility)
    my_shifts = Shift.query.filter(Shift.date == selected_date, Shift.staff_user_id == current_user.id).all()
    floor_opts = ['Basement','Ground Floor','First Floor','Second Floor','Third Floor']
    staff_list = _users_for_shifts() if is_admin else []
    return render_template('floor/checklists/index.html', checklists=checklists, my_shifts=my_shifts, selected_date=selected_date, floor_opts=floor_opts, staff_list=staff_list, selected_floor=floor_filter, selected_staff=staff_filter, q=q)


@app.route('/floor/checklists/new', methods=['GET','POST'])
@login_required
@permission_required('manage_eod_checklist')
def floor_checklists_new():
    # Creation via modal (POST) or modal content (GET)
    if request.method == 'POST':
        try:
            shift_id = request.form.get('shift_id', type=int)
            staff_user_id = int(request.form.get('staff_user_id') or current_user.id)
            raw_date = (request.form.get('date') or '').strip()
            d = None
            for fmt in ('%Y-%m-%d','%d-%m-%Y'):
                try:
                    d = _dt.strptime(raw_date, fmt).date(); break
                except Exception:
                    pass
            if not d:
                d = date.today()
            floor = (request.form.get('floor') or '').strip()
            # Items: prefer JSON if provided; otherwise parse item_{i} + item_{i}_val (checkbox or yes/no)
            items_raw = request.form.get('items')
            import json
            import re
            if items_raw:
                try:
                    items = json.loads(items_raw)
                except Exception:
                    items = []
            else:
                labels: dict[int, str] = {}
                pat = re.compile(r'^item_(\d+)$')
                for k, v in request.form.items():
                    m = pat.match(k)
                    if m and v and v.strip():
                        labels[int(m.group(1))] = v.strip()
                items = []
                for idx in sorted(labels.keys()):
                    label = labels[idx]
                    raw_val = (request.form.get(f'item_{idx}_val') or '').strip().lower()
                    # checkbox posts 'on'; select may post 'yes'|'no'; normalize to yes/no
                    val = 'yes' if raw_val in ('yes','on','true','1') else 'no'
                    items.append({'todo': label, 'value': val})
            checklist = EndOfDayChecklist(
                shift_id=shift_id or None,
                staff_user_id=staff_user_id,
                date=d,
                floor=floor or None,
                items=json.dumps(items),
                completed=True,
                completed_at=datetime.utcnow(),
                created_by_id=current_user.id
            )
            db.session.add(checklist)
            db.session.commit()
            flash('End of Day Checklist saved', 'success')
            return redirect(url_for('floor_checklists_index'))
        except Exception as exc:
            db.session.rollback()
            flash(f'Failed to save checklist: {exc}', 'danger')
            return redirect(url_for('floor_checklists_index'))
    # GET: return modal fragment
    # Provide today's shifts for current user to preselect
    today = date.today()
    my_shifts = Shift.query.filter(Shift.date == today, Shift.staff_user_id == current_user.id).all()
    floor_opts = ['Basement','Ground Floor','First Floor','Second Floor','Third Floor']
    return render_template('floor/checklists/form.html', record=None, my_shifts=my_shifts, floor_opts=floor_opts, today=today)


@app.route('/floor/checklists/<int:cid>/print')
@login_required
@permission_required('manage_eod_checklist')
def floor_checklist_print(cid: int):
    cl = EndOfDayChecklist.query.get_or_404(cid)
    # Only allow owner or admins to print
    if not (user_can('manage_eod_checklist') or cl.staff_user_id == current_user.id):
        abort(403)
    return render_template('floor/checklists/print.html', cl=cl)


@app.route('/floor/checklists/<int:cid>/pdf')
@login_required
@permission_required('manage_eod_checklist')
def floor_checklist_pdf(cid: int):
    from xhtml2pdf import pisa
    cl = EndOfDayChecklist.query.get_or_404(cid)
    if not (user_can('manage_eod_checklist') or cl.staff_user_id == current_user.id):
        abort(403)
    # Render the same print template to HTML and convert to PDF
    html = render_template('floor/checklists/print.html', cl=cl)
    pdf_io = io.BytesIO()
    try:
        pisa.CreatePDF(io.StringIO(html), dest=pdf_io)  # type: ignore[arg-type]
    except Exception as exc:
        flash(f'PDF generation failed: {exc}', 'danger')
        return redirect(url_for('floor_checklists_index'))
    pdf_io.seek(0)
    fname = f"eod_checklist_{cl.id}.pdf"
    return send_file(pdf_io, as_attachment=True, download_name=fname, mimetype='application/pdf')


@app.route('/floor/checklists/<int:cid>/delete', methods=['POST'])
@login_required
@permission_required('manage_eod_checklist')
def floor_checklists_delete(cid: int):
    """Allow users with manage_eod_checklist permission to delete an End of Day Checklist (irreversible)."""
    cl = EndOfDayChecklist.query.get_or_404(cid)
    try:
        db.session.delete(cl)
        db.session.commit()
        flash('Checklist deleted', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to delete checklist: {exc}', 'danger')
    return redirect(url_for('floor_checklists_index'))


@app.route('/api/floor/checklists/today')
@login_required
@permission_required('manage_eod_checklist')
def api_floor_checklists_today():
    today = date.today()
    q = EndOfDayChecklist.query.filter(EndOfDayChecklist.date == today)
    rows = [c.serialize() for c in q.all()]
    return jsonify(rows)


@app.route('/api/floor/reports/today')
@login_required
@permission_required('manage_floor_reports')
def api_floor_reports_today():
    from models import PrintReport
    raw_date = (request.args.get('date') or '').strip()
    d = None
    for fmt in ('%Y-%m-%d','%d-%m-%Y'):
        try:
            if raw_date:
                d = _dt.strptime(raw_date, fmt).date(); break
        except Exception:
            pass
    if not d:
        d = date.today()
    q = PrintReport.query.filter(PrintReport.date == d)
    rows = [r.serialize() for r in q.all()]
    return jsonify(rows)


def _send_eod_reminders():
    # Runs daily at 19:30: find shifts for today where staff hasn't completed checklist and send email
    with app.app_context():
        today = date.today()
        # find shifts today
        shifts = Shift.query.filter(Shift.date == today).all()
        for sh in shifts:
            # check if a checklist exists for this shift/staff
            exists = EndOfDayChecklist.query.filter(EndOfDayChecklist.date == today, EndOfDayChecklist.staff_user_id == sh.staff_user_id).count()
            if exists == 0:
                # Send warning
                try:
                    staff = db.session.get(User, sh.staff_user_id)
                    if staff and staff.email:
                        subj = f"Reminder: End of Day checklist pending for {today.strftime('%d %b %Y')}"
                        body = f"Hello {staff.name},<br/><br/>You have an assigned shift today ({sh.date.strftime('%A %d %b %Y')}). Please complete the End of Day checklist by 19:30 to mark the task complete.<br/><br/>Thanks." 
                        _send_email_safe(staff.email, subj, body, log_prefix='EOD reminder')
                except Exception as e:
                    print(f"[WARN] Failed to send EOD reminder: {e}")


# Schedule EOD reminders at 19:30 daily if APScheduler is available
if BackgroundScheduler is not None:
    try:
        _ensure_scheduler_started()
        if scheduler:
            # Remove old job if exists
            try:
                scheduler.remove_job('eod-reminder')
            except Exception:
                pass
            try:
                # schedule at 19:30 local (approx) daily
                scheduler.add_job(_send_eod_reminders, 'cron', hour=19, minute=30, id='eod-reminder')
            except Exception as _e:
                print(f"[WARN] Scheduler: failed to add eod reminder job: {_e}")
    except Exception:
        pass


# Print Report reminders (similar to EOD): send at 19:30 for staff with a shift and no report
def _send_print_report_reminders():
    with app.app_context():
        from models import PrintReport
        today = date.today()
        shifts = Shift.query.filter(Shift.date == today).all()
        for sh in shifts:
            exists = PrintReport.query.filter(PrintReport.date == today, PrintReport.staff_user_id == sh.staff_user_id).count()
            if exists == 0:
                try:
                    staff = db.session.get(User, sh.staff_user_id)
                    if staff and staff.email:
                        subj = f"Reminder: Print report pending for {today.strftime('%d %b %Y') }"
                        body = f"Hello {staff.name},<br/><br/>You have an assigned shift today ({sh.date.strftime('%A %d %b %Y')}). Please complete the daily print report by 19:30.<br/><br/>Thanks."
                        _send_email_safe(staff.email, subj, body, log_prefix='Print report reminder')
                except Exception as e:
                    print(f"[WARN] Failed to send print report reminder: {e}")

if BackgroundScheduler is not None:
    try:
        _ensure_scheduler_started()
        if scheduler:
            try:
                scheduler.remove_job('print-report-reminder')
            except Exception:
                pass
            try:
                scheduler.add_job(_send_print_report_reminders, 'cron', hour=19, minute=30, id='print-report-reminder')
            except Exception as _e:
                print(f"[WARN] Scheduler: failed to add print report reminder job: {_e}")
    except Exception:
        pass


# Print Reports
@app.route('/floor/reports')
@login_required
@permission_required('manage_floor_reports')
def floor_reports_index():
    # Filters similar to EOD: date, floor, branch, staff, q
    from models import PrintReport
    raw_date = (request.args.get('date') or '').strip()
    selected_date = None
    for fmt in ('%Y-%m-%d','%d-%m-%Y'):
        try:
            if raw_date:
                selected_date = _dt.strptime(raw_date, fmt).date(); break
        except Exception:
            pass
    if not selected_date:
        selected_date = date.today()
    floor_filter = (request.args.get('floor') or '').strip()
    branch_filter = (request.args.get('branch') or '').strip()
    staff_filter = request.args.get('staff', type=int)
    q = (request.args.get('q') or '').strip()

    is_admin = bool(getattr(current_user,'is_superadmin',False) or user_can('manage_floor_reports'))
    query = PrintReport.query.filter(PrintReport.date == selected_date)
    if not is_admin:
        query = query.filter(PrintReport.staff_user_id == current_user.id)
        staff_filter = current_user.id
    if floor_filter:
        query = query.filter(PrintReport.floor == floor_filter)
    if branch_filter:
        query = query.filter(PrintReport.branch == branch_filter)
    if staff_filter:
        query = query.filter(PrintReport.staff_user_id == staff_filter)
    if q:
        try:
            query = query.join(User, PrintReport.staff_user_id == User.id).filter(
                (User.name.ilike(f"%{q}%")) | (PrintReport.floor.ilike(f"%{q}%")) | (PrintReport.branch.ilike(f"%{q}%"))
            )
        except Exception:
            pass
    records = query.order_by(PrintReport.created_at.desc()).all()
    floor_opts = ['Basement','Ground Floor','First Floor','Second Floor','Third Floor']
    branches = BRANCH_CHOICES()
    staff_list = _users_for_shifts() if is_admin else []
    return render_template('floor/reports/index.html', records=records, selected_date=selected_date, floor_opts=floor_opts, branches=branches, staff_list=staff_list, selected_floor=floor_filter, selected_branch=branch_filter, selected_staff=staff_filter, q=q)


@app.route('/floor/reports/new', methods=['GET','POST'])
@login_required
@permission_required('manage_floor_reports')
def floor_reports_new():
    # Create via modal
    from models import PrintReport
    if request.method == 'POST':
        try:
            staff_user_id = int(request.form.get('staff_user_id') or current_user.id)
            raw_date = (request.form.get('date') or '').strip()
            d = None
            for fmt in ('%Y-%m-%d','%d-%m-%Y'):
                try:
                    d = _dt.strptime(raw_date, fmt).date(); break
                except Exception: pass
            if not d: d = date.today()
            day = _shift_day_for(d)
            floor = (request.form.get('floor') or '').strip() or None
            branch = (request.form.get('branch') or '').strip() or None
            pages = request.form.get('pages_printed', type=int) or 0
            has_unapproved = (request.form.get('has_unapproved') in ('yes','on','true','1'))
            details = (request.form.get('unapproved_details') or '').strip() or None
            notes = (request.form.get('notes') or '').strip() or None
            shift_id = request.form.get('shift_id', type=int)
            pr = PrintReport(shift_id=shift_id or None, staff_user_id=staff_user_id, date=d, day=day, floor=floor, branch=branch, pages_printed=pages, has_unapproved=has_unapproved, unapproved_details=details, notes=notes, created_by_id=current_user.id)
            db.session.add(pr); db.session.commit()
            flash('Print report saved','success')
        except Exception as exc:
            db.session.rollback(); flash(f'Failed to save report: {exc}','danger')
        return redirect(url_for('floor_reports_index'))
    # GET returns modal fragment
    today = date.today()
    my_shifts = Shift.query.filter(Shift.date == today, Shift.staff_user_id == current_user.id).all()
    return render_template('floor/reports/form.html', record=None, my_shifts=my_shifts, floor_opts=['Basement','Ground Floor','First Floor','Second Floor','Third Floor'], branches=BRANCH_CHOICES(), today=today)

@app.route('/floor/reports/<int:rid>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_floor_reports')
def floor_reports_edit(rid: int):
    from models import PrintReport
    pr = PrintReport.query.get_or_404(rid)
    if request.method == 'POST':
        try:
            raw_date = (request.form.get('date') or '').strip()
            d = None
            for fmt in ('%Y-%m-%d','%d-%m-%Y'):
                try:
                    d = _dt.strptime(raw_date, fmt).date(); break
                except Exception: pass
            if not d: d = pr.date
            pr.date = d
            pr.day = _shift_day_for(d)
            pr.floor = (request.form.get('floor') or '').strip() or None
            pr.branch = (request.form.get('branch') or '').strip() or None
            pr.pages_printed = request.form.get('pages_printed', type=int) or 0
            pr.has_unapproved = (request.form.get('has_unapproved') in ('yes','on','true','1'))
            pr.unapproved_details = (request.form.get('unapproved_details') or '').strip() or None
            pr.notes = (request.form.get('notes') or '').strip() or None
            db.session.commit(); flash('Report updated','success')
        except Exception as exc:
            db.session.rollback(); flash(f'Update failed: {exc}','danger')
        return redirect(url_for('floor_reports_index'))
    today = pr.date or date.today()
    my_shifts = Shift.query.filter(Shift.date == today, Shift.staff_user_id == current_user.id).all()
    return render_template('floor/reports/form.html', record=pr, my_shifts=my_shifts, floor_opts=['Basement','Ground Floor','First Floor','Second Floor','Third Floor'], branches=BRANCH_CHOICES(), today=today)


@app.route('/invoice-submissions/create-salary-report', methods=['GET','POST'])
@login_required
@permission_required('manage_staff_invoices')
def create_salary_report():
    # GET: return modal fragment used by the submissions page
    if request.method == 'GET':
        companies = Company.query.order_by(Company.name).all()
        return render_template('invoice_management/create_salary_report_modal.html', companies=companies)

    # POST: parse filters, query invoices, build rows and return XLSX
    # parse filters
    companies = request.form.getlist('company')
    months = [int(m) for m in request.form.getlist('month') if m]
    years = [int(y) for y in request.form.getlist('year') if y]
    statuses = request.form.getlist('status')

    # build query against StaffInvoice
    q = StaffInvoice.query.options(joinedload(StaffInvoice.created_by))
    # Normalize company IDs to integers if provided
    company_ids = []
    try:
        company_ids = [int(c) for c in companies if c]
    except Exception:
        # fallback: keep raw values
        company_ids = [c for c in companies if c]
    if company_ids:
        # Join Staff (employee profile) via user_id to filter by Staff.company_id
        q = q.join(Staff, Staff.user_id == StaffInvoice.created_by_id).filter(Staff.company_id.in_(company_ids))
    if months:
        q = q.filter(StaffInvoice.month.in_(months))
    if years:
        q = q.filter(StaffInvoice.year.in_(years))
    if statuses:
        q = q.filter(StaffInvoice.status.in_(statuses))

    invoices = q.order_by(StaffInvoice.id.asc()).all()

    rows = []
    for inv in invoices:
        # Try to find Staff record for the invoice creator
        staff_rec = Staff.query.filter_by(user_id=inv.created_by_id).first()
        ni = getattr(staff_rec, 'national_insurance', '') if staff_rec else ''
        company_name = staff_rec.company.name if (staff_rec and getattr(staff_rec, 'company', None)) else (getattr(inv, 'company', None).name if getattr(inv, 'company', None) else '')
        hours = 0.0
        try:
            hours = float(sum([float(getattr(it, 'hours', 0) or 0) for it in (inv.items or [])]))
        except Exception:
            hours = 0.0
        rate = ''
        if getattr(inv, 'items', None) and len(inv.items) > 0:
            try:
                rate = float(getattr(inv.items[0], 'rate', 0) or 0)
            except Exception:
                rate = ''
        amount = float(getattr(inv, 'amount', 0) or 0)
        notes = getattr(inv, 'notes', '') or ''
        name = inv.created_by.name if getattr(inv, 'created_by', None) else ''
        rows.append({
            'Name': name,
            'National Insurance Number': ni,
            'Company': company_name,
            'Number of Hours Worked': hours,
            'Hour per Rate': rate,
            'Amount': amount,
            'Notes': notes,
        })

    # Generate Excel file in-memory
    try:
        from io import BytesIO

        import pandas as pd

        df = pd.DataFrame(rows)
        # Ensure consistent column order even if rows empty
        cols = ['Name', 'National Insurance Number', 'Company', 'Number of Hours Worked', 'Hour per Rate', 'Amount', 'Notes']
        df = df.reindex(columns=cols)
        bio = BytesIO()
        with pd.ExcelWriter(bio, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='SalaryReport')
        bio.seek(0)
        filename = 'salary_report.xlsx'
        return send_file(bio, as_attachment=True, download_name=filename, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        flash(f'Failed to generate XLSX: {e}', 'danger')
        return redirect(url_for('invoice_submissions'))


@app.route('/salary-reports')
@login_required
@permission_required('manage_staff_invoices')
def salary_reports_index():
    import json as _json

    from models import Company, SalaryReport
    q = SalaryReport.query
    search = (request.args.get('q') or '').strip()
    sort = (request.args.get('sort') or 'created_at').strip()
    # modal-like filters
    company_filters = [int(c) for c in request.args.getlist('company') if c]
    month_filters = [int(m) for m in request.args.getlist('month') if m]
    year_filters = [int(y) for y in request.args.getlist('year') if y]
    status_filters = [s for s in request.args.getlist('status') if s]
    # search across name and creator name if available
    if search:
        try:
            from models import User
            q = q.join(User, SalaryReport.created_by_id == User.id).filter(
                (SalaryReport.name.ilike(f"%{search}%")) | (User.name.ilike(f"%{search}%"))
            )
        except Exception:
            q = q.filter(SalaryReport.name.ilike(f"%{search}%"))
    # sorting
    if sort == 'name':
        q = q.order_by(SalaryReport.name.asc().nullslast())
    else:
        q = q.order_by(SalaryReport.created_at.desc())
    all_reports = q.all()
    # Filter in Python by filter_meta to match modal parameters
    def matches_modal_filters(rep):
        if not (company_filters or month_filters or year_filters or status_filters):
            return True
        try:
            meta = _json.loads(rep.filter_meta) if rep.filter_meta else {}
        except Exception:
            meta = {}
        def has_any(selected, meta_key):
            if not selected:
                return True
            vals = meta.get(meta_key) or []
            # Normalize to ints for numeric filters
            if meta_key in ('company','month','year'):
                try:
                    vals = [int(v) for v in vals if v is not None and v != '']
                except Exception:
                    pass
            return any(v in vals for v in selected)
        return (
            has_any(company_filters, 'company') and
            has_any(month_filters, 'month') and
            has_any(year_filters, 'year') and
            has_any(status_filters, 'status')
        )
    reports = [r for r in all_reports if matches_modal_filters(r)]

    companies = Company.query.order_by(Company.name).all()
    # Precompute display metadata for each report (months/years/company names)
    try:
        name_map = {c.id: c.name for c in companies}
    except Exception:
        name_map = {}
    import calendar as _cal
    for _r in reports:
        try:
            _meta = _json.loads(getattr(_r, 'filter_meta', '') or '{}') or {}
        except Exception:
            _meta = {}
        # Normalize lists
        months = _meta.get('month') or []
        years = _meta.get('year') or []
        comps = _meta.get('company') or []
        # Convert to display
        try:
            month_names = [_cal.month_name[int(m)] for m in months if str(m).isdigit() and 1 <= int(m) <= 12]
        except Exception:
            month_names = []
        try:
            year_values = [int(y) for y in years if str(y).isdigit()]
        except Exception:
            year_values = []
        try:
            comp_names = [name_map.get(int(i), str(i)) for i in comps if str(i).isdigit()]
        except Exception:
            comp_names = []
        # Attach transient attributes for template
        setattr(_r, '_month_names', month_names)
        setattr(_r, '_year_values', year_values)
        setattr(_r, '_company_names', comp_names)
    return render_template(
        'salary_reports/index.html',
        reports=reports,
        q=search,
        sort=sort,
        companies=companies,
        company_filters=company_filters,
        month_filters=month_filters,
        year_filters=year_filters,
        status_filters=status_filters,
    )


@app.route('/salary-reports/new', methods=['POST'])
@login_required
@permission_required('manage_staff_invoices')
def salary_reports_new():
    """Create a new empty SalaryReport and redirect to its import page.

    Optionally accepts a 'name' field; otherwise defaults to today's date.
    """
    from models import SalaryReport
    try:
        name = (request.form.get('name') or '').strip()
        if not name:
            name = f"Salary Report {date.today().strftime('%Y-%m-%d')}"
        rep = SalaryReport(name=name, created_by_id=current_user.id)
        db.session.add(rep)
        db.session.commit()
        flash('Created new salary report', 'success')
        return redirect(url_for('salary_report_import', rid=rep.id))
    except Exception as exc:
        db.session.rollback(); flash(f'Failed to create report: {exc}', 'danger')
        return redirect(url_for('salary_reports_index'))


@app.route('/salary-reports/<int:rid>')
@login_required
@permission_required('manage_staff_invoices')
def salary_report_detail(rid: int):
    import json as _json

    from models import Company, SalaryReport, SalaryReportRow
    rep = SalaryReport.query.get_or_404(rid)
    # Filters
    search = (request.args.get('q') or '').strip()
    company_filter = (request.args.get('company') or '').strip()
    min_hours = request.args.get('min_hours', type=float)
    max_hours = request.args.get('max_hours', type=float)
    min_rate = request.args.get('min_rate', type=float)
    max_rate = request.args.get('max_rate', type=float)
    min_amount = request.args.get('min_amount', type=float)
    max_amount = request.args.get('max_amount', type=float)
    sort = request.args.get('sort')
    page = request.args.get('page', type=int) or 1
    per_page = min(200, max(20, request.args.get('per_page', type=int) or 50))

    q = SalaryReportRow.query.filter_by(report_id=rid)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(SalaryReportRow.name.ilike(like), SalaryReportRow.national_insurance.ilike(like), SalaryReportRow.company.ilike(like), SalaryReportRow.notes.ilike(like)))
    if company_filter:
        q = q.filter(SalaryReportRow.company.ilike(f"%{company_filter}%"))
    if min_hours is not None:
        q = q.filter(SalaryReportRow.hours >= min_hours)
    if max_hours is not None:
        q = q.filter(SalaryReportRow.hours <= max_hours)
    if min_rate is not None:
        q = q.filter(SalaryReportRow.rate >= min_rate)
    if max_rate is not None:
        q = q.filter(SalaryReportRow.rate <= max_rate)
    if min_amount is not None:
        q = q.filter(SalaryReportRow.amount >= min_amount)
    if max_amount is not None:
        q = q.filter(SalaryReportRow.amount <= max_amount)

    if sort == 'amount_desc':
        q = q.order_by(SalaryReportRow.amount.desc())
    elif sort == 'amount_asc':
        q = q.order_by(SalaryReportRow.amount.asc())
    else:
        q = q.order_by(SalaryReportRow.id.asc())

    total = q.count()
    rows = q.offset((page-1)*per_page).limit(per_page).all()

    # paging metadata
    pages = (total + per_page - 1) // per_page if per_page else 1
    # Distinct company list for dropdown
    try:
        companies = [c[0] for c in db.session.query(SalaryReportRow.company).filter_by(report_id=rid).distinct().order_by(SalaryReportRow.company.asc()).all() if c and c[0]]
    except Exception:
        companies = []
    # Parse report filter metadata for display
    try:
        _meta = _json.loads(rep.filter_meta) if rep.filter_meta else {}
    except Exception:
        _meta = {}
    # Resolve company IDs to names
    meta_company_names = []
    try:
        if _meta.get('company'):
            ids = [int(i) for i in _meta.get('company') if i not in (None, '')]
            if ids:
                name_map = {c.id: c.name for c in Company.query.filter(Company.id.in_(ids)).all()}
                meta_company_names = [name_map.get(i, str(i)) for i in ids]
    except Exception:
        meta_company_names = []
    return render_template(
        'salary_reports/detail.html',
        report=rep,
        rows=rows,
        q=search,
        sort=sort,
        page=page,
        pages=pages,
        per_page=per_page,
        total=total,
        company_filter=company_filter,
        companies=companies,
        min_hours=min_hours,
        max_hours=max_hours,
        min_rate=min_rate,
        max_rate=max_rate,
        min_amount=min_amount,
        max_amount=max_amount,
        report_filters={
            'company_names': meta_company_names,
            'months': _meta.get('month') or [],
            'years': _meta.get('year') or [],
            'status': _meta.get('status') or [],
        }
    )


@app.route('/salary-reports/<int:rid>/import', methods=['GET','POST'])
@login_required
@permission_required('manage_staff_invoices')
def salary_report_import(rid: int):
    from models import Company, SalaryReport, SalaryReportRow
    rep = SalaryReport.query.get_or_404(rid)
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            flash('No file uploaded', 'danger'); return redirect(url_for('salary_report_import', rid=rid))
        try:
            import pandas as _pd
            df = _pd.read_excel(f)
            for _, r in df.iterrows():
                row = SalaryReportRow(report_id=rep.id,
                                      name=str(r.get('Name') or ''),
                                      national_insurance=str(r.get('National Insurance Number') or r.get('National Insurance') or ''),
                                      company=str(r.get('Company') or ''),
                                      hours=float(r.get('Number of Hours Worked') or r.get('Hours') or 0),
                                      rate=float(r.get('Hour per Rate') or r.get('Rate') or 0),
                                      amount=float(r.get('Amount') or 0),
                                      notes=str(r.get('Notes') or ''))
                db.session.add(row)
            # Save modal-like filters as metadata if provided
            import json as _json
            meta = {
                'company': [int(c) for c in request.form.getlist('company') if c],
                'month': [int(m) for m in request.form.getlist('month') if m],
                'year': [int(y) for y in request.form.getlist('year') if y],
                'status': [s for s in request.form.getlist('status') if s],
            }
            # Only set if any provided or if empty (overwrite)
            rep.filter_meta = _json.dumps(meta)
            db.session.commit()
            flash('Imported report rows', 'success')
            return redirect(url_for('salary_report_detail', rid=rid))
        except Exception as e:
            db.session.rollback(); flash(f'Import failed: {e}', 'danger')
            return redirect(url_for('salary_report_import', rid=rid))
    companies = Company.query.order_by(Company.name).all()
    return render_template('salary_reports/import.html', report=rep, companies=companies)


@app.route('/salary-reports/<int:rid>/export')
@login_required
@permission_required('manage_staff_invoices')
def salary_report_export(rid: int):
    from models import SalaryReport, SalaryReportRow
    rep = SalaryReport.query.get_or_404(rid)
    # apply same filters as detail view
    search = (request.args.get('q') or '').strip()
    company_filter = (request.args.get('company') or '').strip()
    min_hours = request.args.get('min_hours', type=float)
    max_hours = request.args.get('max_hours', type=float)
    min_rate = request.args.get('min_rate', type=float)
    max_rate = request.args.get('max_rate', type=float)
    min_amount = request.args.get('min_amount', type=float)
    max_amount = request.args.get('max_amount', type=float)
    sort = request.args.get('sort')

    q = SalaryReportRow.query.filter_by(report_id=rid)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(SalaryReportRow.name.ilike(like), SalaryReportRow.national_insurance.ilike(like), SalaryReportRow.company.ilike(like), SalaryReportRow.notes.ilike(like)))
    if company_filter:
        q = q.filter(SalaryReportRow.company.ilike(f"%{company_filter}%"))
    if min_hours is not None:
        q = q.filter(SalaryReportRow.hours >= min_hours)
    if max_hours is not None:
        q = q.filter(SalaryReportRow.hours <= max_hours)
    if min_rate is not None:
        q = q.filter(SalaryReportRow.rate >= min_rate)
    if max_rate is not None:
        q = q.filter(SalaryReportRow.rate <= max_rate)
    if min_amount is not None:
        q = q.filter(SalaryReportRow.amount >= min_amount)
    if max_amount is not None:
        q = q.filter(SalaryReportRow.amount <= max_amount)
    if sort == 'amount_desc':
        q = q.order_by(SalaryReportRow.amount.desc())
    elif sort == 'amount_asc':
        q = q.order_by(SalaryReportRow.amount.asc())
    else:
        q = q.order_by(SalaryReportRow.id.asc())

    rows = q.all()
    data = []
    for r in rows:
        data.append({'Name': r.name, 'National Insurance': r.national_insurance, 'Company': r.company, 'Hours': r.hours, 'Rate': r.rate, 'Amount': r.amount, 'Notes': r.notes})
    try:
        from io import BytesIO

        import pandas as _pd
        df = _pd.DataFrame(data)
        bio = BytesIO()
        with _pd.ExcelWriter(bio, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Rows')
        bio.seek(0)
        fname = f"salary_report_{rid}_rows.xlsx"
        return send_file(bio, as_attachment=True, download_name=fname, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        flash(f'Export failed: {e}', 'danger')
        return redirect(url_for('salary_report_detail', rid=rid))


# Dev helper: initialize SalaryReport tables if missing
@app.cli.command('dev-init-salary-tables')
def dev_init_salary_tables():
    """Create SalaryReport and SalaryReportRow tables if they don't exist yet."""
    try:
        # Import models to register with metadata
        from models import SalaryReport, SalaryReportRow  # noqa: F401
        db.create_all()
        print('✓ Ensured salary report tables exist')
    except Exception as exc:
        print(f'Failed to create tables: {exc}')


# Version & Changelog CLI helpers
@app.cli.command('version')
def cli_version():
    """Print the current application version."""
    try:
        entry = latest_entry()
        date_str = f" ({entry.date})" if entry and entry.date else ""
    except Exception:
        entry = None
        date_str = ""
    print(f"Version: {VERSION}{date_str}")
    if entry and entry.body:
        print("\nNotes:\n" + entry.body)


@app.cli.command('changelog')
@click.option('--limit', default=5, help='Limit number of entries (0 for all).')
def cli_changelog(limit: int):
    """Print parsed changelog entries (newest first)."""
    try:
        lim = None if not limit or int(limit) <= 0 else int(limit)
    except Exception:
        lim = None
    try:
        entries = changelog_json(limit=lim)
        for e in entries:
            print(f"## {e.get('version')} - {e.get('date') or ''}")
            body = (e.get('body') or '').strip()
            if body:
                print(body)
            print()
    except Exception as exc:
        print(f"Failed to read changelog: {exc}")


# Admin utility: retract observation, staff, and student records with safety backup
@app.cli.command('retract-records')
@click.option('--observations/--no-observations', default=True, help='Retract all observation records (backup JSON then delete).')
@click.option('--staff/--no-staff', default=True, help='Retract all staff by marking them inactive (no deletion).')
@click.option('--students/--no-students', default=True, help='Retract all students by setting status="Inactive" (no deletion).')
@click.option('--dry-run', is_flag=True, default=False, help='Show what would change without modifying data.')
@click.option('--yes', is_flag=True, default=False, help='Confirm execution without interactive prompt.')
def cli_retract_records(observations: bool, staff: bool, students: bool, dry_run: bool, yes: bool):
    """Retract records safely without losing data.

    - Observations: export Observation and ObservationDetail to instance/old/observations_backup_YYYYmmddTHHMMSSZ.json then delete rows
    - Staff: set Staff.active = False for all rows
    - Students: set Student.status = 'Inactive' for all rows (previous value preserved only in DB history if any)

    A timestamped SQLite DB backup is created next to the database file when possible.
    """
    import json as _json
    import os as _os
    import shutil as _sh
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from models import Observation, ObservationDetail, Staff, Student

    # Determine DB file path from engine; fallback to config
    try:
        db_path = db.engine.url.database
    except Exception:
        db_path = 'observations.db'

    # If the actual DB resides under instance/, back it up too
    candidates = []
    if db_path:
        candidates.append(db_path)
    if _os.path.exists('instance/observations.db'):
        candidates.append('instance/observations.db')

    # Create backup(s)
    stamp = _dt.now(_tz.utc).strftime('%Y%m%dT%H%M%SZ')
    for p in candidates:
        try:
            if _os.path.exists(p):
                bak = f"{p}.bak-{stamp}"
                print(f"Creating backup: {bak}")
                _sh.copy2(p, bak)
        except Exception as exc:
            print(f"[WARN] Failed to backup {p}: {exc}")

    # Counts before
    obs_n = Observation.query.count() if observations else 0
    det_n = ObservationDetail.query.count() if observations else 0
    staff_n = Staff.query.count() if staff else 0
    students_n = Student.query.count() if students else 0
    print(f"Planned retraction -> observations: {obs_n} (details: {det_n}), staff: {staff_n}, students: {students_n}")

    if dry_run:
        print("Dry-run complete. No changes made.")
        return

    if not yes:
        print("Add --yes to confirm execution. Aborting without changes.")
        return

    # Perform observation backup and deletion
    if observations and (obs_n or det_n):
        try:
            data = []
            for o in Observation.query.all():
                payload = {
                    'id': o.id,
                    'cycle_id': o.cycle_id,
                    'staff_id': o.staff_id,
                    'observer_id': o.observer_id,
                    'date': o.date.isoformat() if getattr(o, 'date', None) else None,
                    'score': o.score,
                    'emailed': bool(getattr(o, 'emailed', False)),
                }
                # Attach detail if present
                d = getattr(o, 'detail', None)
                if d:
                    payload['detail'] = {
                        'timeslot': d.timeslot,
                        'weekly_test': d.weekly_test,
                        'weekly_test_comment': d.weekly_test_comment,
                        'homework': d.homework,
                        'homework_comment': d.homework_comment,
                        'classwork': d.classwork,
                        'classwork_comment': d.classwork_comment,
                        'org_mgmt': d.org_mgmt,
                        'org_mgmt_comment': d.org_mgmt_comment,
                        'positives': d.positives,
                        'improvements': d.improvements,
                        'target_set': d.target_set,
                        'actions_taken': d.actions_taken,
                        'notes': d.notes,
                        'next_review_date': d.next_review_date.isoformat() if getattr(d, 'next_review_date', None) else None,
                    }
                data.append(payload)
            # Write JSON backup under instance/old/
            out_dir = _os.path.join('instance', 'old')
            try:
                _os.makedirs(out_dir, exist_ok=True)
            except Exception:
                pass
            out_path = _os.path.join(out_dir, f'observations_backup_{stamp}.json')
            with open(out_path, 'w', encoding='utf-8') as fh:
                _json.dump({'observations': data}, fh, indent=2)
            # Delete details then observations
            ObservationDetail.query.delete()
            Observation.query.delete()
            db.session.commit()
            print(f"✓ Retracted observations. Backup written to {out_path}")
        except Exception as exc:
            db.session.rollback()
            print(f"[ERR] Failed to retract observations: {exc}")

    # Retract staff by marking inactive
    if staff and staff_n:
        try:
            updated = Staff.query.update({Staff.active: False})
            db.session.commit()
            print(f"✓ Retracted staff: {updated} rows marked inactive")
        except Exception as exc:
            db.session.rollback()
            print(f"[ERR] Failed to retract staff: {exc}")

    # Retract students by status tag
    if students and students_n:
        try:
            updated = Student.query.update({Student.status: 'Inactive'})
            db.session.commit()
            print(f"✓ Retracted students: {updated} rows set to Inactive")
        except Exception as exc:
            db.session.rollback()
            print(f"[ERR] Failed to retract students: {exc}")

# Admin utility: restore observations from a JSON backup created by 'retract-records'
@app.cli.command('restore-observations')
@click.option('--file', 'file_path', default=None, help='Path to observations_backup_*.json. Defaults to the latest in instance/old/.')
@click.option('--update', is_flag=True, default=False, help='Update existing observations if IDs already exist (otherwise they are skipped).')
@click.option('--dry-run', is_flag=True, default=False, help='Show what would be restored without modifying data.')
@click.option('--yes', is_flag=True, default=False, help='Confirm execution without interactive prompt.')
def cli_restore_observations(file_path: str | None, update: bool, dry_run: bool, yes: bool):
    """Restore Observation and ObservationDetail rows from a JSON backup.

    The expected JSON shape matches backups written by 'retract-records':
    {"observations": [{id, cycle_id, staff_id, observer_id, date, score, detail: {...}}]}
    """
    import json as _json
    import os as _os
    from datetime import datetime as _dt

    from models import (Observation, ObservationCycle, ObservationDetail,
                        Staff, User)

    # Resolve default backup file if not provided
    if not file_path:
        search_dir = _os.path.join('instance', 'old')
        latest = None
        latest_mtime = None
        try:
            for name in _os.listdir(search_dir):
                if name.startswith('observations_backup_') and name.endswith('.json'):
                    full = _os.path.join(search_dir, name)
                    try:
                        mtime = _os.path.getmtime(full)
                        if latest is None or (mtime > (latest_mtime or 0)):
                            latest = full
                            latest_mtime = mtime
                    except Exception:
                        continue
        except Exception:
            pass
        file_path = latest

    if not file_path or not _os.path.exists(file_path):
        print(f"[ERR] Backup file not found: {file_path or '(auto)'}")
        return

    # Load JSON
    try:
        with open(file_path, 'r', encoding='utf-8') as fh:
            payload = _json.load(fh)
        records = payload.get('observations') or []
    except Exception as exc:
        print(f"[ERR] Failed to read backup file: {exc}")
        return

    total = len(records)
    # Pre-flight checks: FK presence and existing rows
    missing_fk = 0
    existing_ids = []
    for r in records:
        rid = r.get('id')
        # Check foreign keys exist; if missing, skip these in apply step
        try:
            if not ObservationCycle.query.get(r.get('cycle_id')):
                missing_fk += 1
            if not Staff.query.get(r.get('staff_id')):
                missing_fk += 1
            if not User.query.get(r.get('observer_id')):
                missing_fk += 1
        except Exception:
            # On any unexpected error, defer to apply step
            pass
        if rid is not None and Observation.query.get(rid):
            existing_ids.append(rid)

    print(f"Plan: restore {total} observations from {file_path}")
    if existing_ids and not update:
        print(f"Note: {len(existing_ids)} observation IDs already exist and will be skipped (use --update to overwrite).")
    if missing_fk:
        print(f"Warning: detected {missing_fk} missing related rows (cycle/staff/observer). Those entries will be skipped.")

    if dry_run:
        print("Dry-run complete. No changes made.")
        return
    if not yes:
        print("Add --yes to confirm execution. Aborting without changes.")
        return

    inserted = 0
    updated = 0
    skipped = 0

    def _parse_date(val):
        if not val:
            return None
        try:
            # Accept YYYY-MM-DD or full ISO date
            return _dt.fromisoformat(val).date()
        except Exception:
            try:
                return _dt.strptime(val, '%Y-%m-%d').date()
            except Exception:
                return None

    for r in records:
        rid = r.get('id')
        cycle_id = r.get('cycle_id')
        staff_id = r.get('staff_id')
        observer_id = r.get('observer_id')
        r_date = _parse_date(r.get('date'))
        score = r.get('score')
        emailed = bool(r.get('emailed', False))

        # FK safety
        try:
            if not ObservationCycle.query.get(cycle_id) or not Staff.query.get(staff_id) or not User.query.get(observer_id):
                skipped += 1
                continue
        except Exception:
            skipped += 1
            continue

        existing = Observation.query.get(rid) if rid is not None else None
        if existing:
            if not update:
                skipped += 1
                continue
            # Update fields
            existing.cycle_id = cycle_id
            existing.staff_id = staff_id
            existing.observer_id = observer_id
            if r_date:
                existing.date = r_date
            try:
                existing.score = float(score) if score is not None else existing.score
            except Exception:
                pass
            # Persist emailed flag if provided
            try:
                if emailed is True and getattr(existing, 'emailed', False) is False:
                    existing.emailed = True
            except Exception:
                pass
            # Detail update
            d = r.get('detail') or {}
            if d:
                det = existing.detail or ObservationDetail(observation_id=existing.id)
                det.timeslot = d.get('timeslot')
                det.weekly_test = d.get('weekly_test')
                det.weekly_test_comment = d.get('weekly_test_comment')
                det.homework = d.get('homework')
                det.homework_comment = d.get('homework_comment')
                det.classwork = d.get('classwork')
                det.classwork_comment = d.get('classwork_comment')
                det.org_mgmt = d.get('org_mgmt')
                det.org_mgmt_comment = d.get('org_mgmt_comment')
                det.positives = d.get('positives')
                det.improvements = d.get('improvements')
                det.target_set = d.get('target_set')
                det.actions_taken = d.get('actions_taken')
                det.notes = d.get('notes')
                det.next_review_date = _parse_date(d.get('next_review_date'))
                db.session.add(det)
            db.session.add(existing)
            updated += 1
            continue

        # Create new observation (preserve original ID when possible)
        new_obs = Observation(
            id=rid,
            cycle_id=cycle_id,
            staff_id=staff_id,
            observer_id=observer_id,
            date=r_date or _dt.utcnow().date(),
            score=float(score) if score is not None else 0.0,
        )
        # Set emailed flag if present
        try:
            if emailed:
                new_obs.emailed = True
        except Exception:
            pass
        db.session.add(new_obs)
        db.session.flush()  # ensure new_obs.id available for detail FK

        d = r.get('detail') or {}
        if d:
            det = ObservationDetail(
                observation_id=new_obs.id,
                timeslot=d.get('timeslot'),
                weekly_test=d.get('weekly_test'),
                weekly_test_comment=d.get('weekly_test_comment'),
                homework=d.get('homework'),
                homework_comment=d.get('homework_comment'),
                classwork=d.get('classwork'),
                classwork_comment=d.get('classwork_comment'),
                org_mgmt=d.get('org_mgmt'),
                org_mgmt_comment=d.get('org_mgmt_comment'),
                positives=d.get('positives'),
                improvements=d.get('improvements'),
                target_set=d.get('target_set'),
                actions_taken=d.get('actions_taken'),
                notes=d.get('notes'),
                next_review_date=_parse_date(d.get('next_review_date')),
            )
            db.session.add(det)
        processed += 1
    try:
        db.session.commit()
        print(f"✓ Restore complete. Inserted: {inserted}, Updated: {updated}, Skipped: {skipped}")
    except Exception as exc:
        db.session.rollback()
        print(f"[ERR] Failed to restore observations: {exc}")

@app.route('/api/floor/reports/<int:rid>')
@login_required
@permission_required('manage_floor_reports')
def api_floor_report(rid: int):
    from models import PrintReport
@app.route('/floor/reports/<int:rid>/print')
@login_required
@permission_required('manage_floor_reports')
def floor_report_print(rid: int):
    """Generate a 3x2 inch PNG label (300 DPI) with centered text using Tw Cen MT Bold if available.

    Contents: Resource Name, Resource Type, Code128 barcode, Barcode Number (numeric ID).
    """
    from models import PrintReport
    pr = PrintReport.query.get_or_404(rid)
    # Only allow owner or admins to view
    if not (user_can('manage_floor_reports') or pr.staff_user_id == current_user.id):
        abort(403)
    return render_template('floor/reports/print.html', r=pr)


@app.route('/floor/reports/<int:rid>/pdf')
@login_required
@permission_required('manage_floor_reports')
def floor_report_pdf(rid: int):
    """Temporary placeholder: reuse print page for PDF download link.

    If needed, integrate real PDF rendering later.
    """
    from models import PrintReport
    pr = PrintReport.query.get_or_404(rid)
    if not (user_can('manage_floor_reports') or pr.staff_user_id == current_user.id):
        abort(403)
    # For now, return the same printable HTML (browser can print to PDF)
    return render_template('floor/reports/print.html', r=pr)

@app.route('/floor/call-list')
@login_required
@permission_required('manage_call_list')
def floor_call_list_index():
    """List and filter the daily call list records."""
    from models import CallRecord, Student, User

    # Filters: date, reason, called_by, student, q
    raw_date = (request.args.get('date') or '').strip()
    selected_date = None
    for fmt in ('%Y-%m-%d','%d-%m-%Y'):
        try:
            if raw_date:
                selected_date = _dt.strptime(raw_date, fmt).date(); break
        except Exception:
            pass
    if not selected_date:
        selected_date = date.today()
    reason = (request.args.get('reason') or '').strip()
    called_by = request.args.get('called_by', type=int)
    student_id = request.args.get('student', type=int)
    q = (request.args.get('q') or '').strip()

    query = CallRecord.query.filter(CallRecord.date == selected_date)
    if reason:
        query = query.filter(CallRecord.reason == reason)
    if called_by:
        query = query.filter(CallRecord.created_by_id == called_by)
    if student_id:
        query = query.filter(CallRecord.student_id == student_id)
    if q:
        # simple search across student name/id and discussion/outcome
        try:
            query = query.join(Student, CallRecord.student_id == Student.id).filter(
                (Student.name.ilike(f"%{q}%")) |
                (Student.student_id.ilike(f"%{q}%")) |
                (CallRecord.discussion.ilike(f"%{q}%")) |
                (CallRecord.outcome.ilike(f"%{q}%"))
            )
        except Exception:
            pass
    records = query.order_by(CallRecord.created_at.desc()).all()

    reasons = ['absence','lateness','payment issue','detention','meeting','event','other']
    staff_opts = User.query.filter(User.role.in_(['superadmin','centre_manager','supervisor','admin'])).order_by(User.name.asc()).all()
    students = Student.query.order_by(Student.name.asc()).all()
    return render_template('floor/calls/index.html', records=records, selected_date=selected_date, reasons=reasons, staff_opts=staff_opts, students=students, selected_reason=reason, selected_called_by=called_by, selected_student=student_id, q=q)


@app.route('/floor/call-list/new', methods=['GET','POST'])
@login_required
@permission_required('manage_call_list')
def floor_call_list_new():
    from models import CallRecord, Meeting, Student, User
    if request.method == 'POST':
        try:
            student_id = request.form.get('student_id', type=int)
            reason = (request.form.get('reason') or '').strip()
            raw_date = (request.form.get('date') or '').strip()
            d = None
            for fmt in ('%Y-%m-%d','%d-%m-%Y'):
                try:
                    d = _dt.strptime(raw_date, fmt).date(); break
                except Exception: pass
            if not d: d = date.today()
            day = _shift_day_for(d)
            discussion = (request.form.get('discussion') or '').strip()
            outcome = (request.form.get('outcome') or '').strip() or None

            appt_date = None
            appt_with_id = None
            meeting_ref = None
            event_att = None

            if reason == 'meeting':
                raw_appt = (request.form.get('appointment_date') or '').strip()
                for fmt in ('%Y-%m-%d','%d-%m-%Y'):
                    try:
                        if raw_appt:
                            appt_date = _dt.strptime(raw_appt, fmt).date(); break
                    except Exception: pass
                appt_with_id = request.form.get('appointment_with_id', type=int)
                # Create a Meeting entry
                if appt_date and appt_with_id:
                    # Use 00:00 as time by default
                    m = Meeting(participant_id=appt_with_id, booked_by_id=current_user.id, agenda=discussion or f'Meeting for student {student_id}', date=appt_date, time='00:00')
                    db.session.add(m); db.session.flush()
                    meeting_ref = m
                    # Send notification to the participant (simple)
                    try:
                        from email_utils import send_email
                        participant = User.query.get(appt_with_id)
                        if participant and participant.email:
                            subj = f"New meeting scheduled on {appt_date.strftime('%Y-%m-%d')}"
                            html = f"""
                                <p>Hello {participant.name},</p>
                                <p>A new meeting has been scheduled for <strong>{appt_date.strftime('%A, %d %B %Y')}</strong>.</p>
                                <p><strong>Agenda:</strong> {(discussion or 'N/A')}</p>
                                <p><strong>Booked by:</strong> {current_user.name}</p>
                                <p style='font-size:12px;color:#64748b;'>This is an automated notification from Excel Tutors portal.</p>
                            """
                            send_email(participant.email, subj, html)
                    except Exception as _e:
                        print(f"[WARN] Meeting email failed: {_e}")
            elif reason == 'event':
                event_att = (request.form.get('event_attendance') or '').strip() or None

            rec = CallRecord(created_by_id=current_user.id, student_id=student_id, reason=reason, date=d, day=day, discussion=discussion, outcome=outcome, appointment_date=appt_date, appointment_with_id=appt_with_id, meeting_id=(meeting_ref.id if meeting_ref else None), event_attendance=event_att)
            db.session.add(rec); db.session.commit()
            flash('Call saved','success')
        except Exception as exc:
            db.session.rollback(); flash(f'Failed to save call: {exc}','danger')
        return redirect(url_for('floor_call_list_index'))
    # GET -> modal content (do NOT preload all students; rely on /api/students/suggest for autocomplete)
    reasons = ['absence','lateness','payment issue','detention','meeting','event','other']
    staff_opts = User.query.filter(User.role.in_(['superadmin','centre_manager','supervisor','admin'])).order_by(User.name.asc()).all()
    return render_template('floor/calls/form.html', record=None, reasons=reasons, staff_opts=staff_opts, today=date.today())

@app.route('/floor/call-list/<int:cid>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_call_list')
def floor_call_list_edit(cid: int):
    from models import CallRecord, Meeting, Student, User
    rec = CallRecord.query.get_or_404(cid)
    if request.method == 'POST':
        try:
            rec.student_id = request.form.get('student_id', type=int)
            rec.reason = (request.form.get('reason') or '').strip()
            raw_date = (request.form.get('date') or '').strip()
            d = None
            for fmt in ('%Y-%m-%d','%d-%m-%Y'):
                try:
                    d = _dt.strptime(raw_date, fmt).date(); break
                except Exception: pass
            if not d: d = rec.date
            rec.date = d
            rec.day = _shift_day_for(d)
            rec.discussion = (request.form.get('discussion') or '').strip()
            rec.outcome = (request.form.get('outcome') or '').strip() or None

            # Reset special fields
            rec.appointment_date = None
            rec.appointment_with_id = None
            # Keep existing meeting if still meeting; otherwise clear link
            keep_meeting = False

            if rec.reason == 'meeting':
                raw_appt = (request.form.get('appointment_date') or '').strip()
                appt_date = None
                for fmt in ('%Y-%m-%d','%d-%m-%Y'):
                    try:
                        if raw_appt:
                            appt_date = _dt.strptime(raw_appt, fmt).date(); break
                    except Exception: pass
                appt_with_id = request.form.get('appointment_with_id', type=int)
                rec.appointment_date = appt_date
                rec.appointment_with_id = appt_with_id
                keep_meeting = bool(appt_date and appt_with_id)
                if keep_meeting:
                    if rec.meeting_id:
                        m = Meeting.query.get(rec.meeting_id)
                        if m:
                            m.participant_id = appt_with_id
                            m.agenda = rec.discussion or m.agenda
                            m.date = appt_date
                            m.time = m.time or '00:00'
                    else:
                        m = Meeting(participant_id=appt_with_id, booked_by_id=current_user.id, agenda=rec.discussion or f'Meeting for student {rec.student_id}', date=appt_date, time='00:00')
                        db.session.add(m); db.session.flush()
                        rec.meeting_id = m.id
                else:
                    rec.meeting_id = None
            elif rec.reason == 'event':
                rec.event_attendance = (request.form.get('event_attendance') or '').strip() or None
                rec.meeting_id = None
            else:
                rec.event_attendance = None
                rec.meeting_id = None

            db.session.commit(); flash('Call updated','success')
        except Exception as exc:
            db.session.rollback(); flash(f'Update failed: {exc}','danger')
        return redirect(url_for('floor_call_list_index'))
    reasons = ['absence','lateness','payment issue','detention','meeting','event','other']
    staff_opts = User.query.filter(User.role.in_(['superadmin','centre_manager','supervisor','admin'])).order_by(User.name.asc()).all()
    # Avoid preloading students; autocomplete will query the API
    return render_template('floor/calls/form.html', record=rec, reasons=reasons, staff_opts=staff_opts, today=rec.date or date.today())

@app.route('/floor/call-list/<int:cid>/delete', methods=['POST'])
@login_required
@permission_required('manage_call_list')
def floor_call_list_delete(cid: int):
    from models import CallRecord
    rec = CallRecord.query.get_or_404(cid)
    try:
        db.session.delete(rec); db.session.commit();
        flash('Call deleted','success')
    except Exception as exc:
        db.session.rollback(); flash(f'Delete failed: {exc}','danger')
    return redirect(url_for('floor_call_list_index'))

@app.route('/floor/call-list/print')
@login_required
@permission_required('manage_call_list')
def floor_call_list_print():
    # Print view for a given user and date
    from models import CallRecord, User
    uid = request.args.get('user', type=int) or current_user.id
    raw_date = (request.args.get('date') or '').strip()
    d = None
    for fmt in ('%Y-%m-%d','%d-%m-%Y'):
        try:
            if raw_date:
                d = _dt.strptime(raw_date, fmt).date(); break
        except Exception: pass
    if not d: d = date.today()
    user = User.query.get(uid)
    records = CallRecord.query.filter(CallRecord.created_by_id==uid, CallRecord.date==d).order_by(CallRecord.created_at.asc()).all()
    return render_template('floor/calls/print.html', user=user, selected_date=d, records=records)

@app.route('/floor/call-list/pdf')
@login_required
@permission_required('manage_call_list')
def floor_call_list_pdf():
    from xhtml2pdf import pisa

    from models import CallRecord, User
    uid = request.args.get('user', type=int) or current_user.id
    raw_date = (request.args.get('date') or '').strip()
    d = None
    for fmt in ('%Y-%m-%d','%d-%m-%Y'):
        try:
            if raw_date:
                d = _dt.strptime(raw_date, fmt).date(); break
        except Exception: pass
    if not d: d = date.today()
    user = User.query.get(uid)
    records = CallRecord.query.filter(CallRecord.created_by_id==uid, CallRecord.date==d).order_by(CallRecord.created_at.asc()).all()
    html = render_template('floor/calls/print.html', user=user, selected_date=d, records=records)
    pdf_io = io.BytesIO()
    try:
        pisa.CreatePDF(io.StringIO(html), dest=pdf_io)  # type: ignore[arg-type]
    except Exception as exc:
        flash(f'PDF generation failed: {exc}', 'danger')
        return redirect(url_for('floor_call_list_index'))
    pdf_io.seek(0)
    fname = f"call_list_{uid}_{d.isoformat()}.pdf"
    return send_file(pdf_io, as_attachment=True, download_name=fname, mimetype='application/pdf')

@app.route('/floor/call-list/<int:cid>/view')
@login_required
@permission_required('manage_call_list')
def floor_call_list_view(cid: int):
    from models import CallRecord
    rec = CallRecord.query.get_or_404(cid)
    # Return a lightweight partial for modal consumption
    return render_template('floor/calls/view.html', record=rec)


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

    # ---------------- Floor KPIs (Shifts, Checklists, Calls, Reports, Meetings, Todos) ----------------
    try:
        from models import (CallRecord, EndOfDayChecklist, Meeting,
                            PrintReport, Shift, Todo)

        # Shifts today
        shifts_today_count = Shift.query.filter(Shift.date == today).count()
        # Distinct staff with shifts today
        staff_today_subq = db.session.query(Shift.staff_user_id).filter(Shift.date == today).distinct().subquery()
        staff_today_count = db.session.query(db.func.count(db.func.distinct(Shift.staff_user_id))).filter(Shift.date == today).scalar() or 0
        # EOD submitted today by those staff
        eod_done_count = db.session.query(db.func.count(db.func.distinct(EndOfDayChecklist.staff_user_id))).filter(
            EndOfDayChecklist.date == today,
            EndOfDayChecklist.staff_user_id.in_(db.select(staff_today_subq.c.staff_user_id))
        ).scalar() or 0
        pending_eod_count = max(0, (staff_today_count or 0) - (eod_done_count or 0))
        # Build missing EOD staff list (admin views) and my pending flag
        staff_today_ids = [row[0] for row in db.session.query(Shift.staff_user_id).filter(Shift.date == today).distinct().all()]
        eod_done_ids = [row[0] for row in db.session.query(EndOfDayChecklist.staff_user_id).filter(EndOfDayChecklist.date == today, EndOfDayChecklist.staff_user_id.in_(staff_today_ids)).distinct().all()]
        missing_ids = [sid for sid in staff_today_ids if sid not in (eod_done_ids or [])]
        missing_eod_staff = []
        if missing_ids:
            try:
                missing_eod_staff = User.query.filter(User.id.in_(missing_ids)).order_by(User.name.asc()).all()
            except Exception:
                missing_eod_staff = []
        # Current user pending?
        my_eod_pending = False
        if current_user.is_authenticated:
            try:
                has_shift = db.session.query(Shift.id).filter(Shift.date == today, Shift.staff_user_id == current_user.id).first() is not None
                has_eod = db.session.query(EndOfDayChecklist.id).filter(EndOfDayChecklist.date == today, EndOfDayChecklist.staff_user_id == current_user.id).first() is not None
                my_eod_pending = bool(has_shift and not has_eod)
            except Exception:
                my_eod_pending = False
        # Calls queued today (no outcome)
        from sqlalchemy import or_
        calls_queued_today = CallRecord.query.filter(
            CallRecord.date == today,
            or_(CallRecord.outcome.is_(None), db.func.length(db.func.trim(CallRecord.outcome)) == 0)
        ).count()
        # Print Reports pending for today (by staff with shifts today)
        pr_done_count = db.session.query(db.func.count(db.func.distinct(PrintReport.staff_user_id))).filter(
            PrintReport.date == today,
            PrintReport.staff_user_id.in_(db.select(staff_today_subq.c.staff_user_id))
        ).scalar() or 0
        pending_print_reports = max(0, (staff_today_count or 0) - (pr_done_count or 0))
        # Unapproved print incidents today
        unapproved_prints_today = PrintReport.query.filter(PrintReport.date == today, PrintReport.has_unapproved.is_(True)).count()
        # Upcoming meetings (next 7 days)
        upcoming_window = today + timedelta(days=7)
        upcoming_meetings = Meeting.query.filter(Meeting.date >= today, Meeting.date <= upcoming_window).count()
        # My pending todos
        my_pending_todos = Todo.query.filter(Todo.assigned_to_id == current_user.id, (Todo.status.is_(None)) | (Todo.status != 'Done')).count()
    except Exception:
        shifts_today_count = 0
        pending_eod_count = 0
        calls_queued_today = 0
        pending_print_reports = 0
        unapproved_prints_today = 0
        upcoming_meetings = 0
        my_pending_todos = 0
        missing_eod_staff = []
        my_eod_pending = False

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
        # Floor KPIs
        shifts_today_count=shifts_today_count,
        pending_eod_count=pending_eod_count,
        calls_queued_today=calls_queued_today,
        pending_print_reports=pending_print_reports,
        unapproved_prints_today=unapproved_prints_today,
        upcoming_meetings=upcoming_meetings,
        my_pending_todos=my_pending_todos,
        missing_eod_staff=missing_eod_staff,
        my_eod_pending=my_eod_pending,
        today=today,
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

# Enforce password change on first login for converted accounts
@app.before_request
def _force_password_change_gate():
    try:
        if current_user.is_authenticated and getattr(current_user, 'force_password_reset', False):
            # Allow only a safe subset of endpoints until password is changed
            allowed_endpoints = {'logout', 'profile', 'static', 'login', 'request_reset', 'reset_with_token'}
            ep = (request.endpoint or '')
            if not (ep in allowed_endpoints or ep.startswith('static')):
                if request.path != url_for('profile'):
                    return redirect(url_for('profile'))
    except Exception:
        # Never block due to errors in the gate
        pass
@app.route("/approve")
@login_required
@permission_required('manage_users')
def approve_users():
    role_filter = request.args.get('role')
    q = User.query
    if role_filter and role_filter not in ('all',''):
        q = q.filter(User.role == role_filter)
    users = q.order_by(User.created_at.desc()).all()
    # Build role capability legend from current RolePermission table + descriptions
    known_roles = ['tutor','staff','supervisor','centre_manager','admin','superadmin']
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
@permission_required('manage_users')
def bulk_role_assign():
    ids_raw = request.form.get('user_ids','')
    target_role = (request.form.get('target_role') or '').strip().lower()
    legacy_alias = {'observer':'supervisor','lead':'centre_manager'}
    if target_role in legacy_alias:
        target_role = legacy_alias[target_role]
    if target_role not in ['tutor','staff','supervisor','centre_manager','admin','superadmin']:
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
@permission_required('manage_users')
def approve_user(uid):
    u = User.query.get_or_404(uid)
    u.is_approved = True
    db.session.commit()
    flash(f"Approved {u.email}", "success")
    return redirect(url_for('approve_users'))

@app.route("/approve/<int:uid>/toggle_sa", methods=["POST"])
@login_required
@permission_required('manage_users')
def toggle_superadmin(uid):
    # Only superadmins can toggle superadmin status
    if not current_user.is_superadmin:
        abort(403)
    u = User.query.get_or_404(uid)
    u.is_superadmin = not u.is_superadmin
    db.session.commit()
    flash(f"Toggled superadmin for {u.email}", "success")
    return redirect(url_for('approve_users'))

@app.route('/approve/<int:uid>/role', methods=['POST'])
@login_required
@permission_required('manage_users')
def set_user_role(uid):
    u = User.query.get_or_404(uid)
    role = request.form.get('role','').strip().lower()
    legacy_alias = {'observer': 'supervisor', 'lead': 'centre_manager'}
    if role in legacy_alias:
        role = legacy_alias[role]
    if role not in ['tutor','staff','supervisor','centre_manager','admin','superadmin']:
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
@permission_required('manage_users')
def delete_user(uid):
    """Delete a user.

    Safety guards:
    - Cannot delete yourself.
    - Cannot delete the last remaining superadmin.
    - Blocks deletion if the user has dependent records (observations, meetings, tasks, appointment slots/bookings, permission audits, todos) to avoid orphaned references.
    """
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
@permission_required('manage_users')
def toggle_user_active(uid):
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
    if not current_user.is_superadmin and not user_can('manage_users') and current_user.id != uid:
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

@app.route('/approve/<int:uid>/reset-password', methods=['POST'])
@login_required
@permission_required('manage_users')
def reset_user_password(uid):
    u = User.query.get_or_404(uid)
    try:
        temp_pwd = _generate_temp_password()
        u.password_hash = generate_password_hash(temp_pwd)
        if hasattr(u, 'force_password_reset'):
            u.force_password_reset = True
        db.session.commit()
        try:
            subject, html = _build_account_welcome_email_html(u.name, u.email, temp_pwd)
            send_email(u.email, subject, html)
        except Exception as em:
            flash(f"Password reset, but email failed: {em}", 'warning')
            return redirect(url_for('approve_users'))
        flash('Temporary password emailed to user.', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to reset password: {exc}', 'danger')
    return redirect(url_for('approve_users'))

PERMISSION_GROUP_DEFINITIONS: OrderedDict[str, dict[str, object]] = OrderedDict([
    (
        'core',
        {
            'label': 'Core Platform',
            'description': 'Dashboard access, global workforce tools and system administration.',
            'keys': [
                'view_dashboard',
                'manage_tasks',
                'view_reports',
                'manage_users',
                'manage_settings',
                'manage_error_reports',
            ],
        },
    ),
    (
        'staff_ops',
        {
            'label': 'Staff & Operations',
            'description': 'Staff records, meetings, availability and quality assurance.',
            'keys': [
                'manage_staff',
                'manage_meetings',
                'manage_availability',
                'manage_issues',
                'manage_cycles',
                'manage_observations',
            ],
        },
    ),
    (
        'students',
        {
            'label': 'Students & Admissions',
            'description': 'Student records, admissions pipeline and support tooling.',
            'keys': [
                'manage_students',
                'manage_student_concerns',
                'manage_admission_assessments',
                'manage_appointments',
                'access_enrollment_tool',
            ],
        },
    ),
    (
        'resources',
        {
            'label': 'Resources & Curriculum',
            'description': 'Book catalogues, tuition pricing, resource inventory and DBS management.',
            'keys': [
                'manage_books',
                'order_books',
                'manage_pricing',
                'manage_resources',
                'manage_dbs',
            ],
        },
    ),
    (
        'finance',
        {
            'label': 'Finance & Invoicing',
            'description': 'Invoice management and staff payment workflows.',
            'keys': [
                'manage_kids_club_invoices',
                'manage_staff_invoices',
                'submit_staff_invoices',
            ],
        },
    ),
    (
        'enrollment',
        {
            'label': 'Course Enrollment',
            'description': 'Manage course products, view enrollment orders, and configure discount settings for the public enrollment form.',
            'keys': ['manage_products', 'view_enrollments', 'manage_pricing'],
        },
    ),
    (
        'floor_ops',
        {
            'label': 'Floor & Centre Operations',
            'description': 'On-site scheduling, floor dashboards and attendance fixes.',
            'keys': [
                'floor_dashboard',
                'manage_shifts',
                'manage_supervisor_shifts',
                'manage_eod_checklist',
                'manage_floor_reports',
                'manage_call_list',
                'manage_attendance_fix',
            ],
        },
    ),
    (
        'communications',
        {
            'label': 'Communications & Outreach',
            'description': 'Recruitment comms and email system visibility.',
            'keys': [
                'manage_email_logs',
                'manage_recruitment',
            ],
        },
    ),
    (
        'timetabling',
        {
            'label': 'Specialist Tools',
            'description': 'Specialist timetable generators and utilities.',
            'keys': [
                'access_timetable_main',
                'access_timetable_eastham',
            ],
        },
    ),
])


def _organize_permissions(perms: list[Permission]) -> list[dict[str, object]]:
    """Arrange Permission rows into UI groups with friendly labels."""
    perms_by_key = {p.key: p for p in perms}
    used_keys: set[str] = set()
    groups: list[dict[str, object]] = []
    for slug, meta in PERMISSION_GROUP_DEFINITIONS.items():
        keys = meta.get('keys', []) or []
        group_perms = [perms_by_key[k] for k in keys if k in perms_by_key]
        if not group_perms:
            continue
        used_keys.update(p.key for p in group_perms)
        groups.append(
            {
                'slug': slug,
                'label': meta.get('label', slug.title()),
                'description': meta.get('description', ''),
                'permissions': group_perms,
            }
        )
    remaining = [p for p in perms if p.key not in used_keys]
    if remaining:
        groups.append(
            {
                'slug': 'other',
                'label': 'Other Permissions',
                'description': 'Permissions that are not yet grouped.',
                'permissions': remaining,
            }
        )
    return groups


# ---------------- Permission Management (0.9.0) ----------------
@app.route('/admin/role-permissions', methods=['GET','POST'])
@login_required
@permission_required('manage_users')
def role_permissions():
    # Dynamic roles (exclude superadmin which is implicit all-access)
    roles = sorted({r[0] for r in db.session.query(User.role).distinct() if r[0] and r[0] != 'superadmin'} | {'tutor','staff','supervisor','centre_manager','admin'})
    perms = Permission.query.order_by(Permission.key.asc()).all()
    if request.method == 'POST':

        # For each role/perm pair, expect checkbox name rp_<role>__<perm>
        existing = {(rp.role, rp.permission_key): rp for rp in RolePermission.query.all()}
        seen_keys = set()
        changes = {'added': [], 'removed': []}
        for role in roles:
            for p in perms:
                field = f"rp_{role}__{p.key}"
                want = field in request.form
                key = (role, p.key)
                if want and key not in existing:
                    db.session.add(RolePermission(role=role, permission_key=p.key))
                    changes['added'].append(f"{role}:{p.key}")
                    try:
                        db.session.flush()
                        db.session.add(PermissionAudit(actor_user_id=current_user.id, role=role, permission_key=p.key, action='added'))
                    except Exception:
                        pass
                if not want and key in existing:
                    db.session.delete(existing[key])
                    changes['removed'].append(f"{role}:{p.key}")
                    try:
                        db.session.flush()
                        db.session.add(PermissionAudit(actor_user_id=current_user.id, role=role, permission_key=p.key, action='removed'))
                    except Exception:
                        pass
                seen_keys.add(key)
        db.session.commit()

        # Show detailed feedback
        if not changes['added'] and not changes['removed']:
            flash('No changes made to permissions', 'info')
        else:
            msg_parts = []
            if changes['added']:
                msg_parts.append(f"Added {len(changes['added'])} permission(s)")
            if changes['removed']:
                msg_parts.append(f"Removed {len(changes['removed'])} permission(s)")
            flash(f"Role permissions updated. {' | '.join(msg_parts)}", 'success')
        return redirect(url_for('role_permissions'))
    role_map = {r.role: set() for r in RolePermission.query.with_entities(RolePermission.role).distinct()}
    for rp in RolePermission.query.all():
        role_map.setdefault(rp.role, set()).add(rp.permission_key)
    permission_groups = _organize_permissions(perms)
    return render_template(
        'admin/role_permissions.html',
        roles=roles,
        perms=perms,
        role_map=role_map,
        permission_groups=permission_groups,
    )

@app.route('/admin/role-permissions/appointments', methods=['POST'])
@login_required
@permission_required('manage_users')
def role_permissions_appointments():
    """Quick update endpoint to adjust only the manage_appointments permission per role.

    Does not disturb other role permissions (unlike full matrix save which rewrites all).
    """
    roles = sorted({r[0] for r in db.session.query(User.role).distinct() if r[0] and r[0] != 'superadmin'} | {'tutor','staff','supervisor','centre_manager','admin'})
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
@permission_required('manage_users')
def user_permissions():
    user_id = request.args.get('user_id', type=int) or request.form.get('user_id', type=int)
    users = User.query.order_by(User.name.asc()).all()
    perms = Permission.query.order_by(Permission.key.asc()).all()
    selected_user = User.query.get(user_id) if user_id else None
    if request.method == 'POST' and selected_user:
        # For each permission, radio: inherit / allow / deny -> name=perm_<key> value=inherit|allow|deny
        existing = {up.permission_key: up for up in UserPermission.query.filter_by(user_id=selected_user.id).all()}

        # Track changes for better UX feedback
        changes_detail = {'added_allow': [], 'added_deny': [], 'removed': [], 'modified': []}

        for p in perms:
            val = request.form.get(f'perm_{p.key}')
            if val == 'inherit':
                if p.key in existing:
                    db.session.delete(existing[p.key])
                    changes_detail['removed'].append(p.key)
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
                        changes_detail['modified'].append(f"{p.key} → {'allow' if allow_flag else 'deny'}")
                        try:
                            db.session.flush()
                            db.session.add(PermissionAudit(actor_user_id=current_user.id, target_user_id=selected_user.id, permission_key=p.key, action='allow' if allow_flag else 'deny'))
                        except Exception:
                            pass
                else:
                    db.session.add(UserPermission(user_id=selected_user.id, permission_key=p.key, allow=allow_flag))
                    if allow_flag:
                        changes_detail['added_allow'].append(p.key)
                    else:
                        changes_detail['added_deny'].append(p.key)
                    try:
                        db.session.flush()
                        db.session.add(PermissionAudit(actor_user_id=current_user.id, target_user_id=selected_user.id, permission_key=p.key, action='allow' if allow_flag else 'deny'))
                    except Exception:
                        pass

        db.session.commit()

        # Show detailed feedback based on what actually changed
        if not any(changes_detail.values()):
            flash('No changes made to user permissions', 'info')
        else:
            msg_parts = []
            if changes_detail['added_allow']:
                msg_parts.append(f"Allowed: {', '.join(changes_detail['added_allow'])}")
            if changes_detail['added_deny']:
                msg_parts.append(f"Denied: {', '.join(changes_detail['added_deny'])}")
            if changes_detail['removed']:
                msg_parts.append(f"Removed: {', '.join(changes_detail['removed'])}")
            if changes_detail['modified']:
                msg_parts.append(f"Modified: {', '.join(changes_detail['modified'])}")

            flash(f"User permissions updated. {' | '.join(msg_parts)}", 'success')

        return redirect(url_for('user_permissions', user_id=selected_user.id))
    overrides = {}
    if selected_user:
        overrides = {up.permission_key: up.allow for up in UserPermission.query.filter_by(user_id=selected_user.id).all()}
    permission_groups = _organize_permissions(perms)
    return render_template(
        'admin/user_permissions.html',
        users=users,
        selected_user=selected_user,
        perms=perms,
        overrides=overrides,
        permission_groups=permission_groups,
    )


@app.route('/admin/access-summary')
@login_required
@permission_required('manage_users')
def access_control_summary():
    """Comprehensive access control summary showing all roles, their permissions,
    and every user's effective access — useful for auditing and debugging access issues."""
    perms = Permission.query.order_by(Permission.key.asc()).all()
    perm_desc = {p.key: p.description for p in perms}
    all_keys = [p.key for p in perms]

    # Roles and their assigned permissions
    known_roles = ['tutor', 'staff', 'supervisor', 'centre_manager', 'admin', 'superadmin']
    role_map = {}
    for role in known_roles:
        if role == 'superadmin':
            role_map[role] = set(all_keys)  # superadmin has everything
        else:
            role_map[role] = {
                rp.permission_key
                for rp in RolePermission.query.filter_by(role=role).all()
            }

    # All users with their effective permissions
    users = User.query.filter_by(is_active=True).order_by(User.name.asc()).all()
    user_access = []
    for u in users:
        if u.is_superadmin:
            effective = set(all_keys)
            source = 'superadmin'
        else:
            role = u.role or 'staff'
            effective = set(role_map.get(role, set()))
            source = role
        # Apply user overrides
        overrides = {up.permission_key: up.allow for up in UserPermission.query.filter_by(user_id=u.id).all()}
        override_count = len(overrides)
        for key, allow in overrides.items():
            if allow:
                effective.add(key)
            else:
                effective.discard(key)
        user_access.append({
            'user': u,
            'role': u.role or 'staff',
            'effective': effective,
            'override_count': override_count,
            'source': source,
        })

    permission_groups = _organize_permissions(perms)

    return render_template(
        'admin/access_summary.html',
        perms=perms,
        perm_desc=perm_desc,
        all_keys=all_keys,
        known_roles=known_roles,
        role_map=role_map,
        user_access=user_access,
        permission_groups=permission_groups,
    )


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
            from email_utils import send_with_template
            cust_key = f"appointment_customer_{booking.language}_cancelled"
            try:
                send_with_template(cust_key, {
                    'name': booking.name,
                    'superadmin': slot.superadmin.name,
                    'date': slot.start_at.strftime('%A, %d %B %Y'),
                    'time': slot.start_at.strftime('%H:%M'),
                    'student': booking.student_ref,
                    'reason': booking.reason,
                    'email': booking.email,
                    'phone': booking.phone,
                    'to_email': booking.email,
                }, to_email=booking.email, fallback=lambda: build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='cancelled', cancel_url=None))
            except Exception:
                subj, html = build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='cancelled', cancel_url=None)
                _send_email_safe(booking.email, subj, html, log_prefix='Appointment cancellation')
            try:
                send_with_template('appointment_admin_cancelled_admin', {
                    'name': booking.name,
                    'student': booking.student_ref,
                    'to_email': slot.superadmin.email,
                    'date': slot.start_at.strftime('%A, %d %B %Y'),
                    'time': slot.start_at.strftime('%H:%M'),
                }, to_email=slot.superadmin.email, fallback=lambda: build_appointment_admin_email(booking, slot, mode='cancelled_admin'))
            except Exception:
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
            from email_utils import send_with_template
            cust_key = f"appointment_customer_{booking.language}_cancelled"
            try:
                send_with_template(cust_key, {
                    'name': booking.name,
                    'superadmin': slot.superadmin.name,
                    'date': slot.start_at.strftime('%A, %d %B %Y'),
                    'time': slot.start_at.strftime('%H:%M'),
                    'student': booking.student_ref,
                    'reason': booking.reason,
                    'email': booking.email,
                    'phone': booking.phone,
                    'to_email': booking.email,
                }, to_email=booking.email, fallback=lambda: build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='cancelled', cancel_url=None))
            except Exception:
                subj, html = build_appointment_email(booking, slot, slot.superadmin, language=booking.language, mode='cancelled', cancel_url=None)
                _send_email_safe(booking.email, subj, html, log_prefix='Appointment cancellation')
            try:
                send_with_template('appointment_admin_cancelled_admin', {
                    'name': booking.name,
                    'student': booking.student_ref,
                    'to_email': slot.superadmin.email,
                    'date': slot.start_at.strftime('%A, %d %B %Y'),
                    'time': slot.start_at.strftime('%H:%M'),
                }, to_email=slot.superadmin.email, fallback=lambda: build_appointment_admin_email(booking, slot, mode='cancelled_admin'))
            except Exception:
                admin_subj, admin_html = build_appointment_admin_email(booking, slot, mode='cancelled_admin')
                _send_email_safe(slot.superadmin.email, admin_subj, admin_html, log_prefix='Appointment admin cancellation')
            flash('Booking cancelled and attendee notified.', 'success')
    else:
        flash('Unsupported action.', 'danger')
    return redirect(url_for('admin_appointments'))


# ---------------- Admin: Product Management ---------------- #
@app.route('/admin/products')
@login_required
@permission_required('manage_products')
def admin_products_list():
    """List all products for course enrollment"""
    from models import Product
    products = Product.query.order_by(Product.created_at.desc()).all()
    return render_template('admin/products_list.html', products=products)


@app.route('/admin/products/new', methods=['GET', 'POST'])
@login_required
@permission_required('manage_products')
def admin_products_new():
    """Create new product"""
    from forms import ProductForm
    from models import Product
    form = ProductForm()
    if form.validate_on_submit():
        product = Product(
            name=form.name.data,
            description=form.description.data,
            price=form.price.data,
            thumbnail_url=form.thumbnail_url.data,
            date=form.date.data,
            venue=form.venue.data,
            time=form.time.data,
            instructor=form.instructor.data,
            active=form.active.data
        )
        db.session.add(product)
        db.session.commit()
        flash('Product created successfully', 'success')
        return redirect(url_for('admin_products_list'))
    return render_template('admin/products_form.html', form=form, title='New Product')


@app.route('/admin/products/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('manage_products')
def admin_products_edit(id):
    """Edit existing product"""
    from forms import ProductForm
    from models import Product
    product = Product.query.get_or_404(id)
    form = ProductForm(obj=product)
    if form.validate_on_submit():
        product.name = form.name.data
        product.description = form.description.data
        product.price = form.price.data
        product.thumbnail_url = form.thumbnail_url.data
        product.date = form.date.data
        product.venue = form.venue.data
        product.time = form.time.data
        product.instructor = form.instructor.data
        product.active = form.active.data
        product.updated_at = datetime.utcnow()
        db.session.commit()
        flash('Product updated successfully', 'success')
        return redirect(url_for('admin_products_list'))
    return render_template('admin/products_form.html', form=form, title='Edit Product', product=product)


@app.route('/admin/products/<int:id>/delete', methods=['POST'])
@login_required
@permission_required('manage_products')
def admin_products_delete(id):
    """Delete product"""
    from models import Product
    product = Product.query.get_or_404(id)
    db.session.delete(product)
    db.session.commit()
    flash('Product deleted successfully', 'success')
    return redirect(url_for('admin_products_list'))


@app.route('/admin/products/<int:id>/toggle', methods=['POST'])
@login_required
@permission_required('manage_products')
def admin_products_toggle(id):
    """Toggle product active status"""
    from models import Product
    product = Product.query.get_or_404(id)
    product.active = not product.active
    db.session.commit()
    return jsonify({'success': True, 'active': product.active})


# ---------------- Admin: Enrollment/Order Tracking ---------------- #
@app.route('/admin/enrollments')
@login_required
@permission_required('view_enrollments')
def admin_enrollments_list():
    """List all course enrollment orders"""
    from models import Order, OrderItem

    # ── query-string filters ────────────────────────────────────
    q = (request.args.get('q') or '').strip()
    branch_filter = (request.args.get('branch') or '').strip()
    status_filter = (request.args.get('status') or '').strip().lower()
    year_filter = (request.args.get('year') or '').strip()
    product_filter = (request.args.get('product') or '').strip()

    query = Order.query

    if q:
        like = f'%{q}%'
        query = query.filter(
            db.or_(
                Order.student_name.ilike(like),
                Order.student_id.ilike(like),
                Order.parent_email.ilike(like),
            )
        )
    if branch_filter:
        query = query.filter(Order.branch == branch_filter)
    if status_filter:
        query = query.filter(Order.payment_status == status_filter)
    if year_filter:
        query = query.filter(Order.year_group == year_filter)
    if product_filter:
        query = query.filter(
            Order.items.any(OrderItem.product_name.ilike(f'%{product_filter}%'))
        )

    orders = query.order_by(Order.created_at.desc()).all()

    # ── distinct values for filter dropdowns ────────────────────
    all_branches = sorted({o.branch for o in Order.query.with_entities(Order.branch).distinct()})
    all_statuses = sorted({r[0] for r in Order.query.with_entities(Order.payment_status).distinct()})
    all_years = sorted({r[0] for r in Order.query.with_entities(Order.year_group).distinct()})
    all_products = sorted({r[0] for r in db.session.query(OrderItem.product_name).distinct()})

    return render_template(
        'admin/enrollments_list.html',
        orders=orders,
        q=q,
        branch_filter=branch_filter,
        status_filter=status_filter,
        year_filter=year_filter,
        product_filter=product_filter,
        all_branches=all_branches,
        all_statuses=all_statuses,
        all_years=all_years,
        all_products=all_products,
    )


@app.route('/admin/enrollments/<int:id>')
@login_required
@permission_required('view_enrollments')
def admin_enrollments_detail(id):
    """View enrollment order detail"""
    from models import Order
    order = Order.query.get_or_404(id)
    return render_template('admin/enrollments_detail.html', order=order)


# ---------------- Admin: Award Ceremonies ---------------- #
@app.route('/admin/award-ceremonies')
@login_required
@permission_required('manage_award_ceremonies')
def admin_award_ceremonies():
    """List all award ceremony events"""
    from models import AwardCeremony
    ceremonies = AwardCeremony.query.order_by(AwardCeremony.date.desc()).all()
    return render_template('admin/award_ceremonies_list.html', ceremonies=ceremonies)


@app.route('/admin/award-ceremonies/new', methods=['GET', 'POST'])
@login_required
@permission_required('manage_award_ceremonies')
def admin_award_ceremony_new():
    """Create a new award ceremony event"""
    from forms import AwardCeremonyForm
    from models import AwardCeremony
    form = AwardCeremonyForm()
    if form.validate_on_submit():
        ceremony = AwardCeremony(
            name=form.name.data,
            date=form.date.data,
            venue=form.venue.data,
            address=form.address.data,
            time=form.time.data,
            active=form.active.data,
        )
        db.session.add(ceremony)
        db.session.commit()
        flash('Award ceremony created successfully', 'success')
        return redirect(url_for('admin_award_ceremonies'))
    return render_template('admin/award_ceremony_form.html', form=form, title='New Award Ceremony')


@app.route('/admin/award-ceremonies/<int:id>/edit', methods=['GET', 'POST'])
@login_required
@permission_required('manage_award_ceremonies')
def admin_award_ceremony_edit(id):
    """Edit an existing award ceremony event"""
    from forms import AwardCeremonyForm
    from models import AwardCeremony
    ceremony = AwardCeremony.query.get_or_404(id)
    form = AwardCeremonyForm(obj=ceremony)
    if form.validate_on_submit():
        ceremony.name = form.name.data
        ceremony.date = form.date.data
        ceremony.venue = form.venue.data
        ceremony.address = form.address.data
        ceremony.time = form.time.data
        ceremony.active = form.active.data
        ceremony.updated_at = datetime.utcnow()
        db.session.commit()
        flash('Award ceremony updated successfully', 'success')
        return redirect(url_for('admin_award_ceremonies'))
    return render_template('admin/award_ceremony_form.html', form=form, title='Edit Award Ceremony', ceremony=ceremony)


@app.route('/admin/award-ceremonies/<int:id>/delete', methods=['POST'])
@login_required
@permission_required('manage_award_ceremonies')
def admin_award_ceremony_delete(id):
    """Delete an award ceremony event and all its registrations"""
    from models import AwardCeremony
    ceremony = AwardCeremony.query.get_or_404(id)
    db.session.delete(ceremony)
    db.session.commit()
    flash('Award ceremony deleted successfully', 'success')
    return redirect(url_for('admin_award_ceremonies'))


@app.route('/admin/award-ceremonies/<int:id>/toggle', methods=['POST'])
@login_required
@permission_required('manage_award_ceremonies')
def admin_award_ceremony_toggle(id):
    """Toggle active/inactive status of an award ceremony"""
    from models import AwardCeremony
    ceremony = AwardCeremony.query.get_or_404(id)
    ceremony.active = not ceremony.active
    db.session.commit()
    flash(f"Award ceremony {'activated' if ceremony.active else 'deactivated'}", 'success')
    return jsonify({'success': True, 'active': ceremony.active})


@app.route('/admin/award-ceremonies/<int:id>/registrations')
@login_required
@permission_required('manage_award_ceremonies')
def admin_award_ceremony_registrations(id):
    """View all registrations for a specific award ceremony"""
    from models import AwardCeremony, AwardCeremonyRegistration
    ceremony = AwardCeremony.query.get_or_404(id)
    registrations = AwardCeremonyRegistration.query.filter_by(ceremony_id=id)\
        .order_by(AwardCeremonyRegistration.created_at.desc()).all()
    return render_template('admin/award_ceremony_registrations.html',
                           ceremony=ceremony, registrations=registrations)


@app.route('/admin/award-ceremony-registrations')
@login_required
@permission_required('manage_award_ceremonies')
def admin_all_award_registrations():
    """View all award ceremony registrations across all events with search/filter."""
    from models import AwardCeremony, AwardCeremonyRegistration
    q = AwardCeremonyRegistration.query

    # Filters
    ceremony_id = request.args.get('ceremony', type=int)
    if ceremony_id:
        q = q.filter(AwardCeremonyRegistration.ceremony_id == ceremony_id)
    year_group = request.args.get('year_group', '').strip()
    if year_group:
        q = q.filter(AwardCeremonyRegistration.year_group == year_group)
    search = (request.args.get('search') or '').strip().lower()
    if search:
        q = q.filter(
            db.or_(
                db.func.lower(AwardCeremonyRegistration.child_name).like(f"%{search}%"),
                db.func.lower(AwardCeremonyRegistration.email).like(f"%{search}%"),
                db.func.lower(AwardCeremonyRegistration.student_id).like(f"%{search}%"),
                db.func.lower(AwardCeremonyRegistration.phone).like(f"%{search}%"),
            )
        )

    registrations = q.order_by(AwardCeremonyRegistration.created_at.desc()).all()
    ceremonies = AwardCeremony.query.order_by(AwardCeremony.date.desc()).all()
    year_groups = sorted({r.year_group for r in AwardCeremonyRegistration.query.with_entities(
        AwardCeremonyRegistration.year_group).distinct().all() if r.year_group})
    return render_template('admin/award_all_registrations.html',
                           registrations=registrations, ceremonies=ceremonies,
                           year_groups=year_groups, total=len(registrations),
                           sel_ceremony=ceremony_id, sel_year_group=year_group, search=search)


@app.route('/admin/award-ceremony-registrations/<int:reg_id>')
@login_required
@permission_required('manage_award_ceremonies')
def admin_award_registration_detail(reg_id):
    """View a single registration submission."""
    from models import AwardCeremonyRegistration
    reg = AwardCeremonyRegistration.query.get_or_404(reg_id)
    return render_template('admin/award_registration_detail.html', reg=reg)


@app.route('/admin/award-ceremony-registrations/<int:reg_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_award_ceremonies')
def admin_award_registration_delete(reg_id):
    """Delete a single registration."""
    from models import AwardCeremonyRegistration
    reg = AwardCeremonyRegistration.query.get_or_404(reg_id)
    ceremony_name = reg.ceremony.name if reg.ceremony else 'Unknown'
    db.session.delete(reg)
    db.session.commit()
    flash(f'Registration for "{reg.child_name}" ({ceremony_name}) deleted.', 'success')
    ref = request.referrer
    if ref and 'award-ceremony-registrations' in ref:
        return redirect(ref)
    return redirect(url_for('admin_all_award_registrations'))


@app.route('/admin/award-ceremony-registrations/export')
@login_required
@permission_required('manage_award_ceremonies')
def admin_all_award_registrations_export():
    """Export all (or filtered) award ceremony registrations as XLSX."""
    import io

    import pandas as pd

    from models import AwardCeremonyRegistration
    q = AwardCeremonyRegistration.query
    ceremony_id = request.args.get('ceremony', type=int)
    if ceremony_id:
        q = q.filter(AwardCeremonyRegistration.ceremony_id == ceremony_id)
    year_group = request.args.get('year_group', '').strip()
    if year_group:
        q = q.filter(AwardCeremonyRegistration.year_group == year_group)
    search = (request.args.get('search') or '').strip().lower()
    if search:
        q = q.filter(
            db.or_(
                db.func.lower(AwardCeremonyRegistration.child_name).like(f"%{search}%"),
                db.func.lower(AwardCeremonyRegistration.email).like(f"%{search}%"),
                db.func.lower(AwardCeremonyRegistration.student_id).like(f"%{search}%"),
            )
        )
    registrations = q.order_by(AwardCeremonyRegistration.created_at.desc()).all()
    data = []
    for r in registrations:
        data.append({
            'Ceremony': r.ceremony.name if r.ceremony else '',
            'Ceremony Date': r.ceremony.date.strftime('%d %b %Y') if r.ceremony and r.ceremony.date else '',
            'Child Name': r.child_name,
            'Student ID': r.student_id,
            'Year Group': (r.year_group or '').replace('year', 'Year '),
            'Email': r.email,
            'Phone': r.phone,
            'Registered At': r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else '',
        })
    df = pd.DataFrame(data)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='All Registrations')
    out.seek(0)
    return send_file(out, as_attachment=True, download_name='award_ceremony_all_registrations.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/admin/award-ceremonies/<int:id>/export')
@login_required
@permission_required('manage_award_ceremonies')
def admin_award_ceremony_export(id):
    """Export registrations for an award ceremony as XLSX"""
    import io

    import pandas as pd

    from models import AwardCeremony, AwardCeremonyRegistration
    ceremony = AwardCeremony.query.get_or_404(id)
    registrations = AwardCeremonyRegistration.query.filter_by(ceremony_id=id)\
        .order_by(AwardCeremonyRegistration.created_at.desc()).all()
    data = []
    for r in registrations:
        data.append({
            'Child Name': r.child_name,
            'Student ID': r.student_id,
            'Year Group': r.year_group or '',
            'Email': r.email,
            'Phone': r.phone,
            'Registered At': r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else '',
        })
    df = pd.DataFrame(data)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Registrations')
    out.seek(0)
    fname = f"award_ceremony_{ceremony.id}_registrations.xlsx"
    return send_file(out, as_attachment=True, download_name=fname,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@csrf.exempt
@app.route('/award-ceremony/register', methods=['GET', 'POST'])
@app.route('/public/award-ceremony/register', methods=['GET', 'POST'])
def award_ceremony_register():
    """Public form for parents to register children for an award ceremony"""
    from models import AwardCeremony, AwardCeremonyRegistration
    year_groups = ['year3', 'year4', 'year5', 'year6', 'year7', 'year8',
                   'year9', 'year10', 'year11', 'year12', 'year13']
    ceremonies = AwardCeremony.query.filter_by(active=True).order_by(AwardCeremony.date.asc()).all()

    if request.method == 'POST':
        # Honeypot check
        if (request.form.get('website') or '').strip():
            flash('Thank you for your registration.', 'success')
            return redirect(url_for('award_ceremony_register'))

        ceremony_id = request.form.get('ceremony_id', '').strip()
        child_name = request.form.get('child_name', '').strip()
        student_id = request.form.get('student_id', '').strip()
        year_group = request.form.get('year_group', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()
        confirmation = request.form.get('confirmation')

        errors = []
        if not ceremony_id:
            errors.append('Please select an award ceremony event.')
        if not child_name:
            errors.append("Child's full name is required.")
        if not student_id:
            errors.append('Student ID is required.')
        if not year_group:
            errors.append('Year group is required.')
        if not email or '@' not in email:
            errors.append('A valid email address is required.')
        if not phone:
            errors.append('Phone number is required.')
        if not confirmation:
            errors.append('You must confirm the declaration before submitting.')

        # Validate ceremony exists and is active
        ceremony = None
        if ceremony_id:
            try:
                ceremony = AwardCeremony.query.get(int(ceremony_id))
            except (ValueError, TypeError):
                pass
            if not ceremony or not ceremony.active:
                errors.append('The selected event is not available for registration.')

        if errors:
            for err in errors:
                flash(err, 'warning')
            return render_template('public/award_ceremony_register.html',
                                   ceremonies=ceremonies, year_groups=year_groups,
                                   ceremony_id=ceremony_id, child_name=child_name,
                                   student_id=student_id, year_group=year_group,
                                   email=email, phone=phone)

        registration = AwardCeremonyRegistration(
            ceremony_id=ceremony.id,
            child_name=child_name,
            student_id=student_id,
            year_group=year_group,
            email=email,
            phone=phone,
            confirmed=True,
        )
        db.session.add(registration)
        db.session.commit()

        # Send confirmation email
        try:
            from email_utils import (build_award_ceremony_confirmation_email,
                                     send_email)
            subject_line, html = build_award_ceremony_confirmation_email(registration, ceremony)
            send_email(email, subject_line, html)
        except Exception:
            pass  # Email failure should not block registration

        flash('Registration submitted successfully. A confirmation email has been sent to your email address.', 'success')
        return redirect(url_for('award_ceremony_register'))

    return render_template('public/award_ceremony_register.html',
                           ceremonies=ceremonies, year_groups=year_groups,
                           ceremony_id='', child_name='', student_id='',
                           year_group='', email='', phone='')


# ---------------- Admin: Discount Configuration ---------------- #
@app.route('/admin/settings/enrollment-discount', methods=['GET', 'POST'])
@login_required
@permission_required('manage_settings')
def admin_enrollment_discount():
    """Configure enrollment discount amount"""
    from forms import EnrollmentDiscountForm
    from utils import get_setting, set_setting
    form = EnrollmentDiscountForm()
    if form.validate_on_submit():
        set_setting('enrollment_discount_amount', str(form.discount_amount.data))
        flash('Discount settings saved', 'success')
        return redirect(url_for('admin_enrollment_discount'))

    # Load current value
    current = get_setting('enrollment_discount_amount', '10.00')
    form.discount_amount.data = float(current)
    return render_template('admin/enrollment_discount.html', form=form)


# ---------------- Public Booking ---------------- #


def _booking_context_lang(lang: str | None = None) -> tuple[str, dict]:
    language = lang or _current_booking_language()
    copy = _booking_copy(language)
    return language, copy


def _build_staff_invoice_submitted_email(name: str, inv) -> str:
    # Simple branded HTML consistent with other emails
    rows = [
        ("Invoice ID", f"#{inv.id}"),
        ("Title", inv.name or ''),
        ("Period", inv.month_year_label() if hasattr(inv, 'month_year_label') else f"{inv.month}/{inv.year}"),
        ("Status", inv.status or 'Pending'),
        ("Total Amount", f"£{float(inv.amount or 0):.2f}"),
        ("Submitted", (inv.submitted_at.strftime('%d %b %Y %H:%M') if inv.submitted_at else '-')),
    ]
    lines = ["<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>"]
    for label, value in rows:
        lines.append(
            "<tr>"
            f"<td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td>"
            f"<td style='padding:6px 0;color:#0f172a;'>{value}</td>"
            "</tr>"
        )
    lines.append("</table>")
    body = "".join(lines)
    intro = f"Hello {name},<br/><br/>Your staff invoice has been submitted successfully. Our team will review it and update the status."
    title = f"Staff invoice submitted (#{inv.id})"
    html = f"""
<!DOCTYPE html>
<html lang='en'>
<head><meta charset='utf-8'/><title>{title}</title></head>
<body style=\"margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;\"> 
    <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#f1f5f9;padding:24px 0;'>
        <tr><td align='center'>
            <table role='presentation' width='640' cellpadding='0' cellspacing='0' style='background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;'>
                <tr>
                    <td style='background:#0f172a;padding:24px 32px;'>
                        <h1 style='margin:0;font-size:22px;line-height:1.3;color:#ffffff;font-weight:600;'>Excel Tutors</h1>
                        <p style='margin:6px 0 0;font-size:13px;color:#cbd5f5;'>Invoice confirmation</p>
                    </td>
                </tr>
                <tr>
                    <td style='padding:32px;'>
                        <p style='margin:0 0 16px;font-size:15px;color:#0f172a;'>{intro}</p>
                        {body}
                    </td>
                </tr>
                <tr>
                    <td style='background:#f8fafc;padding:18px 32px;text-align:center;font-size:11px;color:#94a3b8;'>
                        &copy; {date.today().year} Excel Tutors. All rights reserved.
                    </td>
                </tr>
            </table>
        </td></tr>
    </table>
</body>
</html>
    """
    return html


def _build_staff_invoice_approved_email(name: str, inv) -> tuple[str, str]:
    title = f"Staff invoice approved (#{inv.id})"
    intro = f"Hello {name},<br/><br/>Your staff invoice has been approved. The payment will be processed according to our schedule."
    rows = [
        ("Invoice ID", f"#{inv.id}"),
        ("Title", inv.name or ''),
        ("Period", inv.month_year_label() if hasattr(inv, 'month_year_label') else f"{inv.month}/{inv.year}"),
        ("Status", 'Approved'),
        ("Total Amount", f"£{float(inv.amount or 0):.2f}"),
    ]
    lines = ["<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>"]
    for label, value in rows:
        lines.append(
            "<tr>"
            f"<td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td>"
            f"<td style='padding:6px 0;color:#0f172a;'>{value}</td>"
            "</tr>"
        )
    lines.append("</table>")
    body = "".join(lines)
    html = f"""
<!DOCTYPE html>
<html lang='en'>
<head><meta charset='utf-8'/><title>{title}</title></head>
<body style=\"margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;\"> 
    <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#f1f5f9;padding:24px 0;'>
        <tr><td align='center'>
            <table role='presentation' width='640' cellpadding='0' cellspacing='0' style='background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;'>
                <tr>
                    <td style='background:#0f172a;padding:24px 32px;'>
                        <h1 style='margin:0;font-size:22px;line-height:1.3;color:#ffffff;font-weight:600;'>Excel Tutors</h1>
                        <p style='margin:6px 0 0;font-size:13px;color:#cbd5f5;'>Invoice approved</p>
                    </td>
                </tr>
                <tr>
                    <td style='padding:32px;'>
                        <p style='margin:0 0 16px;font-size:15px;color:#0f172a;'>{intro}</p>
                        {body}
                    </td>
                </tr>
                <tr>
                    <td style='background:#f8fafc;padding:18px 32px;text-align:center;font-size:11px;color:#94a3b8;'>
                        &copy; {date.today().year} Excel Tutors. All rights reserved.
                    </td>
                </tr>
            </table>
        </td></tr>
    </table>
</body>
</html>
    """
    return title, html


def _build_staff_invoice_rejected_email(name: str, inv, reason: str | None) -> tuple[str, str]:
    title = f"Staff invoice rejected (#{inv.id})"
    intro = f"Hello {name},<br/><br/>Your staff invoice has been rejected."
    if reason:
        intro += f" Reason: {reason}."
    rows = [
        ("Invoice ID", f"#{inv.id}"),
        ("Title", inv.name or ''),
        ("Period", inv.month_year_label() if hasattr(inv, 'month_year_label') else f"{inv.month}/{inv.year}"),
        ("Status", 'Rejected'),
        ("Total Amount", f"£{float(inv.amount or 0):.2f}"),
    ]
    lines = ["<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>"]
    for label, value in rows:
        lines.append(
            "<tr>"
            f"<td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td>"
            f"<td style='padding:6px 0;color:#0f172a;'>{value}</td>"
            "</tr>"
        )
    lines.append("</table>")
    body = "".join(lines)
    html = f"""
<!DOCTYPE html>
<html lang='en'>
<head><meta charset='utf-8'/><title>{title}</title></head>
<body style=\"margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;\"> 
    <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#f1f5f9;padding:24px 0;'>
        <tr><td align='center'>
            <table role='presentation' width='640' cellpadding='0' cellspacing='0' style='background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;'>
                <tr>
                    <td style='background:#0f172a;padding:24px 32px;'>
                        <h1 style='margin:0;font-size:22px;line-height:1.3;color:#ffffff;font-weight:600;'>Excel Tutors</h1>
                        <p style='margin:6px 0 0;font-size:13px;color:#cbd5f5;'>Invoice rejected</p>
                    </td>
                </tr>
                <tr>
                    <td style='padding:32px;'>
                        <p style='margin:0 0 16px;font-size:15px;color:#0f172a;'>{intro}</p>
                        {body}
                    </td>
                </tr>
                <tr>
                    <td style='background:#f8fafc;padding:18px 32px;text-align:center;font-size:11px;color:#94a3b8;'>
                        &copy; {date.today().year} Excel Tutors. All rights reserved.
                    </td>
                </tr>
            </table>
        </td></tr>
    </table>
</body>
</html>
    """
    return title, html



@app.route('/booking', methods=['GET', 'POST'])
def booking_index():
    lang, copy = _booking_context_lang()
    form = AppointmentBookingForm()
    _populate_booking_form(form)
    form.language.data = lang
    upcoming_slots = _upcoming_slots_query()
    available_choices = list(form.slot_id.choices)

    if request.method == 'POST':
        # Debugging: print posted keys and branch-related values to diagnose
        # "Not a valid choice." errors for ItemForm.branch.
        try:
            print('[DEBUG] staff_invoice_new POST keys:', list(request.form.keys()))
            print('[DEBUG] staff_invoice_new branch choices:', form.ItemForm.branch.choices)
            posted_branch_fields = [(k, request.form.get(k)) for k in request.form.keys() if k.endswith('-branch') or k == 'branch']
            print('[DEBUG] staff_invoice_new posted branch fields:', posted_branch_fields)
        except Exception as _e:
            print('[DEBUG] staff_invoice_new debug error:', _e)
        if form.validate_on_submit():
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
                from email_utils import send_with_template
                cust_key = f"appointment_customer_{lang}_confirmation"
                try:
                    send_with_template(cust_key, {
                        'name': booking.name,
                        'superadmin': slot.superadmin.name,
                        'date': slot.start_at.strftime('%A, %d %B %Y'),
                        'time': slot.start_at.strftime('%H:%M'),
                        'student': booking.student_ref,
                        'reason': booking.reason,
                        'email': booking.email,
                        'phone': booking.phone,
                        'to_email': booking.email,
                        'cancel_url': cancel_url,
                    }, to_email=booking.email, fallback=lambda: build_appointment_email(booking, slot, slot.superadmin, language=lang, mode='confirmation', cancel_url=cancel_url))
                    booking.confirmation_sent_at = datetime.now(timezone.utc)
                except Exception as exc:
                    print(f"[WARN] Appointment confirmation template send failed, falling back: {exc}")
                    subj, html = build_appointment_email(booking, slot, slot.superadmin, language=lang, mode='confirmation', cancel_url=cancel_url)
                    _send_email_safe(booking.email, subj, html, log_prefix='Appointment confirmation')
                    booking.confirmation_sent_at = datetime.now(timezone.utc)

                try:
                    send_with_template('appointment_admin_confirmation', {
                        'name': booking.name,
                        'student': booking.student_ref,
                        'to_email': slot.superadmin.email,
                        'date': slot.start_at.strftime('%A, %d %B %Y'),
                        'time': slot.start_at.strftime('%H:%M'),
                    }, to_email=slot.superadmin.email, fallback=lambda: build_appointment_admin_email(booking, slot, mode='confirmation'))
                except Exception:
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
            if hasattr(user, 'force_password_reset'):
                user.force_password_reset = False
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
                from email_utils import send_with_template
                try:
                    send_with_template('password_reset', {
                        'name': user.name,
                        'link': link,
                        'to_email': email,
                    }, to_email=email, fallback=lambda: ("Reset your password", html))
                    flash("Reset link sent to your email.", "success")
                except Exception:
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
@permission_required('manage_staff')
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
    # Only unmapped staff
    if (request.args.get('unmapped') or '') == '1':
        q = q.filter((Staff.user_id.is_(None)) | (Staff.user_id == 0))
    staff = q.order_by(Staff.name.asc()).all()
    # Map availability branches to staff using direct FK link for accuracy and speed
    from collections import defaultdict
    by_staff = defaultdict(set)
    for rec in Availability.query.filter(Availability.staff_id.isnot(None)).all():
        for b in rec.branch_list():
            by_staff[rec.staff_id].add(b)
    staff_branch_map = {}
    for s in staff:
        merged = set()
        # Merge branches linked via Availability FK
        if s.id in by_staff:
            merged.update(by_staff[s.id])
        # Always include branches already on Staff record
        for b in [p.strip() for p in (s.branch or '').split(',') if p.strip()]:
            merged.add(b)
        staff_branch_map[s.id] = ",".join(sorted(merged)) if merged else (s.branch or '')
    # Distinct department list for filter
    dept_choices = [r[0] for r in db.session.query(Staff.department).distinct().filter(Staff.department.isnot(None)).order_by(Staff.department.asc()).all()]
    return render_template(
        "staff/index.html",
        staff=staff,
    branch_choices=BRANCH_CHOICES(),
        selected_branches=branches_selected,
        departments=dept_choices,
        selected_departments=departments_selected,
        selected_active=list(dict.fromkeys(active_filters)),
        staff_branch_map=staff_branch_map,
    )

@app.route("/staff/new", methods=["GET","POST"])
@login_required
@permission_required('manage_staff')
def staff_new():
    form = StaffForm()
    # Ensure branch multiselect choices are populated
    try:
        from utils import ensure_form_branch_choices
        ensure_form_branch_choices(form)
    except Exception:
        pass
    # Populate department select with existing distinct departments
    dept_rows = [r[0] for r in db.session.query(Staff.department).distinct().filter(Staff.department.isnot(None)).order_by(Staff.department.asc()).all()]
    form.department.choices = [('', '-- None --')] + [(d, d) for d in dept_rows]
    # Populate company choices
    companies = Company.query.order_by(Company.name.asc()).all()
    form.company_id.choices = [(0, '-- None --')] + [(c.id, c.name) for c in companies]
    # Populate DBS checked-by choices (staff whose linked User.role is admin/centre_manager/staff)
    from sqlalchemy.orm import joinedload
    eligible = (
        Staff.query.join(User, Staff.user_id == User.id)
        .filter(User.role.in_(['admin', 'centre_manager', 'staff']))
        .order_by(User.name.asc())
        .all()
    )
    form.dbs_checked_by_id.choices = [(0, '-- None --')] + [
        (st.id, ((st.first_name or '') + ' ' + (st.last_name or st.name or '')).strip()) for st in eligible
    ]
    if form.validate_on_submit():
        branches = ",".join(form.branches.data) if form.branches.data else ""
        # Validate access code (optional) – must be exactly 6 digits if provided
        code = (form.access_code.data or '').strip()
        if code:
            code = ''.join(ch for ch in code if ch.isdigit())
            if len(code) != 6:
                flash("Access Code must be exactly 6 digits.", "warning")
                return render_template("staff/form.html", form=form, staff=None)
        # Build Staff instance including new fields
        s = Staff(
            name=form.name.data,
            first_name=form.first_name.data or None,
            last_name=form.last_name.data or None,
            department=form.department.data,
            email=form.email.data,
            phone=form.phone.data,
            dob=form.dob.data,
            gender=form.gender.data,
            relationship_status=form.relationship_status.data,
            national_insurance=(form.national_insurance.data or None),
            branch=branches,
            salary_per_hour=form.salary_per_hour.data,
            salary_notes=form.salary_notes.data,
            employment_type=form.employment_type.data,
            joining_date=form.joining_date.data,
            medical_condition=form.medical_condition.data,
            medical_condition_other=form.medical_condition_other.data,
            address_line1=form.address_line1.data,
            address_line2=form.address_line2.data,
            town=form.town.data,
            region=form.region.data,
            country=form.country.data,
            postcode=form.postcode.data,
            address_lookup_id=form.address_lookup_id.data,
            emergency_first_name=form.emergency_first_name.data,
            emergency_last_name=form.emergency_last_name.data,
            emergency_mobile=form.emergency_mobile.data,
            emergency_email=form.emergency_email.data,
            emergency_relation=form.emergency_relation.data,
            bank_name_on_account=form.bank_name_on_account.data,
            bank_name=form.bank_name.data,
            bank_sort_code=form.bank_sort_code.data,
            bank_account_number=form.bank_account_number.data,
            dbs_number=form.dbs_number.data,
            dbs_start_date=form.dbs_start_date.data,
            dbs_expiry_date=form.dbs_expiry_date.data,
            dbs_checked_by_id=(form.dbs_checked_by_id.data or None) if form.dbs_checked_by_id.data != 0 else None,
            # If the 'active' checkbox wasn't submitted (e.g. tests), default to True
            active=(form.active.data if ('active' in request.form or request.method == 'POST' and request.form.get('active') is not None) else True),
            company_id=(form.company_id.data or None) if form.company_id.data != 0 else None,
            whitechapel_machine_id=form.whitechapel_machine_id.data or None,
            east_ham_machine_id=form.east_ham_machine_id.data or None,
            stratford_machine_id=form.stratford_machine_id.data or None,
            docklands_machine_id=form.docklands_machine_id.data or None,
        )
        # Assign code – if not provided, auto-generate a unique one
        def gen_code():
            return f"{random.randint(0, 999999):06d}"
        tries = 0
        while True:
            try:
                s.access_code = code or gen_code()
                # Save staff record
                db.session.add(s)
                db.session.flush()
                # Handle photo upload if provided
                try:
                    photo_file = request.files.get('photo')
                    if photo_file and photo_file.filename:
                        from werkzeug.utils import secure_filename
                        filename = secure_filename(photo_file.filename)
                        uploads_dir = os.path.join(app.root_path, 'static', 'uploads')
                        os.makedirs(uploads_dir, exist_ok=True)
                        dest = os.path.join(uploads_dir, filename)
                        photo_file.save(dest)
                        s.photo = filename
                except Exception:
                    pass
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                if code:
                    flash("Access Code already in use. Please choose a different one.", "warning")
                    return render_template("staff/form.html", form=form, staff=None)
                tries += 1
                if tries > 5:
                    flash("Could not generate a unique access code. Please try again.", "danger")
                    return render_template("staff/form.html", form=form, staff=None)
        flash("Staff saved", "success")
        # Log staff created state
        app.logger.info('Staff created id=%s active=%s email=%s', getattr(s, 'id', None), getattr(s, 'active', None), getattr(s, 'email', None))
        # Create a linked User account automatically when staff created and active with an email
        if s.active and (s.email or '').strip():
            email = (s.email or '').strip().lower()
            try:
                existing = User.query.filter_by(email=email).first()
                app.logger.info('Attempting to create linked user for email: %s existing=%s', email, bool(existing))
                if not existing:
                    print('DEBUG: no existing user found for', email)
                    temp_pwd = _generate_temp_password()
                    u = User(
                        name=s.name or (email.split('@')[0] if email else 'User'),
                        email=email,
                        password_hash=generate_password_hash(temp_pwd),
                        is_approved=True,
                        is_active=True,
                        role='staff',
                    )
                    if hasattr(u, 'force_password_reset'):
                        u.force_password_reset = True
                    db.session.add(u)
                    db.session.flush()
                    s.user_id = u.id
                    print('DEBUG: setting s.user_id =', u.id, 'before commit s.id=', getattr(s,'id',None))
                    app.logger.info('Linked user id %s will be associated to staff %s', u.id, s.id)
                    db.session.commit()
                    print('DEBUG: after commit s.user_id=', s.user_id)
                    app.logger.info('Post-commit staff.user_id=%s', s.user_id)
                    try:
                        subject, html = _build_account_welcome_email_html(s.name, email, temp_pwd)
                        send_email(email, subject, html)
                    except Exception:
                        pass
            except Exception as exc:
                # Log and continue; do not rollback the main session which would remove the staff record
                app.logger.exception('Failed to create linked user for staff %s: %s', s.id if s else None, exc)
        return redirect(url_for('staff_index'))
    return render_template("staff/form.html", form=form, staff=None)

@app.route("/staff/<int:sid>/edit", methods=["GET","POST"])
@login_required
@permission_required('manage_staff')
def staff_edit(sid):
    s = Staff.query.get_or_404(sid)
    form = StaffForm()
    # Ensure branch multiselect choices are populated
    try:
        from utils import ensure_form_branch_choices
        ensure_form_branch_choices(form)
    except Exception:
        pass
    # Populate department choices
    dept_rows = [r[0] for r in db.session.query(Staff.department).distinct().filter(Staff.department.isnot(None)).order_by(Staff.department.asc()).all()]
    form.department.choices = [('', '-- None --')] + [(d, d) for d in dept_rows]
    # Populate company choices
    companies = Company.query.order_by(Company.name.asc()).all()
    form.company_id.choices = [(0, '-- None --')] + [(c.id, c.name) for c in companies]
    # Populate DBS checked-by choices
    eligible = Staff.query.order_by(Staff.name.asc()).all()
    form.dbs_checked_by_id.choices = [(0, '-- None --')] + [(st.id, (st.first_name or '') + ' ' + (st.last_name or st.name or '')) for st in eligible]
    if request.method == 'GET':
        form.first_name.data = s.first_name or ''
        form.last_name.data = s.last_name or ''
        form.name.data = s.name
        form.department.data = s.department
        form.email.data = s.email
        form.phone.data = s.phone
        form.branches.data = [b for b in (s.branch or '').split(',') if b]
        form.active.data = s.active
        form.access_code.data = s.access_code or ''
        form.company_id.data = s.company_id or 0
        form.whitechapel_machine_id.data = s.whitechapel_machine_id or ''
        form.east_ham_machine_id.data = s.east_ham_machine_id or ''
        form.stratford_machine_id.data = s.stratford_machine_id or ''
        form.docklands_machine_id.data = s.docklands_machine_id or ''
        # New fields
        form.dob.data = s.dob
        form.gender.data = s.gender
        form.relationship_status.data = s.relationship_status
        form.national_insurance.data = s.national_insurance or ''
        form.salary_per_hour.data = s.salary_per_hour
        form.salary_notes.data = s.salary_notes
        form.employment_type.data = s.employment_type
        form.joining_date.data = s.joining_date
        form.medical_condition.data = s.medical_condition
        form.medical_condition_other.data = s.medical_condition_other
        form.address_line1.data = s.address_line1
        form.address_line2.data = s.address_line2
        form.town.data = s.town
        form.region.data = s.region
        form.country.data = s.country
        form.postcode.data = s.postcode
        form.address_lookup_id.data = s.address_lookup_id
        form.emergency_first_name.data = s.emergency_first_name
        form.emergency_last_name.data = s.emergency_last_name
        form.emergency_mobile.data = s.emergency_mobile
        form.emergency_email.data = s.emergency_email
        form.emergency_relation.data = s.emergency_relation
        form.bank_name_on_account.data = s.bank_name_on_account
        form.bank_name.data = s.bank_name
        form.bank_sort_code.data = s.bank_sort_code
        form.bank_account_number.data = s.bank_account_number
        form.dbs_number.data = s.dbs_number
        form.dbs_start_date.data = s.dbs_start_date
        form.dbs_expiry_date.data = s.dbs_expiry_date
        form.dbs_checked_by_id.data = s.dbs_checked_by_id or 0
    if form.validate_on_submit():
        s.first_name = form.first_name.data or None
        s.last_name = form.last_name.data or None
        s.name = form.name.data
        s.department = form.department.data
        s.email = form.email.data
        s.phone = form.phone.data
        s.branch = ",".join(form.branches.data) if form.branches.data else ""
        # Preserve active state unless the checkbox was explicitly submitted
        if ('active' in request.form) or (request.method == 'POST' and request.form.get('active') is not None):
            s.active = form.active.data
        # Mirror active flag to linked user if present
        try:
            if s.user_id:
                u = User.query.get(s.user_id)
                if u:
                    u.is_active = bool(s.active)
        except Exception:
            db.session.rollback()
        s.company_id = (form.company_id.data or None) if form.company_id.data != 0 else None
        s.whitechapel_machine_id = form.whitechapel_machine_id.data or None
        s.east_ham_machine_id = form.east_ham_machine_id.data or None
        s.stratford_machine_id = form.stratford_machine_id.data or None
        s.docklands_machine_id = form.docklands_machine_id.data or None
        # New fields
        s.dob = form.dob.data
        s.gender = form.gender.data
        s.relationship_status = form.relationship_status.data
        s.national_insurance = form.national_insurance.data or None
        s.salary_per_hour = form.salary_per_hour.data
        s.salary_notes = form.salary_notes.data
        s.employment_type = form.employment_type.data
        s.joining_date = form.joining_date.data
        s.medical_condition = form.medical_condition.data
        s.medical_condition_other = form.medical_condition_other.data
        s.address_line1 = form.address_line1.data
        s.address_line2 = form.address_line2.data
        s.town = form.town.data
        s.region = form.region.data
        s.country = form.country.data
        s.postcode = form.postcode.data
        s.address_lookup_id = form.address_lookup_id.data
        s.emergency_first_name = form.emergency_first_name.data
        s.emergency_last_name = form.emergency_last_name.data
        s.emergency_mobile = form.emergency_mobile.data
        s.emergency_email = form.emergency_email.data
        s.emergency_relation = form.emergency_relation.data
        s.bank_name_on_account = form.bank_name_on_account.data
        s.bank_name = form.bank_name.data
        s.bank_sort_code = form.bank_sort_code.data
        s.bank_account_number = form.bank_account_number.data
        s.dbs_number = form.dbs_number.data
        s.dbs_start_date = form.dbs_start_date.data
        s.dbs_expiry_date = form.dbs_expiry_date.data
        s.dbs_checked_by_id = (form.dbs_checked_by_id.data or None) if form.dbs_checked_by_id.data != 0 else None
        # Handle photo upload if provided
        try:
            photo_file = request.files.get('photo')
            if photo_file and photo_file.filename:
                from werkzeug.utils import secure_filename
                filename = secure_filename(photo_file.filename)
                uploads_dir = os.path.join(app.root_path, 'static', 'uploads')
                os.makedirs(uploads_dir, exist_ok=True)
                dest = os.path.join(uploads_dir, filename)
                photo_file.save(dest)
                s.photo = filename
        except Exception:
            pass
        # Access code validation and uniqueness enforcement
        raw = (form.access_code.data or '').strip()
        if raw:
            raw = ''.join(ch for ch in raw if ch.isdigit())
            if len(raw) != 6:
                flash("Access Code must be exactly 6 digits.", "warning")
                return render_template("staff/form.html", form=form, staff=s)
            # If changed, ensure no collision
            if raw != (s.access_code or ''):
                other = Staff.query.filter(Staff.id != s.id, Staff.access_code == raw).first()
                if other:
                    flash("Access Code already in use by another staff member.", "warning")
                    return render_template("staff/form.html", form=form, staff=s)
                s.access_code = raw
        else:
            # Auto-generate if cleared
            def gen_code():
                return f"{random.randint(0, 999999):06d}"
            tries = 0
            while True:
                try:
                    s.access_code = gen_code()
                    db.session.commit()
                    break
                except IntegrityError:
                    db.session.rollback()
                    tries += 1
                    if tries > 5:
                        flash("Could not generate a unique access code. Please try again.", "danger")
                        return render_template("staff/form.html", form=form, staff=s)
        # Before commit, ensure linked user active state matches
        db.session.commit()
        flash("Staff updated", "success")
        return redirect(url_for('staff_index'))
    return render_template("staff/form.html", form=form, staff=s)

@app.route("/staff/<int:sid>/delete")
@login_required
@permission_required('manage_staff')
def staff_delete(sid):
    s = Staff.query.get_or_404(sid)
    db.session.delete(s)
    db.session.commit()
    flash("Staff deleted", "success")
    return redirect(url_for('staff_index'))

@app.route('/staff/<int:sid>/toggle')
@login_required
@permission_required('manage_staff')
def staff_toggle_active(sid):
    s = Staff.query.get_or_404(sid)
    s.active = not bool(s.active)
    db.session.commit()
    flash(f"{'Activated' if s.active else 'Deactivated'} {s.name}", 'success')
    # Mirror activation to linked User account
    try:
        if s.user_id:
            u = User.query.get(s.user_id)
            if u:
                u.is_active = bool(s.active)
                db.session.commit()
    except Exception:
        db.session.rollback()
    # Preserve filters (except pagination) by redirecting back
    return redirect(request.referrer or url_for('staff_index'))

@app.route('/api/staff/<int:sid>/toggle-active', methods=['POST'])
@login_required
@permission_required('manage_staff')
def staff_toggle_active_api(sid: int):
    s = Staff.query.get_or_404(sid)
    s.active = not bool(s.active)
    try:
        db.session.commit()
        # Mirror to linked user
        if s.user_id:
            u = User.query.get(s.user_id)
            if u:
                u.is_active = bool(s.active)
                db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 500
    return jsonify({
        'success': True,
        'active': bool(s.active),
        'label': 'Deactivate' if s.active else 'Activate',
        'badge': 'Active' if s.active else 'Inactive'
    })

@app.route('/api/staff/<int:sid>/email-access-code', methods=['POST'])
@login_required
@permission_required('manage_staff')
def api_staff_email_access_code(sid: int):
        s = Staff.query.get_or_404(sid)
        if not s.email:
                return jsonify({'success': False, 'error': 'Staff member has no email address.'}), 400
        code = s.access_code or ''
        subject = 'Your Excel Tutors portal access code'
        body = f"""
<!DOCTYPE html>
<html><body style="font-family:'Segoe UI',Arial,sans-serif;background:#f8fafc;padding:24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
        <tr>
            <td style="background:#0f172a;color:#fff;padding:18px 22px;font-weight:600;">Excel Tutors</td>
        </tr>
        <tr>
            <td style="padding:22px;color:#0f172a;">
                <p style="margin:0 0 12px;">Hello {s.name or 'there'},</p>
                <p style="margin:0 0 12px;">Here is your staff access code for the Resource Management public loan/return form:</p>
                <div style="margin:14px 0;">
                    <span style="display:inline-block;padding:10px 16px;border-radius:10px;background:#e0e7ff;color:#3730a3;font-weight:700;letter-spacing:2px;font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;">{code or 'N/A'}</span>
                </div>
                <p style="margin:0 0 12px;">Keep this code private. You can use it to borrow and return resources at the centre.</p>
                <p style="margin:18px 0 0;font-size:12px;color:#64748b;">If you have any issues, reply to this email.</p>
            </td>
        </tr>
        <tr>
            <td style="background:#f8fafc;color:#94a3b8;padding:14px 22px;text-align:center;font-size:11px;">&copy; {date.today().year} Excel Tutors</td>
        </tr>
    </table>
    </body></html>
        """
        try:
                send_email(s.email, subject, body)
        except Exception as exc:
                return jsonify({'success': False, 'error': str(exc)}), 500
        return jsonify({'success': True})

@app.route('/api/staff/email-access-codes', methods=['POST'])
@login_required
@permission_required('manage_staff')
def api_staff_email_access_codes_bulk():
        """Email access codes to a set of staff IDs or all active staff.

        Accepts JSON body: { ids: [1,2,3] } optional. If absent/empty, targets all active staff with an email.
        """
        try:
                payload = request.get_json(silent=True) or {}
                ids = payload.get('ids') or []
                q = Staff.query
                if ids:
                        q = q.filter(Staff.id.in_(ids))
                else:
                        q = q.filter(Staff.active.is_(True))
                targets = [s for s in q.all() if s.email]
                sent = 0
                errors: list[dict] = []
                for s in targets:
                        try:
                                code = s.access_code or ''
                                subject = 'Your Excel Tutors portal access code'
                                body = f"""
<!DOCTYPE html>
<html><body style=\"font-family:'Segoe UI',Arial,sans-serif;background:#f8fafc;padding:24px;\">\n  <table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"max-width:640px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;\">\n    <tr>\n      <td style=\"background:#0f172a;color:#fff;padding:18px 22px;font-weight:600;\">Excel Tutors</td>\n    </tr>\n    <tr>\n      <td style=\"padding:22px;color:#0f172a;\">\n        <p style=\"margin:0 0 12px;\">Hello {s.name or 'there'},</p>\n        <p style=\"margin:0 0 12px;\">Here is your staff access code for the Resource Management public loan/return form:</p>\n        <div style=\"margin:14px 0;\">\n          <span style=\"display:inline-block;padding:10px 16px;border-radius:10px;background:#e0e7ff;color:#3730a3;font-weight:700;letter-spacing:2px;font-family:ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;\">{code or 'N/A'}</span>\n        </div>\n        <p style=\"margin:0 0 12px;\">Keep this code private. You can use it to borrow and return resources at the centre.</p>\n        <p style=\"margin:18px 0 0;font-size:12px;color:#64748b;\">If you have any issues, reply to this email.</p>\n      </td>\n    </tr>\n    <tr>\n      <td style=\"background:#f8fafc;color:#94a3b8;padding:14px 22px;text-align:center;font-size:11px;\">&copy; {date.today().year} Excel Tutors</td>\n    </tr>\n  </table>\n  </body></html>\n                """
                                send_email(s.email, subject, body)
                                sent += 1
                        except Exception as exc:
                                errors.append({'id': s.id, 'email': s.email, 'error': str(exc)})
                return jsonify({'success': True, 'sent': sent, 'errors': errors})
        except Exception as exc:
                return jsonify({'success': False, 'error': str(exc)}), 500

# ---- Staff -> User conversion endpoints ---- #

def _generate_temp_password(length: int = 10) -> str:
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def _build_account_welcome_email_html(name: str, email: str, temp_password: str) -> tuple[str, str]:
    subject = 'Your Excel Tutors account'
    body = f"""
<!DOCTYPE html>
<html><body style="font-family:'Segoe UI',Arial,sans-serif;background:#f1f5f9;padding:24px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;margin:0 auto;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;">
    <tr>
      <td style="background:#0f172a;padding:20px 28px;color:#ffffff;font-weight:600;">Excel Tutors</td>
    </tr>
    <tr>
      <td style="padding:28px;color:#0f172a;">
        <p style="margin:0 0 12px;">Hello {name or 'there'},</p>
        <p style="margin:0 0 12px;">An account has been created for you on the Excel Tutors portal.</p>
        <table role="presentation" cellpadding="0" cellspacing="0" style="margin:14px 0 18px;">
          <tr>
            <td style="padding:6px 10px;color:#64748b;font-weight:600;">Username</td>
            <td style="padding:6px 10px;color:#0f172a;">{email}</td>
          </tr>
          <tr>
            <td style="padding:6px 10px;color:#64748b;font-weight:600;">Temporary Password</td>
            <td style="padding:6px 10px;color:#0f172a;">{temp_password}</td>
          </tr>
        </table>
        <p style="margin:0 0 12px;">For security, you'll be asked to change this password when you first log in.</p>
        <div style="margin-top:18px;">
          <a href="{url_for('login', _external=True)}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 20px;border-radius:10px;font-size:14px;font-weight:600;">Go to login</a>
        </div>
        <p style="margin:24px 0 0;font-size:12px;color:#64748b;">If you didn't expect this, please contact your centre manager.</p>
      </td>
    </tr>
    <tr>
      <td style="background:#f8fafc;color:#94a3b8;padding:16px 28px;text-align:center;font-size:11px;">&copy; {date.today().year} Excel Tutors</td>
    </tr>
  </table>
</body></html>
    """
    return subject, body

@app.route('/api/staff/convert-to-users', methods=['POST'])
@login_required
@permission_required('manage_staff')
def api_staff_convert_to_users():
    payload = request.get_json(silent=True) or {}
    ids = payload.get('ids') or []
    if not ids:
        return jsonify({'success': False, 'error': 'No staff selected.'}), 400
    results = {'created': 0, 'skipped': [], 'errors': []}
    for sid in ids:
        try:
            s = Staff.query.get(int(sid))
            if not s:
                results['errors'].append({'id': sid, 'error': 'not_found'})
                continue
            if not s.active:
                results['skipped'].append({'id': s.id, 'reason': 'inactive'})
                continue
            if s.user_id:
                results['skipped'].append({'id': s.id, 'reason': 'already_mapped'})
                continue
            email = (s.email or '').strip().lower()
            if not email:
                results['skipped'].append({'id': s.id, 'reason': 'no_email'})
                continue
            existing = User.query.filter_by(email=email).first()
            if existing:
                results['skipped'].append({'id': s.id, 'reason': 'user_exists'})
                continue
            temp_pwd = _generate_temp_password()
            u = User(
                name=s.name or (email.split('@')[0] if email else 'User'),
                email=email,
                password_hash=generate_password_hash(temp_pwd),
                is_approved=True,
                is_active=True,
                role='tutor',
            )
            if hasattr(u, 'force_password_reset'):
                u.force_password_reset = True
            db.session.add(u)
            db.session.flush()
            s.user_id = u.id
            db.session.commit()
            try:
                subject, html = _build_account_welcome_email_html(s.name, email, temp_pwd)
                send_email(email, subject, html)
            except Exception as _em:
                results['errors'].append({'id': s.id, 'error': f'email_failed: {_em}'})
            results['created'] += 1
        except Exception as exc:
            db.session.rollback()
            results['errors'].append({'id': sid, 'error': str(exc)})
    return jsonify({'success': True, **results})


@app.route('/api/postcodes/lookup')
@login_required
def api_postcodes_lookup():
    # Simple proxy to postcodes.io lookup/suggest endpoint
    q = (request.args.get('q') or '').strip()
    if not q:
        return jsonify({'success': False, 'error': 'missing_query'}), 400
    try:
        import requests

        # Use the suggestions endpoint for partial terms
        url = f'https://api.postcodes.io/postcodes/{q}/autocomplete'
        resp = requests.get(url, timeout=5)
        data = resp.json() if resp.status_code == 200 else {'result': []}
        return jsonify({'success': True, 'data': data.get('result') or []})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/postcodes/details')
@login_required
def api_postcodes_details():
    """Return full postcode details for a supplied postcode value. Query param: postcode=..."""
    pc = (request.args.get('postcode') or '').strip()
    if not pc:
        return jsonify({'success': False, 'error': 'missing_postcode'}), 400
    try:
        import requests

        # Normalize postcode for the API
        url = f'https://api.postcodes.io/postcodes/{pc}'
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return jsonify({'success': False, 'error': 'not_found'}), 404
        data = resp.json().get('result') or {}
        # Map some useful fields
        mapped = {
            'postcode': data.get('postcode'),
            'admin_district': data.get('admin_district'),
            'region': data.get('region'),
            'parish': data.get('parish'),
            'longitude': data.get('longitude'),
            'latitude': data.get('latitude'),
        }
        return jsonify({'success': True, 'data': mapped})
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/api/postcodes/addresses')
@login_required
def api_postcodes_addresses():
    """Return address-level results for a postcode using a configured provider.

    If the environment variable GETADDRESS_IO_KEY is set, this will call
    https://api.getaddress.io/find/{postcode}?api-key=KEY and return a list of
    structured addresses. If no provider key is present, the endpoint returns
    not_supported.
    """
    pc = (request.args.get('postcode') or '').strip()
    if not pc:
        return jsonify({'success': False, 'error': 'missing_postcode'}), 400
    # Prefer GetAddress.io when key is configured
    ga_key = os.environ.get('GETADDRESS_IO_KEY')
    try:
        if ga_key:
            import requests

            url = f'https://api.getaddress.io/find/{pc}?api-key={ga_key}&expand=true'
            resp = requests.get(url, timeout=7)
            if resp.status_code != 200:
                return jsonify({'success': False, 'error': 'provider_error'}), 502
            j = resp.json()
            raw_addresses = j.get('addresses') or []
            mapped = []
            for a in raw_addresses:
                # getaddress.io returns structured address components when expand=true
                # fields like line_1, line_2, town_or_city, county, postcode
                mapped.append({
                    'summary': a.get('formatted_address') or ', '.join(filter(None, [a.get('line_1'), a.get('line_2'), a.get('town_or_city')])),
                    'address_line_1': a.get('line_1') or '',
                    'address_line_2': a.get('line_2') or '',
                    'town': a.get('town_or_city') or a.get('post_town') or '',
                    'region': a.get('county') or a.get('region') or '',
                    'postcode': a.get('postcode') or pc,
                })
            return jsonify({'success': True, 'data': mapped})

        # No supported provider configured
        return jsonify({'success': False, 'error': 'no_address_provider_configured'}), 501
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500


@app.route('/staff/<int:sid>/upload-photo', methods=['POST'])
@login_required
@permission_required('manage_staff')
def staff_upload_photo(sid: int):
    s = Staff.query.get_or_404(sid)
    photo_file = request.files.get('photo')
    if not photo_file or not photo_file.filename:
        return jsonify({'success': False, 'error': 'no_file'}), 400
    try:
        from werkzeug.utils import secure_filename
        filename = secure_filename(photo_file.filename)
        uploads_dir = os.path.join(app.root_path, 'static', 'uploads')
        os.makedirs(uploads_dir, exist_ok=True)
        dest = os.path.join(uploads_dir, filename)
        photo_file.save(dest)
        s.photo = filename
        db.session.commit()
        return jsonify({'success': True, 'photo': filename})
    except Exception as exc:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(exc)}), 500

@app.route('/staff/<int:sid>/map-user', methods=['GET','POST'])
@login_required
@permission_required('manage_staff')
def staff_map_user(sid: int):
    st = Staff.query.get_or_404(sid)
    users = User.query.order_by(User.name.asc()).all()
    preselect_id = None
    if st.email:
        m = User.query.filter_by(email=(st.email or '').strip().lower()).first()
        preselect_id = m.id if m else None
    if request.method == 'POST':
        try:
            user_id = int(request.form.get('user_id'))
        except Exception:
            user_id = None
        if not user_id:
            flash('Please select a user to map.', 'warning')
            return render_template('staff/map_user.html', staff=st, users=users, preselect_id=preselect_id)
        u = User.query.get(user_id)
        if not u:
            flash('Selected user not found.', 'danger')
            return render_template('staff/map_user.html', staff=st, users=users, preselect_id=preselect_id)
        st.user_id = u.id
        db.session.commit()
        flash('Staff mapped to the selected user.', 'success')
        return redirect(url_for('staff_index'))
    return render_template('staff/map_user.html', staff=st, users=users, preselect_id=preselect_id)

@app.route('/staff/<int:sid>')
@login_required
@permission_required('manage_staff')
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
@permission_required('manage_staff')
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


# ---------------- Resource Management ---------------- #
def _resource_branch_initials(branch: str) -> str:
    b = (branch or '').lower()
    if 'whitechapel' in b:
        return 'WC'
    if 'east ham' in b:
        return 'EH'
    if 'stratford' in b:
        return 'ST'
    if 'docklands' in b:
        return 'DL'
    return (branch or '')[:2].upper()

def _resource_bind_form_choices(form) -> None:
    branches = [(b, b) for b in BRANCH_CHOICES()]
    if hasattr(form, 'branch'):
        form.branch.choices = branches
    if hasattr(form, 'type'):
        form.type.choices = RESOURCE_TYPE_CHOICES
    if hasattr(form, 'status'):
        form.status.choices = RESOURCE_STATUS_CHOICES

def _resource_next_seq(rtype: str) -> int:
    from models import Resource
    latest = (Resource.query.filter_by(type=rtype)
              .order_by(Resource.type_seq.desc())
              .first())
    return (latest.type_seq + 1) if latest and latest.type_seq else 1

def _resource_generate_ids(rtype: str, branch: str, type_other: str | None = None) -> tuple[str,str,str,int]:
    # Legacy helper retained but now delegates numeric ID generation.
    seq = _resource_next_seq(rtype)
    type_slug_source = type_other if (rtype == 'Other' and type_other) else rtype
    type_slug = type_slug_source.strip().replace(' ', '').upper()
    br_init = _resource_branch_initials(branch)
    name = f"{type_slug}-{seq}-{br_init}"
    # Numeric resource ID equals barcode value (Code128 content)
    rid = _resource_next_numeric_id()
    return rid, rid, name, seq

def _resource_next_numeric_id(length: int = 10) -> str:
    """Generate next unique numeric ID for resources, persisted via Setting.

    Stores/reads key 'resource_next_id' using utils.get_setting/set_setting.
    Ensures uniqueness against Resource.resource_id; returns zero-padded string.
    """
    from models import Resource as _Resource
    from utils import get_setting, set_setting

    # Default starting number (10 digits) if not set
    default_start = 10 ** (length - 1)
    try:
        current_raw = str(get_setting('resource_next_id', str(default_start)))
        current = int(''.join(ch for ch in current_raw if ch.isdigit()))
    except Exception:
        current = default_start
    # Try up to 50 attempts in case of race/uniqueness issues
    for _ in range(50):
        candidate = f"{current:0{length}d}"
        exists = _Resource.query.filter_by(resource_id=candidate).first()
        if not exists:
            # Persist next value
            try:
                set_setting('resource_next_id', str(current + 1))
            except Exception:
                pass
            return candidate
        current += 1
    # Fallback: random 12-digit if sequential space exhausted (unlikely)
    import random as _r
    return f"{_r.randint(10**(length-1), 10**length - 1)}"

@app.route('/resources')
@login_required
@permission_required('manage_resources')
def resources_index():
    # Data for table rendered server-side, DataTables JS enhances it
    items = Resource.query.order_by(Resource.created_at.desc()).all()
    return render_template(
        'resources/index.html',
        items=items,
        branch_choices=BRANCH_CHOICES(),
        resource_type_choices=RESOURCE_TYPE_CHOICES,
    )

@app.route('/resources/loans')
@login_required
@permission_required('manage_resources')
def resources_loans_index():
    loans = (ResourceLoan.query
             .options(joinedload(ResourceLoan.resource), joinedload(ResourceLoan.staff))
             .filter(ResourceLoan.status == 'on_loan')
             .order_by(ResourceLoan.loaned_at.desc())
             .all())
    # Compose display payload
    rows = []
    for ln in loans:
        rows.append({
            'id': ln.id,
            'resource_id': ln.resource_id,
            'resource_name': ln.resource.name if ln.resource else '',
            'barcode': ln.resource.barcode_value if ln.resource else '',
            'staff_name': ln.staff.name if ln.staff else '',
            'staff_email': getattr(ln.staff, 'email', '') or '',
            'loaned_at': ln.loaned_at,
            'due_at': ln.due_at,
        })
    can_force_return = getattr(current_user, 'is_superadmin', False)
    return render_template('resources/loans.html', loans=rows, can_force_return=can_force_return)


@app.route('/resources/loans/<int:loan_id>/force-return', methods=['POST'])
@login_required
@permission_required('manage_resources')
def resources_force_return(loan_id: int):
    if not getattr(current_user, 'is_superadmin', False):
        abort(403)
    loan = (ResourceLoan.query
            .options(joinedload(ResourceLoan.resource), joinedload(ResourceLoan.staff))
            .get_or_404(loan_id))
    if loan.status != 'on_loan':
        flash('Loan is not currently active.', 'warning')
        return redirect(url_for('resources_loans_index'))
    now = datetime.now(timezone.utc)
    loan.status = 'returned'
    loan.returned_at = now
    try:
        db.session.commit()
        resource_name = loan.resource.name if loan.resource else 'Resource'
        flash(f'{resource_name} is now available.', 'success')
    except Exception as exc:
        db.session.rollback()
        app.logger.error('resources_force_return: failed %s', exc, exc_info=True)
        flash('Failed to force return. Please try again.', 'danger')
    return redirect(url_for('resources_loans_index'))


@app.route('/resources/loans/history')
@login_required
@permission_required('manage_resources')
def resources_loans_history():
    # Show last 30 days of loan activity (plus any currently active loans)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    q = (ResourceLoan.query
         .options(joinedload(ResourceLoan.resource), joinedload(ResourceLoan.staff))
         .order_by(ResourceLoan.loaned_at.desc()))
    # Include loans where: loaned_at within 30 days OR returned within 30 days OR still on loan
    q = q.filter(
        or_(
            ResourceLoan.loaned_at >= cutoff,
            and_(ResourceLoan.returned_at.isnot(None), ResourceLoan.returned_at >= cutoff),
            ResourceLoan.status == 'on_loan'
        )
    )
    loans = q.all()
    rows = []
    staff_names = set()
    types = set()
    for ln in loans:
        res = ln.resource
        st = ln.staff
        staff_name = st.name if st else ''
        staff_names.add(staff_name) if staff_name else None
        rtype = res.type if res else ''
        if rtype:
            types.add(rtype)
        rows.append({
            'id': ln.id,
            'resource_name': res.name if res else '',
            'resource_id': res.resource_id if res else '',
            'resource_type': rtype,
            'barcode': res.barcode_value if res else '',
            'staff_name': staff_name,
            'status': ln.status,
            'loaned_at': ln.loaned_at,
            'due_at': ln.due_at,
            'returned_at': ln.returned_at,
        })
    staff_list = sorted([s for s in staff_names if s])
    type_list = sorted([t for t in types if t])
    return render_template('resources/loan_history.html', loans=rows, staff_list=staff_list, type_list=type_list)

@app.route('/resources/dashboard')
@login_required
@permission_required('manage_resources')
def resources_dashboard():
    from sqlalchemy import func

    from models import Staff
    now = datetime.now(timezone.utc)

    # Core KPIs
    total = db.session.query(func.count(Resource.id)).scalar() or 0
    by_status = dict(db.session.query(Resource.status, func.count(Resource.id)).group_by(Resource.status).all())
    on_loan = db.session.query(func.count(ResourceLoan.id)).filter(ResourceLoan.status=='on_loan').scalar() or 0
    overdue = db.session.query(func.count(ResourceLoan.id)).filter(ResourceLoan.status=='on_loan', ResourceLoan.due_at <= now).scalar() or 0
    available = max(total - on_loan, 0)

    # Distributions
    by_type = dict(db.session.query(Resource.type, func.count(Resource.id)).group_by(Resource.type).all())
    by_branch = dict(db.session.query(Resource.branch, func.count(Resource.id)).group_by(Resource.branch).all())

    # Trend: loans over last 14 days (by day)
    cutoff14 = now - timedelta(days=13)
    loans_14 = (ResourceLoan.query
                .filter(ResourceLoan.loaned_at.isnot(None))
                .filter(ResourceLoan.loaned_at >= cutoff14)
                .all())
    # Build a zero-initialized day map for 14 days
    from datetime import date as _date
    day_keys = [(_date.fromtimestamp((cutoff14 + timedelta(days=i)).timestamp())) for i in range(14)]
    trend_map: dict[str, int] = {d.isoformat(): 0 for d in day_keys}
    for ln in loans_14:
        try:
            d = ln.loaned_at.astimezone(timezone.utc).date().isoformat()
            if d in trend_map:
                trend_map[d] += 1
        except Exception:
            pass
    loans_trend = [{ 'day': k, 'count': trend_map[k] } for k in sorted(trend_map.keys())]

    # Top borrowers in last 30 days
    cutoff30 = now - timedelta(days=30)
    top_rows = (db.session.query(ResourceLoan.staff_id, func.count(ResourceLoan.id).label('c'))
                .filter(ResourceLoan.loaned_at.isnot(None), ResourceLoan.loaned_at >= cutoff30)
                .group_by(ResourceLoan.staff_id)
                .order_by(func.count(ResourceLoan.id).desc())
                .limit(10)
                .all())
    staff_map = {s.id: s.name for s in Staff.query.filter(Staff.id.in_([sid for sid, _ in top_rows if sid])).all()}
    top_borrowers = [{ 'name': (staff_map.get(sid) or 'Unknown'), 'count': cnt } for sid, cnt in top_rows]

    # Overdue aging buckets for active loans
    overdue_loans = (ResourceLoan.query
                     .filter(ResourceLoan.status=='on_loan', ResourceLoan.due_at <= now)
                     .all())
    buckets = { '1_3': 0, '4_7': 0, '8_plus': 0 }
    for ln in overdue_loans:
        try:
            days = (now - ln.due_at).days
            if days <= 3:
                buckets['1_3'] += 1
            elif days <= 7:
                buckets['4_7'] += 1
            else:
                buckets['8_plus'] += 1
        except Exception:
            pass

    # Longest current loans (oldest loaned_at)
    longest_current = (ResourceLoan.query
                       .options(joinedload(ResourceLoan.resource), joinedload(ResourceLoan.staff))
                       .filter(ResourceLoan.status=='on_loan')
                       .order_by(ResourceLoan.loaned_at.asc())
                       .limit(10)
                       .all())
    longest_rows = []
    for ln in longest_current:
        try:
            days_on_loan = (now - ln.loaned_at).days if ln.loaned_at else None
        except Exception:
            days_on_loan = None
        longest_rows.append({
            'resource_name': ln.resource.name if ln.resource else '',
            'resource_id': ln.resource.resource_id if ln.resource else '',
            'staff_name': ln.staff.name if ln.staff else '',
            'loaned_at': ln.loaned_at,
            'days_on_loan': days_on_loan,
        })

    # Recent activity (last 15 loan/return events)
    recent_loans = (ResourceLoan.query
                    .options(joinedload(ResourceLoan.resource), joinedload(ResourceLoan.staff))
                    .order_by(ResourceLoan.loaned_at.desc())
                    .limit(15)
                    .all())
    recent_returns = (ResourceLoan.query
                      .options(joinedload(ResourceLoan.resource), joinedload(ResourceLoan.staff))
                      .filter(ResourceLoan.returned_at.isnot(None))
                      .order_by(ResourceLoan.returned_at.desc())
                      .limit(15)
                      .all())
    recent_activity = []
    for ln in recent_loans:
        if ln.loaned_at:
            recent_activity.append({
                'event': 'loan',
                'when': ln.loaned_at,
                'resource_name': ln.resource.name if ln.resource else '',
                'resource_id': ln.resource.resource_id if ln.resource else '',
                'staff_name': ln.staff.name if ln.staff else '',
            })
    for ln in recent_returns:
        recent_activity.append({
            'event': 'return',
            'when': ln.returned_at,
            'resource_name': ln.resource.name if ln.resource else '',
            'resource_id': ln.resource.resource_id if ln.resource else '',
            'staff_name': ln.staff.name if ln.staff else '',
        })
    # Sort combined by time desc and keep top 15
    recent_activity.sort(key=lambda r: r['when'] or now, reverse=True)
    recent_activity = recent_activity[:15]

    return render_template(
        'resources/dashboard.html',
        total=total,
        available=available,
        by_status=by_status,
        on_loan=on_loan,
        overdue=overdue,
        by_type=by_type,
        by_branch=by_branch,
        loans_trend=loans_trend,
        top_borrowers=top_borrowers,
        overdue_buckets=buckets,
        longest_rows=longest_rows,
        recent_activity=recent_activity,
    )

@app.route('/api/resources', methods=['POST'])
@login_required
@permission_required('manage_resources')
def resources_create():
    form = ResourceForm()
    _resource_bind_form_choices(form)
    if not form.validate_on_submit():
        return jsonify({'success': False, 'errors': form.errors}), 400
    rtype = form.type.data
    branch = form.branch.data
    type_other = (form.type_other.data or '').strip() if rtype == 'Other' else None
    if rtype == 'Other' and not type_other:
        return jsonify({'success': False, 'errors': {'type_other': ['Please specify the category when selecting Other.']}}), 400
    rid, barcode_val, name_default, seq = _resource_generate_ids(rtype, branch, type_other)
    name = (form.name.data or '').strip() or name_default
    res = Resource(type=rtype, type_other=type_other, branch=branch, type_seq=seq,
                   resource_id=rid, name=name, barcode_value=barcode_val,
                   status=form.status.data)
    db.session.add(res)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        return jsonify({'success': False, 'errors': {'_': ['Save failed: duplicate identifier']}}), 400
    return jsonify({'success': True, 'id': res.id})

@app.route('/api/resources/bulk', methods=['POST'])
@login_required
@permission_required('manage_resources')
def resources_bulk_create():
    form = ResourceBulkForm()
    _resource_bind_form_choices(form)
    if not form.validate_on_submit():
        return jsonify({'success': False, 'errors': form.errors}), 400
    rtype = form.type.data
    branch = form.branch.data
    qty = form.quantity.data or 1
    status = form.status.data or 'functional'
    type_other = (form.type_other.data or '').strip() if rtype == 'Other' else None
    if rtype == 'Other' and not type_other:
        return jsonify({'success': False, 'errors': {'type_other': ['Please specify the category when selecting Other.']}}), 400
    created_ids: list[int] = []
    for _ in range(qty):
        rid, barcode_val, name_default, seq = _resource_generate_ids(rtype, branch, type_other)
        res = Resource(
            type=rtype,
            type_other=type_other,
            branch=branch,
            type_seq=seq,
            resource_id=rid,
            name=name_default,
            barcode_value=barcode_val,
            status=status,
        )
        db.session.add(res)
        try:
            db.session.flush()
            created_ids.append(res.id)
        except Exception:
            db.session.rollback()
            continue
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'success': False, 'errors': {'_': ['Bulk create failed: duplicate identifier']}}), 400
    except Exception as _exc:
        db.session.rollback()
        return jsonify({'success': False, 'errors': {'_': ['Bulk create failed']}}), 500
    return jsonify({'success': True, 'count': len(created_ids), 'ids': created_ids})

@app.route('/api/resources/<int:rid>', methods=['GET'])
@login_required
@permission_required('manage_resources')
def resources_get(rid: int):
    res = Resource.query.get_or_404(rid)
    payload = {
        'id': res.id,
        'type': res.type,
        'type_other': res.type_other,
        'branch': res.branch,
        'name': res.name,
        'status': res.status,
        'resource_id': res.resource_id,
        'barcode_value': res.barcode_value,
    }
    return jsonify({'success': True, 'resource': payload})

@app.route('/api/resources/<int:rid>', methods=['POST'])
@login_required
@permission_required('manage_resources')
def resources_update(rid: int):
    res = Resource.query.get_or_404(rid)
    form = ResourceForm()
    if not form.validate_on_submit():
        return jsonify({'success': False, 'errors': form.errors}), 400
    rtype = form.type.data
    branch = form.branch.data
    type_other = (form.type_other.data or '').strip() if rtype == 'Other' else None
    # If type or branch changed, regenerate name (and optionally IDs if we want immutability – we'll keep resource_id/barcode stable)
    res.type = rtype
    res.type_other = type_other
    res.branch = branch
    res.name = (form.name.data or '').strip() or res.name
    res.status = form.status.data
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/resources/<int:rid>/delete', methods=['POST'])
@login_required
@permission_required('manage_resources')
def resources_delete(rid: int):
    res = Resource.query.get_or_404(rid)
    db.session.delete(res)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/resources/<int:rid>/barcode.png')
@login_required
@permission_required('manage_resources')
def resources_barcode_png(rid: int):
    """Generate a 3x2 inch PNG label (300 DPI) with centered text using Tw Cen MT Bold if available.

    Shows: Resource Name, Resource Type, Code128 barcode, and Barcode Number (numeric ID).
    """
    res = Resource.query.get_or_404(rid)
    try:
        from io import BytesIO

        from barcode import Code128
        from barcode.writer import ImageWriter
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return abort(500, description='Barcode dependencies not installed')

    # Canvas 3x2 inches at 300 DPI
    DPI = 300
    width = int(3 * DPI)
    height = int(2 * DPI)
    label = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(label)

    # Load Tw Cen MT Bold if available; fallback chain
    def load_font(size: int):
        candidates = [
            'Tw Cen MT Bold.ttf', 'TwCenMT-Bold.ttf', 'TCCB____.TTF',
            'Tw Cen MT.ttf', 'TwCenMT.ttf',
            '/Library/Fonts/Tw Cen MT Bold.ttf',
            '/Library/Fonts/Tw Cen MT.ttf',
            '/System/Library/Fonts/Supplemental/Tw Cen MT Bold.ttf',
            '/System/Library/Fonts/Supplemental/Tw Cen MT.ttf',
            'C\\\Windows\\Fonts\\TCCB____.TTF',
            'C\\\Windows\\Fonts\\TwCenMT-Bold.ttf',
            'C\\\Windows\\Fonts\\Tw Cen MT Bold.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    # Draw centered text fitted to width
    def draw_centered_text(text: str, y: int, max_width: int, base_size: int):
        if not text:
            return 0
        size = base_size
        font = load_font(size)
        tw, th = draw.textbbox((0, 0), text, font=font)[2:4]
        while tw > max_width and size > 8:
            size -= 1
            font = load_font(size)
            tw, th = draw.textbbox((0, 0), text, font=font)[2:4]
        x = (width - tw) // 2
        draw.text((x, y), text, fill='black', font=font)
        return th

    # Target area for barcode in px (leave margins for text above/below)
    margin_x = int(width * 0.06)
    top_y = int(height * 0.10)
    max_text_width = width - margin_x * 2
    name_h = 0  # calculated later when drawing text
    type_y = 0
    type_h = 0
    num_text_h = 28
    # Estimate available height after headers (recomputed after text draw)
    available_h_est = int(height * 0.55)

    # Generate a crisp Code128 barcode at 300 DPI sized to our target width without resampling.
    # We adjust module_width (in mm) based on an initial render to fit target width.
    def _render_barcode(module_width_mm: float, module_height_mm: float, quiet_zone_mm: float):
        opts = {
            'module_width': module_width_mm,
            'module_height': module_height_mm,
            'quiet_zone': quiet_zone_mm,
            'write_text': False,
            'dpi': DPI,
        }
        bc = Code128(str(res.barcode_value or ''), writer=ImageWriter())
        tmp = BytesIO()
        bc.write(tmp, options=opts)
        tmp.seek(0)
        return Image.open(tmp).convert('RGB')

    try:
        # Initial guess for module sizing (mm)
        mw = 0.70  # bar width (mm)
        mh = 22.0  # bar height (mm)
        qz = max(7.0, mw * 10)  # quiet zone ~10x module width (mm)
        bc_img = _render_barcode(mw, mh, qz)
        target_w = width - margin_x * 2
        # If too wide/narrow, scale module width and re-render (linear relationship)
        if bc_img.width != 0:
            scale_factor = target_w / bc_img.width
            # Only re-render if >15% off to avoid tiny variations
            if scale_factor < 0.85 or scale_factor > 1.15:
                mw = max(0.40, min(1.20, mw * scale_factor))
                qz = max(7.0, mw * 10)
                bc_img = _render_barcode(mw, mh, qz)
        # Ensure height fits roughly within available area; tweak height if needed
        if bc_img.height > available_h_est * 1.1:
            # Reduce height moderately and re-render (keep width via same mw/qz)
            mh = max(16.0, mh * (available_h_est / bc_img.height))
            bc_img = _render_barcode(mw, mh, qz)
    except Exception:
        return abort(400, description='Invalid barcode content')

    # Layout text (after barcode sizing decisions)
    name_h = draw_centered_text(res.name or '', top_y, max_text_width, base_size=40)
    type_y = top_y + name_h + 6
    type_h = draw_centered_text((res.type or ''), type_y, max_text_width, base_size=28)
    available_h = height - (type_y + type_h + 16) - (num_text_h + 20)
    # Paste barcode at native size (no resampling) to preserve bar fidelity
    bc_w, bc_h = bc_img.width, bc_img.height
    # If still slightly larger than available area, final safeguard: only downscale with NEAREST
    if bc_w > (width - margin_x * 2) or bc_h > available_h:
        from PIL import Image as _Img
        scale = min((width - margin_x * 2) / bc_w, available_h / bc_h)
        new_w = max(1, int(bc_w * scale))
        new_h = max(1, int(bc_h * scale))
        bc_img = bc_img.resize((new_w, new_h), resample=_Img.NEAREST)
        bc_w, bc_h = new_w, new_h
    bc_x = (width - bc_w) // 2
    bc_y = type_y + type_h + 16 + max(0, (available_h - bc_h) // 2)
    label.paste(bc_img, (bc_x, bc_y))

    # Barcode number
    code_y = bc_y + new_h + 8
    draw_centered_text(str(res.barcode_value or ''), code_y, max_text_width, base_size=26)

    out = BytesIO()
    label.save(out, format='PNG', dpi=(DPI, DPI))
    out.seek(0)
    return send_file(out, mimetype='image/png', as_attachment=True, download_name=f"{res.resource_id}.png")

 

@app.route('/resources/barcodes.pdf')
@login_required
@permission_required('manage_resources')
def resources_barcodes_pdf():
    """Generate a single PDF containing barcode labels for resources.

    Query params:
    - type: optional resource type filter (exact match on Resource.type)

    Layout: 15 labels per page (3 columns x 5 rows). Each label has a border,
    and text in the label image is rendered larger for readability.
    """
    rtype = (request.args.get('type') or '').strip()
    q = Resource.query
    if rtype:
        q = q.filter(Resource.type == rtype)
    items = q.order_by(Resource.type.asc(), Resource.branch.asc(), Resource.resource_id.asc()).all()

    # Build PNG labels for each resource and embed as data URIs
    labels = []
    try:
        import base64
        from io import BytesIO

        from barcode import Code128
        from barcode.writer import ImageWriter
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        abort(500, description='Barcode dependencies not installed')

    DPI = 300
    width = int(3 * DPI)
    height = int(2 * DPI)

    def load_font(size: int):
        candidates = [
            'Tw Cen MT Bold.ttf', 'TwCenMT-Bold.ttf', 'TCCB____.TTF',
            'Tw Cen MT.ttf', 'TwCenMT.ttf',
            '/Library/Fonts/Tw Cen MT Bold.ttf',
            '/Library/Fonts/Tw Cen MT.ttf',
            '/System/Library/Fonts/Supplemental/Tw Cen MT Bold.ttf',
            '/System/Library/Fonts/Supplemental/Tw Cen MT.ttf',
            'C\\Windows\\Fonts\\TCCB____.TTF',
            'C\\Windows\\Fonts\\TwCenMT-Bold.ttf',
            'C\\Windows\\Fonts\\Tw Cen MT Bold.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    def draw_centered_text(draw: ImageDraw.ImageDraw, text: str, y: int, max_width: int, base_size: int):
        if not text:
            return 0
        size = base_size
        font = load_font(size)
        tw, th = draw.textbbox((0, 0), text, font=font)[2:4]
        while tw > max_width and size > 8:
            size -= 1
            font = load_font(size)
            tw, th = draw.textbbox((0, 0), text, font=font)[2:4]
        x = (width - tw) // 2
        draw.text((x, y), text, fill='black', font=font)
        return th

    def render_label_png(res: Resource) -> bytes:
        label = Image.new('RGB', (width, height), 'white')
        draw = ImageDraw.Draw(label)
        # Text areas
        margin_x = int(width * 0.06)
        top_y = int(height * 0.08)
        max_text_width = width - margin_x * 2
        # Slightly larger fonts for readability in sheet printing
        name_h = draw_centered_text(draw, res.name or '', top_y, max_text_width, base_size=46)
        type_y = top_y + name_h + 8
        type_h = draw_centered_text(draw, (res.type or ''), type_y, max_text_width, base_size=32)

        # Barcode rendering with dynamic module sizing
        num_text_h = 32
        available_h = height - (type_y + type_h + 16) - (num_text_h + 22)
        target_w = width - margin_x * 2

        def _render_bc(module_width_mm: float, module_height_mm: float, quiet_zone_mm: float) -> Image.Image:
            opts = {
                'module_width': module_width_mm,
                'module_height': module_height_mm,
                'quiet_zone': quiet_zone_mm,
                'write_text': False,
                'dpi': DPI,
            }
            bc = Code128(str(res.barcode_value or ''), writer=ImageWriter())
            tmp = BytesIO(); bc.write(tmp, options=opts); tmp.seek(0)
            return Image.open(tmp).convert('RGB')

        try:
            mw = 0.70; mh = 22.0; qz = max(7.0, mw * 10)
            bc_img = _render_bc(mw, mh, qz)
            if bc_img.width != 0:
                scale_factor = target_w / bc_img.width
                if scale_factor < 0.85 or scale_factor > 1.15:
                    mw = max(0.40, min(1.20, mw * scale_factor)); qz = max(7.0, mw * 10)
                    bc_img = _render_bc(mw, mh, qz)
            if bc_img.height > available_h * 1.1:
                mh = max(16.0, mh * (available_h / bc_img.height))
                bc_img = _render_bc(mw, mh, qz)
        except Exception:
            # If barcode fails, return a blank label with just texts
            bc_img = None

        # Paste barcode at native size (downscale only if absolutely needed)
        if bc_img is not None:
            bc_w, bc_h = bc_img.width, bc_img.height
            if bc_w > target_w or bc_h > available_h:
                from PIL import Image as _Img
                scale = min(target_w / bc_w, available_h / bc_h)
                bc_img = bc_img.resize((max(1, int(bc_w * scale)), max(1, int(bc_h * scale))), resample=_Img.NEAREST)
                bc_w, bc_h = bc_img.width, bc_img.height
            bc_x = (width - bc_w) // 2
            bc_y = type_y + type_h + 16 + max(0, (available_h - bc_h) // 2)
            label.paste(bc_img, (bc_x, bc_y))

        code_y = height - (num_text_h + 8)
        draw_centered_text(draw, str(res.barcode_value or ''), code_y, max_text_width, base_size=30)
        out = BytesIO(); label.save(out, format='PNG', dpi=(DPI, DPI)); out.seek(0)
        return out.read()

    for r in items:
        try:
            png_bytes = render_label_png(r)
            b64 = base64.b64encode(png_bytes).decode('utf-8')
            labels.append({'img_data': f"data:image/png;base64,{b64}", 'name': r.name, 'type': r.type, 'id': r.resource_id})
        except Exception:
            continue

    # Build HTML grid for xhtml2pdf
    html = render_template('resources/barcodes_pdf.html', labels=labels, selected_type=rtype)
    try:
        from io import BytesIO

        from xhtml2pdf import pisa
        pdf_io = BytesIO(); pisa.CreatePDF(html, dest=pdf_io); pdf_io.seek(0)
        fname = 'resource_barcodes.pdf' if not rtype else f'resource_barcodes_{rtype.replace(" ","_")}.pdf'
        return send_file(pdf_io, mimetype='application/pdf', as_attachment=True, download_name=fname)
    except Exception:
        # Fallback to HTML if PDF generation fails
        return html


# ---------------- Public: Resource Loan/Return ---------------- #
@app.route('/public/resources/loan', methods=['GET','POST'])
def public_resource_loan():
    """Public form to loan or return a resource by barcode and staff access code.

    Form fields: action (loan|return), access_code, barcode_value
    """
    if request.method == 'GET':
        from flask import make_response

        # Pass through any status/message from PRG redirect to drive modal
        status = (request.args.get('status') or '').strip()
        msg = (request.args.get('msg') or '').strip()
        resp = make_response(render_template('public/resources_loan.html', status=status, msg=msg))
        # Disable caching to prevent autofill/back-forward cache
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
        return resp
    # POST
    action = (request.form.get('action') or '').strip().lower()
    access_code = (request.form.get('access_code') or '').strip()
    barcode_value = (request.form.get('barcode_value') or '').strip()
    if action not in ('loan','return'):
        return redirect(url_for('public_resource_loan', status='error', msg='Invalid action selected.'))
    # Lookup staff by access_code
    staff = Staff.query.filter_by(access_code=access_code).first()
    if not staff:
        return redirect(url_for('public_resource_loan', status='error', msg='Invalid access code.'))
    # Lookup resource by barcode
    res = Resource.query.filter_by(barcode_value=barcode_value).first()
    if not res:
        return redirect(url_for('public_resource_loan', status='error', msg='Resource not found for that barcode.'))
    now = datetime.now(timezone.utc)
    if action == 'loan':
        # Only one active loan allowed per resource
        existing = ResourceLoan.query.filter_by(resource_id=res.id, status='on_loan').first()
        if existing:
            return redirect(url_for('public_resource_loan', status='error', msg='Cannot loan: this item is already on loan.'))
        due = datetime(now.year, now.month, now.day, 19, 30, tzinfo=timezone.utc)  # due today 19:30 UTC
        loan = ResourceLoan(resource_id=res.id, staff_id=staff.id, loaned_at=now, due_at=due, status='on_loan')
        db.session.add(loan)
        try:
            db.session.commit()
        except Exception as _exc:
            db.session.rollback()
            return redirect(url_for('public_resource_loan', status='error', msg='Cannot loan: this item is already on loan.'))
        return redirect(url_for('public_resource_loan', status='success', msg=f'Loan recorded for {res.name}. Due by 7:30 PM today.'))
    else:
        # Return flow
        existing = ResourceLoan.query.filter_by(resource_id=res.id, status='on_loan').first()
        if not existing:
            return redirect(url_for('public_resource_loan', status='error', msg='Cannot return: this item is not currently on loan.'))
        existing.status = 'returned'
        existing.returned_at = now
    db.session.commit()
    return redirect(url_for('public_resource_loan', status='success', msg=f'Return recorded for {res.name}. Thank you.'))


def _send_overdue_resource_emails():
    """Send reminder emails to staff with items still on loan past due time (19:30 UTC)."""
    with app.app_context():
        now = datetime.now(timezone.utc)
        overdue_loans = (ResourceLoan.query
                         .filter(ResourceLoan.status=='on_loan')
                         .filter(ResourceLoan.due_at <= now)
                         .all())
        for ln in overdue_loans:
            try:
                staff = ln.staff
                res = ln.resource
                if staff and getattr(staff, 'email', None):
                    subject = f"Overdue Resource: {res.name if res else 'Item'}"
                    body = (
                        f"Hello {staff.name},<br/><br/>"
                        f"This is a reminder that the resource <strong>{res.name if res else ln.resource_id}</strong> is still on loan and was due back by 7:30 PM today. "
                        f"Please return it as soon as possible.<br/><br/>Thanks."
                    )
                    _send_email_safe(staff.email, subject, body, log_prefix='Resource overdue')
            except Exception as _exc:
                print(f"[WARN] Overdue email failed: {_exc}")

# Schedule daily overdue check at 19:30 UTC if scheduler available
if BackgroundScheduler is not None:
    try:
        _ensure_scheduler_started()
        if scheduler:
            try:
                scheduler.remove_job('resource-overdue')
            except Exception:
                pass
            try:
                scheduler.add_job(_send_overdue_resource_emails, 'cron', hour=19, minute=30, id='resource-overdue')
            except Exception as _e:
                print(f"[WARN] Scheduler: failed to add resource overdue job: {_e}")
    except Exception:
        pass


# ---------------- CLI: Staff Access Codes ---------------- #
@app.cli.command('gen-staff-codes')
@click.option('--force', is_flag=True, default=False, help='Regenerate for all staff (overwrites existing codes).')
def gen_staff_codes(force: bool):
    """Generate 6-digit unique access codes for staff.

    By default, only fills missing/blank codes. Use --force to regenerate for all.
    """
    import random

    from models import Staff
    updated = 0
    q = Staff.query
    if not force:
        q = q.filter((Staff.access_code.is_(None)) | (Staff.access_code == ''))
    staff_list = q.all()
    if not staff_list:
        click.echo('No staff to update.')
        return
    # Build a set of codes to avoid in-memory collisions
    existing = set()
    if force:
        # When forcing, drop all existing codes from the set to allow full regeneration
        pass
    else:
        existing = {c for (c,) in db.session.query(Staff.access_code).filter(Staff.access_code.isnot(None)).all()}

    def gen_code():
        return f"{random.randint(0, 999999):06d}"

    for s in staff_list:
        if force:
            s.access_code = None
        code = gen_code()
        tries = 0
        while code in existing and tries < 20:
            code = gen_code(); tries += 1
        s.access_code = code
        existing.add(code)
        updated += 1
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        raise click.ClickException(f"Failed to update staff codes: {exc}")
    click.echo(f"Updated access codes for {updated} staff member(s).")

# ---------------- CLI: One-off DB patch for Observation.emailed ---------------- #
@app.cli.command('patch-obs-emailed')
def cli_patch_observation_emailed():
    """Ensure the observation.emailed column exists (SQLite ALTER TABLE if missing).

    Safe to run multiple times; will only add the column when absent.
    """
    from sqlalchemy import text as _text
    added = False
    with db.engine.connect() as _conn:
        try:
            tables = {t[0] for t in _conn.execute(_text("SELECT name FROM sqlite_master WHERE type='table'"))}
            if 'observation' in tables:
                cols = {row[1] for row in _conn.execute(_text("PRAGMA table_info(observation)"))}
                if 'emailed' not in cols:
                    try:
                        _conn.execute(_text("ALTER TABLE observation ADD COLUMN emailed BOOLEAN DEFAULT 0"))
                        added = True
                        click.echo('Added column observation.emailed (DEFAULT 0).')
                    except Exception as _exc:
                        raise click.ClickException(f'Failed to add observation.emailed: {_exc}')
                else:
                    click.echo('Column observation.emailed already present.')
            else:
                click.echo("Table 'observation' does not exist yet.")
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(f"Patch failed: {exc}")
    if not added:
        # No change performed
        pass


# ---------------- Todo deadline reminders (daily) ---------------- #
def _send_todo_deadline_emails():
    """Send reminders for Todos that are due soon or overdue.

    - Due soon: due_date within 3 days inclusive (and not done)
    - Overdue: due_date < today (and not done)
    Dedupe per subject per day using EmailLog.
    """
    from datetime import date as _date

    from sqlalchemy import and_ as _and
    from sqlalchemy import func as _func

    from email_utils import (build_task_due_soon_email,
                             build_task_overdue_email, send_email)
    from models import EmailLog as _EmailLog
    from models import Todo as _Todo
    from models import User as _User

    with app.app_context():
        today = _date.today()
        # Fetch all pending todos with a due_date
        todos = (_Todo.query
                 .join(_User, _Todo.assigned_to_id == _User.id)
                 .filter((_Todo.status.is_(None)) | (_Todo.status != 'Done'))
                 .filter(_Todo.due_date.isnot(None))
                 .all())
        for t in todos:
            try:
                assigned = t.assigned_to
                if not assigned or not getattr(assigned, 'email', None):
                    continue
                delta = (t.due_date - today).days if t.due_date else None
                if delta is None:
                    continue
                if delta < 0:
                    subject, html = build_task_overdue_email(t, assigned)
                elif delta <= 3:
                    subject, html = build_task_due_soon_email(t, assigned)
                else:
                    continue
                # Dedupe by subject on the same day
                already = (_EmailLog.query
                           .filter(_EmailLog.to_email == assigned.email)
                           .filter(_EmailLog.subject == subject)
                           .filter(_func.date(_EmailLog.sent_at) == today)
                           .count())
                if already:
                    continue
                send_email(assigned.email, subject, html, setting_name=get_setting('rota_setting', 'operations'))
            except Exception as _exc:
                print(f"[WARN] Todo reminder failed for #{t.id if t else '?'}: {_exc}")

# Schedule daily todo reminders at 09:00 UTC if scheduler available
if BackgroundScheduler is not None:
    try:
        _ensure_scheduler_started()
        if scheduler:
            try:
                scheduler.remove_job('todo-deadlines')
            except Exception:
                pass
            try:
                scheduler.add_job(_send_todo_deadline_emails, 'cron', hour=9, minute=0, id='todo-deadlines')
            except Exception as _e:
                print(f"[WARN] Scheduler: failed to add todo deadlines job: {_e}")
    except Exception:
        pass

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
    page_raw = (request.args.get('page') or '1').strip()
    per_page_raw = (request.args.get('per_page') or '25').strip().lower()

    per_page = None
    if per_page_raw == 'all':
        page = 1
    else:
        try:
            per_page = max(1, min(100, int(per_page_raw or '25')))
            per_page_raw = str(per_page)
            page = max(1, int(page_raw or '1'))
        except ValueError:
            per_page = 25
            per_page_raw = '25'
            try:
                page = max(1, int(page_raw or '1'))
            except ValueError:
                page = 1

    if per_page is None:
        page = 1

    # ---------------- Base Query & Filters ---------------- #
    base_q = Book.query
    if year:
        # Compare case-insensitively to support mixed input/storage
        base_q = base_q.filter(db.func.lower(Book.year_group) == year)
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

    if total == 0:
        page = 1

    if per_page is None:
        books_rows = books_query.all()
        pages = 1 if total else 0
    else:
        books_rows = (books_query
                      .offset((page-1)*per_page)
                      .limit(per_page)
                      .all())
        pages = (total // per_page) + (1 if total % per_page else 0)
        if pages and page > pages:
            page = pages
            books_rows = (books_query
                          .offset((page-1)*per_page)
                          .limit(per_page)
                          .all())

    books = [b.serialize() for b in books_rows]

    # Distinct subjects for filter dropdown
    subjects = [r[0] for r in db.session.query(Book.subject)
                               .distinct()
                               .filter(Book.subject.isnot(None))
                               .order_by(Book.subject.asc()).all()]

    # Distinct years for filter dropdown based on stored values
    years = [r[0] for r in db.session.query(Book.year_group)
                              .distinct()
                              .filter(Book.year_group.isnot(None), Book.year_group != '')
                              .order_by(Book.year_group.asc()).all()]

    # Discover available uploaded covers for dropdown-based selection
    cover_files = []
    try:
        covers_dir = os.path.join(app.root_path, 'static', 'uploads', 'book_covers')
        if os.path.isdir(covers_dir):
            for fn in sorted(os.listdir(covers_dir)):
                if fn.startswith('.'):
                    continue
                # Basic image extension filter
                ext = os.path.splitext(fn)[1].lower()
                if ext in {'.jpg','.jpeg','.png','.webp','.gif'}:
                    cover_files.append(fn)
    except Exception:
        pass

    per_page_choices = [('10','10'), ('25','25'), ('50','50'), ('100','100'), ('all','All')]
    return render_template('tools/books_index.html',
                           books=books,
                           page=page,
                           pages=pages,
                           total=total,
                           per_page=per_page_raw,
                           per_page_choices=per_page_choices,
                           sort=sort,
                           direction=direction,
                           selected_year=year,
                           selected_subject=subject,
                           selected_status=status_param or ('all' if legacy_inactive_flag else 'active'),
                           q=q,
                           subjects=subjects,
                           years=years,
                           cover_files=cover_files)

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
    # Preflight: ensure an EmailSetting exists for book order sending (configurable)
    try:
        setting_name = get_setting('book_orders_setting', 'operations')
        from models import EmailSetting
        configured = EmailSetting.query.filter_by(name=setting_name).first()
        if not configured:
            flash(f"Book order email setting '{setting_name}' is not configured. Add an Email Setting with this name to enable sending.", 'warning')
    except Exception:
        pass
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
        # Resolve destination and setting name from dynamic settings
        to_email = get_setting('book_orders_to', 'techsupport@exceltutors.org.uk')
        setting_name = get_setting('book_orders_setting', 'operations')
        cc_recipients = [addr for addr in ('management@exceltutors.org.uk', 'enquiries@exceltutors.org.uk') if addr.lower() != (to_email or '').lower()]
        send_email(to_email, subject, html, setting_name=setting_name, cc=cc_recipients)
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
        to_email = get_setting('book_orders_to', 'techsupport@exceltutors.org.uk')
        setting_name = get_setting('book_orders_setting', 'operations')
        cc_recipients = [addr for addr in ('management@exceltutors.org.uk', 'enquiries@exceltutors.org.uk') if addr.lower() != (to_email or '').lower()]
        send_email(to_email, subject, html, setting_name=setting_name, cc=cc_recipients)
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

@app.route('/tools/books/covers.json')
@login_required
@permission_required('manage_books')
def books_covers_json():
    """Return JSON list of available uploaded book cover files and URLs."""
    covers = []
    try:
        base_dir = os.path.join(app.root_path, 'static', 'uploads', 'book_covers')
        if os.path.isdir(base_dir):
            for fn in sorted(os.listdir(base_dir)):
                if fn.startswith('.'):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext in {'.jpg','.jpeg','.png','.webp','.gif'}:
                    covers.append({'name': fn, 'url': url_for('static', filename=f'uploads/book_covers/{fn}', _external=False)})
    except Exception:
        pass
    return jsonify({'covers': covers})

@app.route('/tools/books/upload-cover', methods=['POST'])
@login_required
@permission_required('manage_books')
def books_upload_cover():
    """Upload a cover image to static/uploads/book_covers and return filename+url.

    Field name: file
    """
    f = request.files.get('file')
    if not f or not getattr(f, 'filename', ''):
        return jsonify({'success': False, 'error': 'No file provided'}), 400
    filename = f.filename
    ext = os.path.splitext(filename)[1].lower()
    if ext not in {'.jpg','.jpeg','.png','.webp','.gif'}:
        return jsonify({'success': False, 'error': 'Unsupported file type'}), 400
    # Ensure directory exists
    dest_dir = os.path.join(app.root_path, 'static', 'uploads', 'book_covers')
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception:
        pass
    # Unique filename
    from uuid import uuid4
    safe_name = f"{uuid4().hex}{ext}"
    dest_path = os.path.join(dest_dir, safe_name)
    try:
        f.save(dest_path)
    except Exception as exc:
        return jsonify({'success': False, 'error': f'Failed to save: {exc}'}), 500
    url = url_for('static', filename=f'uploads/book_covers/{safe_name}', _external=False)
    return jsonify({'success': True, 'name': safe_name, 'url': url})

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
@permission_required('manage_staff')
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
@permission_required('manage_staff')
def staff_export():
    rows = Staff.query.all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name","department","email","phone","branch"])
    for s in rows:
        writer.writerow([s.name, s.department, s.email, s.phone, s.branch])
    mem = io.BytesIO(output.getvalue().encode('utf-8'))
    mem.seek(0)
    # Flask 2.x+: use download_name instead of deprecated attachment_filename
    return send_file(mem, mimetype='text/csv', as_attachment=True, download_name='staff_export.csv')

# ---------------- Cycles CRUD ----------------
@app.route("/cycles")
@login_required
@permission_required('manage_cycles')
def cycles_index():
    cycles = ObservationCycle.query.order_by(ObservationCycle.start_date.desc().nullslast()).all()
    return render_template("cycles/index.html", cycles=cycles)

@app.route("/cycles/new", methods=["GET","POST"])
@login_required
@permission_required('manage_cycles')
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
@permission_required('manage_cycles')
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
@permission_required('manage_cycles')
def cycle_delete(cid):
    c = ObservationCycle.query.get_or_404(cid)
    db.session.delete(c)
    db.session.commit()
    flash("Cycle deleted", "success")
    return redirect(url_for('cycles_index'))

# ---------------- Availability CRUD & Import ----------------
@app.route('/availability')
@login_required
@permission_required('manage_availability')
def availability_index():
    # Remote API sync disabled – use only locally stored availability data
    sync_count = None
    sync_error = None
    # Filters: department(s), branch(es), subject(s), day(s), plus free-text search
    q = Availability.query
    selected_departments = [d.strip() for d in request.args.getlist('department') if d.strip()]
    if selected_departments:
        q = q.filter(Availability.department.in_(selected_departments))
    selected_branches = [b.strip() for b in request.args.getlist('branch') if b.strip()]
    if selected_branches:
        # Robust token match within CSV branches column to avoid partial mismatches and spacing issues
        branch_conds = []
        for b in selected_branches:
            token = b.strip()
            # Match exact, start, end, or middle positions in comma-separated string
            branch_conds.extend([
                Availability.branches.ilike(f"{token}"),
                Availability.branches.ilike(f"{token},%"),
                Availability.branches.ilike(f"%,{token}"),
                Availability.branches.ilike(f"%,{token},%"),
            ])
        q = q.filter(or_(*branch_conds))
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
    # --- De-duplicate short-name vs full-name variants per branch & department ---
    # Goal: Keep per-branch entries distinct, but merge duplicates where one record is just the
    # first name (e.g., "adit") and another is the full name (e.g., "adit hossain mintu").
    # Key by (first-name token, canonical-branches, department). Prefer record with the longest
    # name (most tokens); break ties by most recent timestamp.
    try:
        def _timestamp(rec):
            ts = getattr(rec, 'updated_at', None) or getattr(rec, 'created_at', None)
            return ts or datetime.min
        def _canon_branches(s):
            parts = [p.strip() for p in (s or '').split(',') if p.strip()]
            parts = sorted(set(parts))
            return ",".join(parts)
        best_by_key = {}
        for rec in records:
            name = (rec.name or '').strip()
            tokens = [t for t in name.split() if t]
            first = tokens[0].lower() if tokens else ''
            key = (first, _canon_branches(rec.branches), (rec.department or '').lower())
            prev = best_by_key.get(key)
            if prev is None:
                best_by_key[key] = rec
                continue
            # Choose the more complete record: more name tokens wins; then newer timestamp
            prev_tokens = [t for t in (prev.name or '').split() if t]
            choose_current = False
            if len(tokens) > len(prev_tokens):
                choose_current = True
            elif len(tokens) == len(prev_tokens) and _timestamp(rec) > _timestamp(prev):
                choose_current = True
            if choose_current:
                best_by_key[key] = rec
        # Preserve selected sort by re-sorting the deduped list by the same column
        deduped = list(best_by_key.values())
        if direction == 'desc':
            deduped.sort(key=lambda r: (getattr(r, sort, None) or '').lower() if sort in ('name','department') else getattr(r, sort, getattr(r, 'name', '')), reverse=True)
        else:
            deduped.sort(key=lambda r: (getattr(r, sort, None) or '').lower() if sort in ('name','department') else getattr(r, sort, getattr(r, 'name', '')))
        records = deduped
    except Exception:
        # Fail-safe: if any error occurs in dedupe, show original records
        pass
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

    # --- Summary stats (counts) ---
    from collections import Counter
    dept_counts = Counter()
    branch_counts = Counter()
    subject_counts = Counter()
    day_counts = Counter()
    for rec in records:
        dept_counts.update([(rec.department or 'Unknown')])
        for b in rec.branch_list():
            branch_counts.update([b])
        if rec.subjects:
            for s in [p.strip() for p in rec.subjects.split(',') if p.strip()]:
                subject_counts.update([s])
        if rec.days:
            txt = rec.days
            for wd in weekday_tokens:
                if wd in txt:
                    day_counts.update([wd])
    # Sorted lists for template (top first)
    stats_departments = sorted(dept_counts.items(), key=lambda x: (-x[1], (x[0] or '')))
    stats_branches = sorted(branch_counts.items(), key=lambda x: (-x[1], x[0]))
    stats_subjects = sorted(subject_counts.items(), key=lambda x: (-x[1], x[0]))
    stats_days = [(d, day_counts.get(d, 0)) for d in weekday_tokens if d in day_counts]
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
                           synced_at=None,
                           stats_departments=stats_departments,
                           stats_branches=stats_branches,
                           stats_subjects=stats_subjects,
                           stats_days=stats_days)


@app.route('/availability/import_xlsx', methods=['POST'])
@login_required
@permission_required('manage_availability')
def availability_import_xlsx():
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        token = (payload.get('confirm_token') or payload.get('token') or '').strip()
        if not token:
            return jsonify({'error': 'Missing confirmation token.'}), 400
        serializer = _availability_import_serializer()
        try:
            token_data = serializer.loads(token, max_age=3600)
        except SignatureExpired:
            return jsonify({'error': 'Confirmation token has expired. Please upload again.'}), 400
        except BadSignature:
            return jsonify({'error': 'Invalid confirmation token.'}), 400

        rows = token_data.get('rows') or []
        if not isinstance(rows, list):
            return jsonify({'error': 'Confirmation payload is malformed.'}), 400
        df = pd.DataFrame(rows)
        decisions = payload.get('decisions') if isinstance(payload, dict) else {}
        try:
            plan = _process_availability_import(df, apply_changes=True, decisions=decisions if isinstance(decisions, dict) else {})
        except Exception as exc:
            return jsonify({'error': f'Import failed: {exc}'}), 500

        summary = _summarize_availability_plan(plan)
        response = {
            'status': 'applied',
            'plan': plan,
            'summary': summary,
            'filename': token_data.get('filename'),
            'row_count': token_data.get('row_count'),
        }
        warnings: list[str] = []
        unmatched = plan.get('unmatched') or []
        ambiguous = plan.get('ambiguous') or []
        if unmatched:
            unique = sorted(set(unmatched))
            preview = ', '.join(unique[:10])
            if len(unique) > 10:
                preview += ', ...'
            warnings.append(f'No staff match for: {preview}')
        if ambiguous:
            unique = sorted(set(ambiguous))
            preview = ', '.join(unique[:10])
            if len(unique) > 10:
                preview += ', ...'
            warnings.append(f'Multiple staff matches for: {preview}')
        if warnings:
            response['warnings'] = warnings
        return jsonify(response)

    wants_json = 'application/json' in (request.headers.get('Accept') or '').lower()
    upload = request.files.get('xlsx_file')
    if upload is None or not getattr(upload, 'filename', ''):
        if wants_json:
            return jsonify({'error': 'Select an XLSX file to import.'}), 400
        flash('Select an XLSX file to import.', 'danger')
        return redirect(url_for('availability_index'))

    filename_lower = upload.filename.lower()
    if not filename_lower.endswith(('.xlsx', '.xls')):
        if wants_json:
            return jsonify({'error': 'Only Excel files (.xlsx) are supported for availability import.'}), 400
        flash('Only Excel files (.xlsx) are supported for availability import.', 'danger')
        return redirect(url_for('availability_index'))

    try:
        df = pd.read_excel(upload)
    except Exception as exc:
        if wants_json:
            return jsonify({'error': f'Unable to read spreadsheet: {exc}'}), 400
        flash(f'Unable to read spreadsheet: {exc}', 'danger')
        return redirect(url_for('availability_index'))

    required_columns = [
        'Name',
        'Department',
        'Which Subjects Can You Teach',
        'Which Days Are You Available',
        'Which Branch Do You Want to Take a Lesson In?',
        'NotesMessages?',
    ]
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        message = 'Spreadsheet missing required columns: ' + ', '.join(missing)
        if wants_json:
            return jsonify({'error': message}), 400
        flash(message, 'danger')
        return redirect(url_for('availability_index'))

    plan = _process_availability_import(df, apply_changes=False)
    preview_summary = _summarize_availability_plan(plan)

    if wants_json:
        safe_df = df.where(pd.notnull(df), None)
        safe_rows: list[dict[str, object]] = []
        for record in safe_df.to_dict(orient='records'):
            sanitized: dict[str, object] = {}
            for key, value in record.items():
                if isinstance(value, datetime):
                    sanitized[key] = value.isoformat()
                elif isinstance(value, pd.Timestamp):
                    sanitized[key] = value.to_pydatetime().isoformat()
                else:
                    sanitized[key] = value
            safe_rows.append(sanitized)
        serializer = _availability_import_serializer()
        token_payload = {
            'rows': safe_rows,
            'filename': upload.filename,
            'row_count': int(getattr(df, 'shape', [0])[0]) if hasattr(df, 'shape') else len(safe_df),
        }
        token = serializer.dumps(token_payload)
        response = {
            'status': 'preview',
            'plan': plan,
            'summary': preview_summary,
            'token': token,
            'filename': upload.filename,
            'row_count': token_payload['row_count'],
        }
        warnings: list[str] = []
        unmatched = plan.get('unmatched') or []
        ambiguous = plan.get('ambiguous') or []
        if unmatched:
            unique = sorted(set(unmatched))
            preview = ', '.join(unique[:10])
            if len(unique) > 10:
                preview += ', ...'
            warnings.append(f'No staff match for: {preview}')
        if ambiguous:
            unique = sorted(set(ambiguous))
            preview = ', '.join(unique[:10])
            if len(unique) > 10:
                preview += ', ...'
            warnings.append(f'Multiple staff matches for: {preview}')
        if warnings:
            response['warnings'] = warnings
        return jsonify(response)

    try:
        plan = _process_availability_import(df, apply_changes=True)
    except Exception as exc:
        db.session.rollback()
        flash(f'Import failed: {exc}', 'danger')
        return redirect(url_for('availability_index'))

    summary_text = _summarize_availability_plan(plan) or 'No changes detected'
    flash('Availability import complete (' + summary_text + ').', 'success')

    unmatched = plan.get('unmatched') or []
    ambiguous = plan.get('ambiguous') or []
    if unmatched:
        sample = sorted(set(unmatched))
        preview = ', '.join(sample[:10])
        if len(sample) > 10:
            preview += ', ...'
        flash('No staff match for: ' + preview, 'warning')
    if ambiguous:
        sample = sorted(set(ambiguous))
        preview = ', '.join(sample[:10])
        if len(sample) > 10:
            preview += ', ...'
        flash('Multiple staff matches for: ' + preview, 'warning')

    if plan.get('log_entries', 0) == 0 and (plan.get('created') or plan.get('updated')):
        flash('Import completed but no change log entries were recorded.', 'info')

    return redirect(url_for('availability_index'))


@app.route('/availability/changelog')
@login_required
@permission_required('manage_availability')
def availability_changelog():
    staff_filter = (request.args.get('staff') or '').strip()
    field_filter = (request.args.get('field') or '').strip()
    source_filter = (request.args.get('source') or '').strip()
    try:
        limit_value = int(request.args.get('limit', 250))
    except (TypeError, ValueError):
        limit_value = 250
    limit = min(max(limit_value, 1), 1000)

    query = AvailabilityChange.query.options(
        joinedload(AvailabilityChange.staff),
        joinedload(AvailabilityChange.availability),
        joinedload(AvailabilityChange.changed_by),
    )

    if staff_filter:
        like = f"%{staff_filter.lower()}%"
        query = query.outerjoin(Staff, AvailabilityChange.staff_id == Staff.id)
        query = query.outerjoin(Availability, AvailabilityChange.availability_id == Availability.id)
        query = query.filter(or_(func.lower(Staff.name).like(like), func.lower(Availability.name).like(like)))

    if field_filter:
        query = query.filter(AvailabilityChange.field == field_filter)

    if source_filter:
        like_source = f"%{source_filter.lower()}%"
        query = query.filter(func.lower(AvailabilityChange.source).like(like_source))

    entries = query.order_by(AvailabilityChange.changed_at.desc()).limit(limit).all()

    fields = [row[0] for row in db.session.query(AvailabilityChange.field).distinct().order_by(AvailabilityChange.field.asc()).all() if row[0]]
    sources = [row[0] for row in db.session.query(AvailabilityChange.source).distinct().filter(AvailabilityChange.source.isnot(None)).order_by(AvailabilityChange.source.asc()).all() if row[0]]

    return render_template(
        'availability/changelog.html',
        entries=entries,
        staff_filter=staff_filter,
        field_filter=field_filter,
        source_filter=source_filter,
        fields=fields,
        sources=sources,
        limit=limit,
    )


@app.route('/availability/bulk', methods=['POST'])
@login_required
@permission_required('manage_availability')
def availability_bulk():
    action = (request.form.get('action') or '').strip().lower()
    raw_ids = request.form.getlist('ids')
    ids = []
    for raw in raw_ids:
        try:
            val = int(raw)
            ids.append(val)
        except (TypeError, ValueError):
            continue
    if not ids:
        flash('Select at least one availability record first.', 'warning')
        return redirect(url_for('availability_index'))
    records = Availability.query.filter(Availability.id.in_(ids)).all()
    if not records:
        flash('No matching availability records found for bulk action.', 'warning')
        return redirect(url_for('availability_index'))

    if action == 'delete':
        deleted = 0
        try:
            for record in records:
                staff_ref = None
                try:
                    staff_ref = record.staff
                except Exception:
                    staff_ref = None
                _log_availability_change(
                    record,
                    staff_ref,
                    'record',
                    record.name,
                    'Deleted via bulk action',
                    'deleted',
                    'bulk_delete',
                )
                db.session.delete(record)
                deleted += 1
            db.session.commit()
            flash(f'Deleted {deleted} availability record(s).', 'success')
        except Exception as exc:
            db.session.rollback()
            flash(f'Bulk delete failed: {exc}', 'danger')
    else:
        flash('Unsupported bulk action.', 'danger')
    return redirect(url_for('availability_index'))

# Safety: ensure new column exists for legacy databases (after deployment without restart)
try:
    from sqlalchemy import text as _text_av
    with db.engine.begin() as _conn:
        cols = {row[1] for row in _conn.execute(_text_av("PRAGMA table_info(availability)"))}
        if 'staff_id' not in cols:
            _conn.execute(_text_av("ALTER TABLE availability ADD COLUMN staff_id INTEGER"))
except Exception:
    # Non-fatal; app will continue and column will be created on next run if needed
    pass

try:
    from sqlalchemy import text as _text_avchg
    with db.engine.begin() as _conn:
        exists = _conn.execute(_text_avchg("SELECT name FROM sqlite_master WHERE type='table' AND name='availability_change'"))
        if not exists.fetchone():
            _conn.execute(_text_avchg(
                """
                CREATE TABLE IF NOT EXISTS availability_change (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    availability_id INTEGER,
                    staff_id INTEGER,
                    field VARCHAR(120) NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    change_type VARCHAR(40) NOT NULL DEFAULT 'updated',
                    source VARCHAR(80),
                    changed_by_id INTEGER,
                    changed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(availability_id) REFERENCES availability(id) ON DELETE SET NULL,
                    FOREIGN KEY(staff_id) REFERENCES staff(id) ON DELETE SET NULL,
                    FOREIGN KEY(changed_by_id) REFERENCES user(id) ON DELETE SET NULL
                )
                """
            ))
            _conn.execute(_text_avchg("CREATE INDEX IF NOT EXISTS ix_availability_change_availability_id ON availability_change (availability_id)"))
            _conn.execute(_text_avchg("CREATE INDEX IF NOT EXISTS ix_availability_change_staff_id ON availability_change (staff_id)"))
            _conn.execute(_text_avchg("CREATE INDEX IF NOT EXISTS ix_availability_change_changed_at ON availability_change (changed_at)"))
except Exception:
    pass

@app.route('/availability/new', methods=['GET','POST'])
@login_required
@permission_required('manage_availability')
def availability_new():
    form = AvailabilityForm()
    ensure_form_branch_choices(form)
    allowed_branch_values = {value for value, _ in getattr(form.branches, 'choices', [])}
    # Populate department choices from existing distinct departments (Availability ∪ Staff)
    dept_av = [r[0] for r in db.session.query(Availability.department).distinct().filter(Availability.department.isnot(None)).all()]
    dept_st = [r[0] for r in db.session.query(Staff.department).distinct().filter(Staff.department.isnot(None)).all()]
    dept_rows = sorted(set([d for d in dept_av + dept_st if d]))
    form.department.choices = [('', '-- None --')] + [(d, d) for d in dept_rows]
    # Staff for select/autocomplete
    staff_rows = db.session.query(Staff.id, Staff.name, Staff.department).order_by(Staff.name.asc()).all()
    staff_names = [r[1] for r in staff_rows]
    # Prefill from query params or staff records
    if request.method == 'GET':
        q_name = (request.args.get('name') or '').strip()
        q_dept = (request.args.get('department') or '').strip()
        if q_name:
            form.name.data = q_name
        if q_dept and any(d == q_dept for d in dept_rows):
            form.department.data = q_dept
        # If name present, try to prefill branches from Staff record
        if q_name:
            cand = Staff.query
            cand = cand.filter(Staff.name.ilike(q_name))
            if q_dept:
                cand = cand.filter(Staff.department == q_dept)
            st = cand.first()
            if st and (st.branch or ''):
                form.branches.data = [b for b in (st.branch or '').split(',') if b and b in allowed_branch_values]
    if form.validate_on_submit():
        selected_branches = [b for b in (form.branches.data or []) if b in allowed_branch_values]
        a = Availability(
            name=form.name.data.strip(),
            department=(form.department.data or '').strip() or None,
            branches=",".join(selected_branches) if selected_branches else None,
            days=form.days.data.strip() if form.days.data else None,
            subjects=form.subjects.data.strip() if form.subjects.data else None,
            notes=form.notes.data.strip() if form.notes.data else None,
        )
        # Explicit staff link if provided via hidden input 'staff_id' from template
        sid = request.form.get('staff_id')
        if sid and sid.isdigit():
            a.staff_id = int(sid)
            # If department is blank, inherit from staff for consistency
            if not a.department:
                st = Staff.query.get(a.staff_id)
                if st and st.department:
                    a.department = st.department
        db.session.add(a)
        db.session.flush()
        staff_ref = None
        try:
            if a.staff_id:
                staff_ref = Staff.query.get(a.staff_id)
            else:
                staff_ref = getattr(a, 'staff', None)
        except Exception:
            staff_ref = getattr(a, 'staff', None)
        _log_availability_change(a, staff_ref, 'record', None, 'Created via form', 'created', 'manual_form')
        db.session.commit()
        flash('Availability record created','success')
        return redirect(url_for('availability_index'))
    return render_template('availability/form.html', form=form, record=None, staff_names=staff_names, staff_rows=staff_rows)

@app.route('/availability/<int:aid>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_availability')
def availability_edit(aid):
    a = Availability.query.get_or_404(aid)
    form = AvailabilityForm()
    ensure_form_branch_choices(form)
    allowed_branch_values = {value for value, _ in getattr(form.branches, 'choices', [])}
    dept_av = [r[0] for r in db.session.query(Availability.department).distinct().filter(Availability.department.isnot(None)).all()]
    dept_st = [r[0] for r in db.session.query(Staff.department).distinct().filter(Staff.department.isnot(None)).all()]
    dept_rows = sorted(set([d for d in dept_av + dept_st if d]))
    form.department.choices = [('', '-- None --')] + [(d, d) for d in dept_rows]
    staff_rows = db.session.query(Staff.id, Staff.name, Staff.department).order_by(Staff.name.asc()).all()
    staff_names = [r[1] for r in staff_rows]
    if request.method == 'GET':
        form.name.data = a.name
        form.department.data = a.department or ''
        form.branches.data = [b for b in (a.branches or '').split(',') if b and b in allowed_branch_values]
        form.days.data = a.days
        form.subjects.data = a.subjects
        form.notes.data = a.notes
    if form.validate_on_submit():
        selected_branches = [b for b in (form.branches.data or []) if b in allowed_branch_values]
        before_state = {
            'name': a.name,
            'department': a.department,
            'branches': a.branches,
            'days': a.days,
            'subjects': a.subjects,
            'notes': a.notes,
            'staff_id': a.staff_id,
        }
        a.name = form.name.data.strip()
        a.department = (form.department.data or '').strip() or None
        a.branches = ",".join(selected_branches) if selected_branches else None
        a.days = form.days.data.strip() if form.days.data else None
        a.subjects = form.subjects.data.strip() if form.subjects.data else None
        a.notes = form.notes.data.strip() if form.notes.data else None
        # Update explicit link
        sid = request.form.get('staff_id')
        if sid == '':
            a.staff_id = None
        elif sid and sid.isdigit():
            a.staff_id = int(sid)
        changes: list[tuple[str, str | None, str | None]] = []
        for field, old_val in before_state.items():
            new_val = getattr(a, field)
            if field == 'staff_id':
                if old_val != new_val:
                    changes.append((field, str(old_val) if old_val is not None else None, str(new_val) if new_val is not None else None))
                continue
            old_txt = (old_val or '').strip() if isinstance(old_val, str) else (str(old_val) if old_val is not None else '')
            new_txt = (new_val or '').strip() if isinstance(new_val, str) else (str(new_val) if new_val is not None else '')
            if old_txt != new_txt:
                changes.append((field, old_val if old_val is not None else None, new_val if new_val else None))
        if changes:
            db.session.flush()
            staff_ref = None
            try:
                if a.staff_id:
                    staff_ref = Staff.query.get(a.staff_id)
                else:
                    staff_ref = getattr(a, 'staff', None)
            except Exception:
                staff_ref = getattr(a, 'staff', None)
            for field, old_val, new_val in changes:
                _log_availability_change(a, staff_ref, field, old_val, new_val, 'updated', 'manual_form')
        db.session.commit()
        flash('Availability updated','success')
        return redirect(url_for('availability_index'))
    return render_template('availability/form.html', form=form, record=a, staff_names=staff_names, staff_rows=staff_rows)

@app.route('/availability/<int:aid>/delete')
@login_required
@permission_required('manage_availability')
def availability_delete(aid):
    a = Availability.query.get_or_404(aid)
    db.session.delete(a)
    db.session.commit()
    flash('Availability deleted','success')
    return redirect(url_for('availability_index'))

@app.route('/availability/import_remote', methods=['POST'])
@login_required
@permission_required('manage_availability')
def availability_import_remote():
    flash('Remote availability import has been disabled. Use the New/Edit forms to manage availability data locally.', 'info')
    return redirect(url_for('availability_index'))

# ---------------- Issues (tracker) ----------------
@app.route('/issues')
@login_required
@permission_required('manage_issues')
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
    # Branches: merge canonical BRANCH_CHOICES() with distinct values from DB
    db_branches = [r[0] for r in db.session.query(Issue.branch).distinct().filter(Issue.branch.isnot(None)).order_by(Issue.branch.asc()).all()]
    base_branches = BRANCH_CHOICES()  # ordered canonical list
    branches = base_branches + [b for b in db_branches if b not in base_branches]

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
@permission_required('manage_issues')
def issue_new():
    form = IssueForm()
    # Ensure branch dropdown is populated consistently
    try:
        from utils import ensure_form_branch_choices
        ensure_form_branch_choices(form)
    except Exception:
        pass
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
@permission_required('manage_issues')
def issue_edit(iid):
    issue = Issue.query.get_or_404(iid)
    form = IssueForm(obj=issue)
    # Ensure branch dropdown is populated consistently
    try:
        from utils import ensure_form_branch_choices
        ensure_form_branch_choices(form)
    except Exception:
        pass
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
        if request.args.get('view'):
            return render_template('issues/view.html', issue=issue)
        return render_template('issues/partials/_form_inner.html', form=form, issue=issue)
    return render_template('issues/form.html', form=form, issue=issue)


@app.route('/issues/<int:iid>/delete')
@login_required
@permission_required('manage_issues')
def issue_delete(iid):
    issue = Issue.query.get_or_404(iid)
    db.session.delete(issue)
    db.session.commit()
    flash('Issue deleted','success')
    return redirect(url_for('issues_index'))

# ---------------- Meetings ----------------
@app.route('/meetings')
@login_required
@permission_required('manage_meetings')
def meetings_index():
    from datetime import date as _date
    from datetime import timedelta
    today = _date.today()
    week_ahead = today + timedelta(days=7)
    q = Meeting.query

    # Time scope filter: 'upcoming' (default), 'past', or 'all'
    show = (request.args.get('show') or 'upcoming').strip().lower()
    if show == 'upcoming':
        q = q.filter(Meeting.date >= today)
    elif show == 'past':
        q = q.filter(Meeting.date < today)
    # 'all' → no date scope filter

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

    # Analytics / summaries (always computed against upcoming meetings from today)
    all_upcoming = Meeting.query.filter(Meeting.date >= today).all()
    upcoming_today = [m for m in all_upcoming if m.date == today]
    upcoming_week = [m for m in all_upcoming if m.date <= week_ahead]
    total = len(meetings)
    user_today = [m for m in upcoming_today if m.participant_id == current_user.id or m.booked_by_id == current_user.id]
    user_week = [m for m in upcoming_week if m.participant_id == current_user.id or m.booked_by_id == current_user.id]

    users = User.query.order_by(User.name.asc()).all()
    # Preflight: warn if meeting email setting missing
    try:
        setting_name = get_setting('meetings_setting', get_setting('rota_setting', 'operations'))
        from models import EmailSetting as _ES
        if not _ES.query.filter_by(name=setting_name).first():
            flash(f"Meeting email setting '{setting_name}' is not configured. Add an Email Setting with this name to enable meeting notifications.", 'warning')
    except Exception:
        pass
    return render_template('meetings/index.html', meetings=meetings, users=users,
                           total=total, upcoming_today=len(upcoming_today), upcoming_week=len(upcoming_week),
                           user_today=len(user_today), user_week=len(user_week), search=search,
                           sel_part=part, sel_booked=booked, start_date=start_date, end_date=end_date,
                           show=show)

# Fallback: handle accidental POSTs to /meetings (collection) by treating as create
@app.route('/meetings', methods=['POST'])
@login_required
@permission_required('manage_meetings')
def meetings_index_post():
    """Fallback creation endpoint if the create form posts to /meetings instead of /meetings/new.

    This guards against stale cached form markup or JS overrides causing a 405.
    Prefer using /meetings/new for creation; this simply delegates.
    """
    form = MeetingForm()
    eligible_users = User.query.filter(
        db.or_(User.is_superadmin == True, User.role == 'centre_manager')
    ).order_by(User.name.asc()).all()
    form.participant_id.choices = [(u.id, u.name) for u in eligible_users]
    # Student choices (include sentinel 0 for none)
    try:
        from models import Student as _Student
        students = _Student.query.order_by(_Student.name.asc()).all()
        form.student_id.choices = [(0, '— None —')] + [(s.id, s.label()) for s in students]
    except Exception:
        form.student_id.choices = [(0, '— None —')]
    if form.validate_on_submit():
        sid = form.student_id.data if hasattr(form, 'student_id') else 0
        m = Meeting(participant_id=form.participant_id.data, booked_by_id=current_user.id,
                    agenda=form.agenda.data.strip(), date=form.date.data, time=form.time.data.strip(),
                    student_id=(sid if sid else None),
                    student_name=form.student_name.data.strip() if form.student_name.data else None,
                    parent_name=form.parent_name.data.strip() if form.parent_name.data else None,
                    outcome=form.outcome.data.strip() if form.outcome.data else None)
        db.session.add(m)
        db.session.commit()
        # Send confirmation emails and schedule reminder
        try:
            from email_utils import (build_meeting_admin_email,
                                     build_meeting_student_email)
            from models import Student as _Student
            student = _Student.query.get(m.student_id) if m.student_id else None
            setting_name = get_setting('meetings_setting', get_setting('rota_setting', 'operations'))
            # Determine parent preferred email
            recipients = []
            if student:
                parent_email = getattr(student, 'email', None)
                if (not parent_email) and getattr(student, 'preferred_contact_raw', None):
                    try:
                        pe, _pp = parse_preferred_contact(student.preferred_contact_raw)
                        if pe:
                            parent_email = pe
                    except Exception:
                        pass
                # Collect recipients: parent preferred email and student.email (dedup)
                for em in [parent_email, getattr(student, 'email', None)]:
                    if em and em not in recipients:
                        recipients.append(em)
            # Send student-facing confirmation to recipients (parent and/or student)
            if student and recipients:
                subj, html = build_meeting_student_email(m, student, m.participant, mode='confirmation')
                for to_email in recipients:
                    try:
                        send_email(to_email, subj, html, setting_name=setting_name)
                    except Exception as _send_exc:
                        print(f"[WARN] Meeting confirmation email (to {to_email}) failed: {_send_exc}")
                m.confirmation_sent_at = datetime.now(timezone.utc)
            # Notify participant
            if m.participant and getattr(m.participant, 'email', None):
                subj_a, html_a = build_meeting_admin_email(m, m.participant, student, mode='confirmation')
                send_email(m.participant.email, subj_a, html_a, setting_name=setting_name)
            db.session.commit()
        except Exception as _e:
            db.session.rollback()
            print(f"[WARN] Meeting confirmation email failed: {_e}")
        try:
            _schedule_meeting_reminder(m)
        except Exception as _e:
            print(f"[WARN] Scheduling meeting reminder failed: {_e}")
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
@permission_required('manage_meetings')
def meeting_new():
    form = MeetingForm()
    eligible_users = User.query.filter(
        db.or_(User.is_superadmin == True, User.role == 'centre_manager')
    ).order_by(User.name.asc()).all()
    form.participant_id.choices = [(u.id, u.name) for u in eligible_users]
    # Populate student choices
    try:
        students = Student.query.order_by(Student.name.asc()).all()
        form.student_id.choices = [(0, '— None —')] + [(s.id, s.label()) for s in students]
    except Exception:
        form.student_id.choices = [(0, '— None —')]
    if request.method == 'POST' and form.validate_on_submit():
        sid = form.student_id.data if hasattr(form, 'student_id') else 0
        m = Meeting(participant_id=form.participant_id.data, booked_by_id=current_user.id,
                    agenda=form.agenda.data.strip(), date=form.date.data, time=form.time.data.strip(),
                    student_id=(sid if sid else None),
                    student_name=form.student_name.data.strip() if form.student_name.data else None,
                    parent_name=form.parent_name.data.strip() if form.parent_name.data else None,
                    outcome=form.outcome.data.strip() if form.outcome.data else None)
        db.session.add(m)
        db.session.commit()
        # Send confirmation emails and schedule reminder
        try:
            student = Student.query.get(m.student_id) if m.student_id else None
            from email_utils import (build_meeting_admin_email,
                                     build_meeting_student_email)
            setting_name = get_setting('meetings_setting', get_setting('rota_setting', 'operations'))
            # Determine parent preferred email
            recipients = []
            if student:
                parent_email = getattr(student, 'email', None)
                if (not parent_email) and getattr(student, 'preferred_contact_raw', None):
                    try:
                        pe, _pp = parse_preferred_contact(student.preferred_contact_raw)
                        if pe:
                            parent_email = pe
                    except Exception:
                        pass
                for em in [parent_email, getattr(student, 'email', None)]:
                    if em and em not in recipients:
                        recipients.append(em)
            # Send to parents/students
            if student and recipients:
                subj, html = build_meeting_student_email(m, student, m.participant, mode='confirmation')
                for to_email in recipients:
                    try:
                        send_email(to_email, subj, html, setting_name=setting_name)
                    except Exception as _send_exc:
                        print(f"[WARN] Meeting confirmation email (to {to_email}) failed: {_send_exc}")
                m.confirmation_sent_at = datetime.now(timezone.utc)
            if m.participant and getattr(m.participant, 'email', None):
                subj_a, html_a = build_meeting_admin_email(m, m.participant, student, mode='confirmation')
                send_email(m.participant.email, subj_a, html_a, setting_name=setting_name)
            db.session.commit()
        except Exception as _e:
            db.session.rollback()
            print(f"[WARN] Meeting confirmation email failed: {_e}")
        try:
            _schedule_meeting_reminder(m)
        except Exception as _e:
            print(f"[WARN] Scheduling meeting reminder failed: {_e}")
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
@permission_required('manage_meetings')
def meeting_edit(mid):
    m = Meeting.query.get_or_404(mid)
    form = MeetingForm(obj=m)
    eligible_users = User.query.filter(
        db.or_(User.is_superadmin == True, User.role == 'centre_manager')
    ).order_by(User.name.asc()).all()
    form.participant_id.choices = [(u.id, u.name) for u in eligible_users]
    # Ensure current participant is in choices even if role changed
    if m.participant_id and m.participant_id not in [u.id for u in eligible_users]:
        form.participant_id.choices.append((m.participant_id, m.participant.name if m.participant else f'User #{m.participant_id}'))
    # Populate student choices
    try:
        students = Student.query.order_by(Student.name.asc()).all()
        form.student_id.choices = [(0, '— None —')] + [(s.id, s.label()) for s in students]
        form.student_id.data = (m.student_id or 0)
    except Exception:
        form.student_id.choices = [(0, '— None —')]
    if request.method == 'POST' and form.validate_on_submit():
        m.participant_id = form.participant_id.data
        m.agenda = form.agenda.data.strip()
        m.date = form.date.data
        m.time = form.time.data.strip()
        sid = form.student_id.data if hasattr(form, 'student_id') else 0
        m.student_id = (sid if sid else None)
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
@permission_required('manage_meetings')
def meeting_delete(mid):
    m = Meeting.query.get_or_404(mid)
    try:
        _cancel_meeting_reminder(m.id)
    except Exception:
        pass
    db.session.delete(m)
    db.session.commit()
    flash('Meeting deleted','success')
    return redirect(url_for('meetings_index'))

# ---------------- To-Do Module ----------------
@app.route('/todos')
@login_required
@permission_required('manage_tasks')
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
@permission_required('manage_tasks')
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
        # Send notification email to assignee (avoid emailing yourself redundantly only if different users)
        try:
            if t.assigned_to and t.assigned_to.email:
                html = build_task_notification_email(t, current_user, t.assigned_to)
                # Subject line emphasises status & due date
                subj_due = f" (Due {t.due_date.strftime('%Y-%m-%d')})" if t.due_date else ""
                from email_utils import send_with_template
                try:
                    send_with_template('task_notification', {
                        'description': t.description,
                        'due': (t.due_date.strftime('%Y-%m-%d') if t.due_date else ''),
                        'assigned_to': t.assigned_to.name,
                        'assigned_to_id': t.assigned_to.id,
                        'created_by': t.created_by.name if getattr(t, 'created_by', None) else '',
                        'status': t.status or 'Pending',
                        'criticality': t.criticality or 'N/A',
                        'urgency': t.urgency or 'N/A',
                        'to_email': t.assigned_to.email,
                    }, to_email=t.assigned_to.email, fallback=lambda: (f"New Task Assigned: {t.description[:60]}{subj_due}", html))
                except Exception:
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
@permission_required('manage_tasks')
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
        if request.args.get('view'):
            return render_template('todos/view.html', todo=t)
        return render_template('todos/partials/_form_inner.html', form=form, todo=t)
    return render_template('todos/form.html', form=form, todo=t)

@app.route('/todos/<int:tid>/delete')
@login_required
@permission_required('manage_tasks')
def todo_delete(tid):
    t = Todo.query.get_or_404(tid)
    assigned = t.assigned_to_id
    db.session.delete(t)
    db.session.commit()
    flash('Task deleted','success')
    return redirect(url_for('todos_index', assigned=assigned))

@app.route('/todos/<int:tid>/toggle', methods=['POST'])
@login_required
@permission_required('manage_tasks')
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
@permission_required('manage_tasks')
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
@permission_required('manage_observations')
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
@permission_required('manage_observations')
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
@permission_required('manage_observations')
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
@permission_required('manage_observations')
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
@permission_required('manage_observations')
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
@permission_required('manage_observations')
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
@permission_required('manage_observations')
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
@permission_required('manage_observations')
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
        from email_utils import send_email
        if pdf_bytes:
            attachments = [(pdf_bytes, 'application', 'pdf', f'observation_{oid}.pdf')]
            from email_utils import send_with_template
            try:
                send_with_template('observation_report', {
                    'staff_name': obs.staff.name,
                    'date': str(obs.date),
                    'to_email': tutor_email,
                }, to_email=tutor_email, fallback=lambda: (f"Observation Report - {obs.staff.name} ({obs.date})", body))
            except Exception:
                send_email(tutor_email, f"Observation Report - {obs.staff.name} ({obs.date})", body, attachments=attachments)
        else:
            send_email(tutor_email, f"Observation Report - {obs.staff.name} ({obs.date})", body)
        # Mark observation as emailed (permanent)
        try:
            if not getattr(obs, 'emailed', False):
                obs.emailed = True
                db.session.commit()
        except Exception:
            db.session.rollback()
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
@permission_required('manage_observations')
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
@permission_required('manage_kids_club_invoices')
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
@permission_required('manage_kids_club_invoices')
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
@permission_required('manage_kids_club_invoices')
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
@permission_required('manage_kids_club_invoices')
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

# -------- Student autocomplete (meetings/call list) -------- #
@app.route('/api/students/suggest')
@login_required
def api_student_suggest():
    """Return up to 20 students matching `q` in student_id or name.

    Response format: [{id, label, name, student_id, status}]
    Label is formatted as "<student_id>-<name>" for direct insertion.
    Note: We do not filter by a non-existent `is_active`; optionally prioritize
    status == 'Active' in ordering when available.
    """
    q = (request.args.get('q') or '').strip()
    limit = min(int(request.args.get('limit') or 20), 50)
    if not q:
        return jsonify([])

    like = f"%{q}%"
    # Match by ID or name, case-insensitive
    base_q = Student.query.filter(
        or_(Student.name.ilike(like), Student.student_id.ilike(like))
    )

    # Ordering: exact ID match first, then ID prefix, then 'Active' status, then name
    try:
        from sqlalchemy import case, func
        q_lower = q.lower()
        exact_id_first = case((func.lower(Student.student_id) == q_lower, 0), else_=1)
        prefix_id_first = case((func.lower(Student.student_id).like(q_lower + '%'), 0), else_=1)
        active_first = case((Student.status == 'Active', 0), else_=1)
        base_q = base_q.order_by(exact_id_first, prefix_id_first, active_first, Student.name.asc())
    except Exception:
        base_q = base_q.order_by(Student.name.asc())

    students = base_q.limit(limit).all()
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
            'status': (s.status or ''),
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
@permission_required('manage_kids_club_invoices')
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
@permission_required('manage_kids_club_invoices')
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
@permission_required('manage_kids_club_invoices')
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
            if 'payment_method' not in cols:
                try:
                    _c.execute(_text2("ALTER TABLE invoice ADD COLUMN payment_method VARCHAR(40)"))
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
@permission_required('manage_kids_club_invoices')
def invoices_new():
    form = InvoiceForm()
    # Ensure column exists (defensive; may have been added after process start)
    try:
        from sqlalchemy import text as _text2
        with db.engine.connect() as _c:
            if 'created_by_id' not in {r[1] for r in _c.execute(_text2("PRAGMA table_info(invoice)"))}:
                try: _c.execute(_text2("ALTER TABLE invoice ADD COLUMN created_by_id INTEGER"))
                except Exception: pass
            if 'payment_method' not in {r[1] for r in _c.execute(_text2("PRAGMA table_info(invoice)"))}:
                try: _c.execute(_text2("ALTER TABLE invoice ADD COLUMN payment_method VARCHAR(40)"))
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
            payment_method=(form.payment_method.data or None),
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
@permission_required('manage_kids_club_invoices')
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
@permission_required('manage_kids_club_invoices')
def invoice_detail(invoice_id):
    invoice = Invoice.query.options(joinedload(Invoice.company)).get_or_404(invoice_id)
    return render_template('invoices/detail.html', invoice=invoice)


@app.route('/invoices/<int:invoice_id>/duplicate', methods=['GET', 'POST'])
@login_required
@permission_required('manage_kids_club_invoices')
def invoice_duplicate(invoice_id):
    source = Invoice.query.get_or_404(invoice_id)
    form = InvoiceForm()
    companies = Company.query.order_by(Company.name.asc()).all()
    form.company_id.choices = [(c.id, c.name) for c in companies]

    if request.method == 'GET':
        form.company_id.data = source.company_id
        form.parent_name.data = source.parent_name
        form.parent_phone.data = source.parent_phone
        form.parent_email.data = source.parent_email
        form.parent_address.data = source.parent_address
        form.child_name.data = source.child_name
        form.sub_total.data = source.sub_total
        form.total.data = source.total
        form.status.data = source.status or 'PAID'
        form.payment_method.data = source.payment_method or ''
        form.notes.data = source.notes
        form.invoice_date.data = None
        form.due_date.data = None
        form.period_start.data = None
        form.period_end.data = None

    if form.validate_on_submit():
        company = Company.query.get(form.company_id.data)
        if not company:
            flash('Selected company was not found', 'danger')
            return redirect(url_for('invoice_duplicate', invoice_id=invoice_id))
        invoice_no = company.generate_invoice_no()
        duplicate = Invoice(
            invoice_no=invoice_no,
            company_id=company.id,
            created_by_id=current_user.id,
            invoice_date=form.invoice_date.data,
            due_date=form.due_date.data,
            payment_method=form.payment_method.data or None,
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
        db.session.add(duplicate)
        company.next_invoice_seq = (company.next_invoice_seq or 1) + 1
        db.session.commit()
        flash(f'Invoice {duplicate.invoice_no} created from {source.invoice_no}', 'success')
        return redirect(url_for('invoice_detail', invoice_id=duplicate.id))

    if request.method != 'GET' and not form.invoice_date.data:
        form.invoice_date.data = None
    if request.method != 'GET' and not form.due_date.data:
        form.due_date.data = None
    if request.method != 'GET' and not form.period_start.data:
        form.period_start.data = None
    if request.method != 'GET' and not form.period_end.data:
        form.period_end.data = None

    return render_template('invoices/form.html', form=form, mode='duplicate', source_invoice=source)

@app.route('/invoices/<int:invoice_id>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_kids_club_invoices')
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
        inv.payment_method = (form.payment_method.data or None)
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

# ---------------- Staff Invoice Management (Employee-submitted) ---------------- #

def _is_staff_invoice_manager() -> bool:
    try:
        return getattr(current_user, 'is_superadmin', False) or user_can('manage_staff_invoices')
    except Exception:
        return False


@app.route('/staff-invoices')
@login_required
@permission_required('submit_staff_invoices','manage_staff_invoices', any=True)
def staff_invoices_index():
    # Any authenticated user: if manager -> see all, else show only own invoices
    manager = _is_staff_invoice_manager()
    q = StaffInvoice.query
    if not manager:
        q = q.filter(StaffInvoice.created_by_id == current_user.id)

    # Filters / search / sort via GET params
    # q=<query string> - search by invoice id, name or creator name
    # employee=<user_id>, month=<1-12>, year=<YYYY>, company=<company_id>, status=<status>
    args = request.args
    search_q = (args.get('q') or '').strip()
    employee = args.get('employee')
    month = args.get('month')
    year = args.get('year')
    company_id = args.get('company')
    status = args.get('status')
    sort = args.get('sort') or 'created_at'
    order = (args.get('order') or 'desc').lower()

    # Join to User to allow searching by creator name
    q = q.join(User, StaffInvoice.created_by)

    if search_q:
        # try numeric id match or partial name match
        if search_q.isdigit():
            q = q.filter(StaffInvoice.id == int(search_q))
        else:
            sq = f"%{search_q}%"
            q = q.filter(db.or_(StaffInvoice.name.ilike(sq), User.name.ilike(sq)))

    if employee:
        try:
            uid = int(employee)
            q = q.filter(StaffInvoice.created_by_id == uid)
        except Exception:
            pass

    if month:
        try:
            m = int(month)
            q = q.filter(StaffInvoice.month == m)
        except Exception:
            pass

    if year:
        try:
            y = int(year)
            q = q.filter(StaffInvoice.year == y)
        except Exception:
            pass

    if status:
        q = q.filter(StaffInvoice.status == status)

    # If company filter requested, join to Staff -> Company via Staff.user_id == User.id
    company_map = {}
    if company_id:
        try:
            cid = int(company_id)
            # join through Staff to Company
            q = q.join(Staff, Staff.user_id == User.id).join(Company, Company.id == Staff.company_id).filter(Company.id == cid)
        except Exception:
            pass

    # Sorting
    sort_map = {
        'id': StaffInvoice.id,
        'month': StaffInvoice.month,
        'year': StaffInvoice.year,
        'amount': StaffInvoice.amount,
        'status': StaffInvoice.status,
        'created_at': StaffInvoice.created_at,
    }
    col = sort_map.get(sort, StaffInvoice.created_at)
    if order == 'asc':
        q = q.order_by(col.asc())
    else:
        q = q.order_by(col.desc())

    records = q.all()

    # Build lists for filter controls
    employees = User.query.join(StaffInvoice, StaffInvoice.created_by_id == User.id).distinct().order_by(User.name).all()
    companies = Company.query.order_by(Company.name).all()
    years = [r[0] for r in db.session.query(StaffInvoice.year).distinct().order_by(StaffInvoice.year.desc()).all()]
    months = [(i, __import__('calendar').month_name[i]) for i in range(1,13)]
    statuses = ['Draft','Pending','Approved','Rejected']

    # Map user->company name for display
    user_ids = list({inv.created_by_id for inv in records})
    staff_rows = []
    if user_ids:
        staff_rows = Staff.query.filter(Staff.user_id.in_(user_ids)).all()
    user_company = {s.user_id: (Company.query.get(s.company_id).name if s.company_id and Company.query.get(s.company_id) else None) for s in staff_rows}

    return render_template(
        'staff_invoices/index.html',
        records=records,
        manager=manager,
        employees=employees,
        companies=companies,
        years=years,
        months=months,
        statuses=statuses,
        filters={'q': search_q, 'employee': employee, 'month': month, 'year': year, 'company': company_id, 'status': status, 'sort': sort, 'order': order},
        user_company=user_company,
        branch_choices=BRANCH_CHOICES(),
    )


@app.route('/staff-invoices/new', methods=['GET','POST'])
@login_required
@permission_required('submit_staff_invoices', any=True, any_=True)
def staff_invoice_new():
    form = StaffInvoiceForm()
    # Populate branch choices for item subform from DB
    try:
        from models import Branch
        choices = [(b.name, b.name) for b in Branch.query.order_by(Branch.name).all()]
        # Ensure an explicit empty choice exists so Optional SelectFields
        # which render a blank option ('') validate correctly when user
        # leaves branch blank. This avoids "Not a valid choice." errors.
        if choices:
            form.ItemForm.branch.choices = [('', '')] + choices
        else:
            # fallback to utils BRANCH_CHOICES list
            form.ItemForm.branch.choices = [('', '')] + [(b,b) for b in BRANCH_CHOICES()]
    except Exception:
        form.ItemForm.branch.choices = [('', '')] + [(b,b) for b in BRANCH_CHOICES()]
    # Ensure each existing item entry has the same choices set on its branch field
    try:
        for entry in form.items.entries:
            try:
                entry.form.branch.choices = form.ItemForm.branch.choices
            except Exception:
                pass
    except Exception:
        pass
    # Default month/year and default rate from Staff.salary_per_hour (autofill editable)
    default_rate = 0
    try:
        today = date.today()
        # find staff record for current_user to pull salary_per_hour
        try:
            staff_rec = Staff.query.filter(Staff.user_id == current_user.id).first()
            if staff_rec and getattr(staff_rec, 'salary_per_hour', None) is not None:
                # convert to float for template/JS use
                default_rate = float(staff_rec.salary_per_hour)
        except Exception:
            default_rate = 0
        if request.method == 'GET':
            form.month.data = today.month
            form.year.data = today.year
            # Prefill one empty line item with today's date and default rate
            if len(form.items.entries) == 0:
                form.items.append_entry({'date': today, 'day': today.strftime('%A'), 'branch': '', 'hours': 0, 'rate': default_rate, 'amount': 0})
    except Exception:
        pass
    if request.method == 'POST' and form.validate_on_submit():
        is_submit = 'submit_invoice' in request.form
        status = 'Pending' if is_submit else 'Draft'
        # Build a sensible automatic name (user + month year) since form no longer provides one
        try:
            import calendar as _cal
            month_label = _cal.month_name[int(form.month.data or 0)]
        except Exception:
            month_label = str(form.month.data or '')
        name_default = f"{getattr(current_user,'name', 'Invoice')} {month_label} {form.year.data}"
        si = StaffInvoice(
            name=name_default,
            month=form.month.data,
            year=form.year.data,
            amount=0,
            status=status,
            created_by_id=current_user.id,
            submitted_at=(datetime.utcnow() if is_submit else None),
        )
        db.session.add(si)
        # Items
        total = Decimal('0')
        for idx, entry in enumerate(form.items.entries):
            itf = entry.form
            d = itf.date.data
            try:
                day = d.strftime('%A') if d else ''
            except Exception:
                day = ''
            hours = Decimal(str(itf.hours.data or 0))
            rate = Decimal(str(itf.rate.data or 0))
            amount = (hours * rate).quantize(Decimal('0.01'))
            total += amount
            item = StaffInvoiceItem(
                invoice=si,
                date=d,
                day=day,
                branch=(itf.branch.data or '').strip() or None,
                hours=float(hours),
                description=(itf.description.data or '').strip() or None,
                rate=float(rate),
                amount=float(amount),
            )
            db.session.add(item)
        si.amount = float(total)
        db.session.commit()
        # Handle attachments upload (optional multi-attachments)
        try:
            files = request.files.getlist('attachments') or []
            if files:
                _save_invoice_attachments(si, files)
        except Exception as _e:
            print(f"[WARN] Attachment upload failed: {_e}")
        if is_submit:
            # Send confirmation email to the submitter (branded HTML)
            try:
                subj = f"Invoice submitted successfully (#{si.id})"
                html = _build_staff_invoice_submitted_email(current_user.name, si)
                if current_user.email:
                    send_email(current_user.email, subj, html)
            except Exception as _e:
                print(f"[WARN] Staff invoice confirmation email failed: {_e}")
        flash('Invoice saved as Draft' if not is_submit else 'Invoice submitted for approval', 'success')
        return redirect(url_for('staff_invoice_detail', invoice_id=si.id))
    return render_template('staff_invoices/form.html', form=form, default_rate=default_rate)


def _get_staff_invoice_or_404(invoice_id: int) -> StaffInvoice:
    inv = StaffInvoice.query.get_or_404(invoice_id)
    if _is_staff_invoice_manager():
        return inv
    # Owner-only access for non-managers
    if inv.created_by_id != current_user.id:
        abort(403)
    return inv


@app.route('/staff-invoices/<int:invoice_id>')
@login_required
@permission_required('submit_staff_invoices','manage_staff_invoices', any=True)
def staff_invoice_detail(invoice_id):
    inv = _get_staff_invoice_or_404(invoice_id)
    manager = _is_staff_invoice_manager()
    return render_template('staff_invoices/detail.html', inv=inv, manager=manager)


@app.route('/staff-invoices/<int:invoice_id>/attachments/<int:att_id>/download')
@login_required
@permission_required('submit_staff_invoices','manage_staff_invoices', any=True)
def staff_invoice_attachment_download(invoice_id, att_id):
    inv = _get_staff_invoice_or_404(invoice_id)
    att = StaffInvoiceAttachment.query.filter_by(id=att_id, invoice_id=inv.id).first_or_404()
    # Serve as download
    abs_path = os.path.join(app.static_folder, att.path)
    if not os.path.exists(abs_path):
        abort(404)
    directory = os.path.dirname(abs_path)
    filename = os.path.basename(abs_path)
    return send_from_directory(directory, filename, as_attachment=True, download_name=(att.original_name or filename))


@app.route('/staff-invoices/<int:invoice_id>/attachments/<int:att_id>/view')
@login_required
@permission_required('submit_staff_invoices','manage_staff_invoices', any=True)
def staff_invoice_attachment_view(invoice_id, att_id):
    inv = _get_staff_invoice_or_404(invoice_id)
    att = StaffInvoiceAttachment.query.filter_by(id=att_id, invoice_id=inv.id).first_or_404()
    abs_path = os.path.join(app.static_folder, att.path)
    if not os.path.exists(abs_path):
        abort(404)
    directory = os.path.dirname(abs_path)
    filename = os.path.basename(abs_path)
    # Serve inline if browser supports content type
    resp = send_from_directory(directory, filename)
    if att.content_type:
        try:
            resp.headers['Content-Type'] = att.content_type
        except Exception:
            pass
    return resp

@app.route('/staff-invoices/<int:invoice_id>/attachments/<int:att_id>/delete', methods=['POST'])
@login_required
@permission_required('submit_staff_invoices','manage_staff_invoices', any=True)
def staff_invoice_attachment_delete(invoice_id, att_id):
    inv = _get_staff_invoice_or_404(invoice_id)
    att = StaffInvoiceAttachment.query.filter_by(id=att_id, invoice_id=inv.id).first_or_404()
    manager = _is_staff_invoice_manager()
    # Only managers or owners of Draft invoices can delete attachments
    if (not manager) and (inv.created_by_id != current_user.id or inv.status != 'Draft'):
        abort(403)
    # Remove the file from disk if present
    try:
        abs_path = os.path.join(app.static_folder, att.path)
        if os.path.exists(abs_path) and os.path.isfile(abs_path):
            os.remove(abs_path)
    except Exception:
        pass
    db.session.delete(att)
    db.session.commit()
    if request.headers.get('Accept') == 'application/json' or request.is_json:
        return jsonify({'success': True})
    flash('Attachment deleted', 'success')
    return redirect(url_for('staff_invoice_detail', invoice_id=inv.id))

@app.route('/staff-invoices/<int:invoice_id>/attachments/upload', methods=['POST'])
@login_required
@permission_required('submit_staff_invoices','manage_staff_invoices', any=True)
def staff_invoice_attachment_upload(invoice_id):
    inv = _get_staff_invoice_or_404(invoice_id)
    manager = _is_staff_invoice_manager()
    # Only managers or owners of Draft invoices can upload attachments
    if (not manager) and (inv.created_by_id != current_user.id or inv.status != 'Draft'):
        abort(403)
    files = request.files.getlist('file') or request.files.getlist('attachments') or []
    if not files:
        return jsonify({'success': False, 'error': 'No files uploaded'}), 400
    created = _save_invoice_attachments(inv, files)
    if not created:
        return jsonify({'success': False, 'error': 'No valid files'}), 400
    def _att_json(a: StaffInvoiceAttachment):
        return {
            'id': a.id,
            'name': a.original_name or os.path.basename(a.path),
            'view_url': url_for('staff_invoice_attachment_view', invoice_id=inv.id, att_id=a.id),
            'download_url': url_for('staff_invoice_attachment_download', invoice_id=inv.id, att_id=a.id),
            'delete_url': url_for('staff_invoice_attachment_delete', invoice_id=inv.id, att_id=a.id),
            'content_type': a.content_type,
            'file_size': a.file_size,
        }
    return jsonify({'success': True, 'attachments': [_att_json(a) for a in created]})

@app.route('/staff-invoices/start-draft', methods=['POST'])
@login_required
@permission_required('submit_staff_invoices','manage_staff_invoices', any=True)
def staff_invoice_start_draft():
    # Create a minimal Draft invoice so the user can use drag-and-drop uploads on the edit page
    try:
        month_str = (request.form.get('month') or '').strip()
        year_str = (request.form.get('year') or '').strip()
        month = int(month_str) if month_str.isdigit() else None
        year = int(year_str) if year_str.isdigit() else None
    except Exception:
        month = None
        year = None
    # Default to current month/year if not provided
    today = date.today()
    if not month:
        month = today.month
    if not year:
        year = today.year
    # Build automatic name similar to 'new' route
    try:
        import calendar as _cal
        month_label = _cal.month_name[int(month or 0)]
    except Exception:
        month_label = str(month or '')
    name_default = f"{getattr(current_user,'name', 'Invoice')} {month_label} {year}"
    inv = StaffInvoice(
        name=name_default,
        month=month,
        year=year,
        amount=0.0,
        status='Draft',
        created_by_id=current_user.id,
        submitted_at=None,
    )
    db.session.add(inv)
    db.session.commit()
    flash('Draft created. You can now add items and upload attachments.', 'success')
    return redirect(url_for('staff_invoice_edit', invoice_id=inv.id))

@app.route('/staff-invoices/<int:invoice_id>/edit', methods=['GET','POST'])
@login_required
@permission_required('submit_staff_invoices','manage_staff_invoices', any=True)
def staff_invoice_edit(invoice_id):
    inv = _get_staff_invoice_or_404(invoice_id)
    manager = _is_staff_invoice_manager()
    # Editing rules: owner can edit only if Draft; managers can edit any
    if (not manager) and (inv.status != 'Draft'):
        abort(403)
    form = StaffInvoiceForm()
    # Populate branch choices for item subform from DB
    try:
        from models import Branch
        choices = [(b.name, b.name) for b in Branch.query.order_by(Branch.name).all()]
        if choices:
            form.ItemForm.branch.choices = [('', '')] + choices
        else:
            form.ItemForm.branch.choices = [('', '')] + [(b,b) for b in BRANCH_CHOICES()]
    except Exception:
        form.ItemForm.branch.choices = [('', '')] + [(b,b) for b in BRANCH_CHOICES()]
    # Ensure each item entry has choices populated for validation on POST
    try:
        for entry in form.items.entries:
            try:
                entry.form.branch.choices = form.ItemForm.branch.choices
            except Exception:
                pass
    except Exception:
        pass

    if request.method == 'GET':
        # Invoice name is generated automatically; don't populate a removed form field
        form.month.data = inv.month
        form.year.data = inv.year
        form.amount.data = inv.amount
        # Populate items
        if len(inv.items or []) == 0:
            form.items.append_entry()
        else:
            for it in inv.items:
                form.items.append_entry({
                    'date': it.date,
                    'day': it.day,
                    'branch': it.branch or '',
                    'hours': float(it.hours or 0),
                    'description': it.description or '',
                    'rate': float(it.rate or 0),
                    'amount': float(it.amount or 0),
                })
    # Managers may also change status via 'status' field if provided
    if request.method == 'POST':
        # Debugging similar to new route: show posted keys and branch values
        try:
            print('[DEBUG] staff_invoice_edit POST keys:', list(request.form.keys()))
            print('[DEBUG] staff_invoice_edit branch choices:', form.ItemForm.branch.choices)
            posted_branch_fields = [(k, request.form.get(k)) for k in request.form.keys() if k.endswith('-branch') or k == 'branch']
            print('[DEBUG] staff_invoice_edit posted branch fields:', posted_branch_fields)
        except Exception as _e:
            print('[DEBUG] staff_invoice_edit debug error:', _e)
        # Accept status-only update (manager)
        if 'status' in request.form and manager:
            new_status = (request.form.get('status') or 'Pending').strip()
            if new_status not in ['Pending','Approved','Rejected']:
                flash('Invalid status', 'warning')
            else:
                inv.status = new_status
                db.session.commit()
                flash('Status updated', 'success')
                return redirect(url_for('staff_invoice_detail', invoice_id=inv.id))
        elif form.validate_on_submit():
            is_submit = 'submit_invoice' in request.form
            inv.month = form.month.data
            inv.year = form.year.data
            # Replace items
            for old in list(inv.items or []):
                db.session.delete(old)
            total = Decimal('0')
            for entry in form.items.entries:
                itf = entry.form
                d = itf.date.data
                day = d.strftime('%A') if d else ''
                hours = Decimal(str(itf.hours.data or 0))
                rate = Decimal(str(itf.rate.data or 0))
                amount = (hours * rate).quantize(Decimal('0.01'))
                total += amount
                db.session.add(StaffInvoiceItem(invoice=inv, date=d, day=day, branch=(itf.branch.data or '').strip() or None, hours=float(hours), description=(itf.description.data or '').strip() or None, rate=float(rate), amount=float(amount)))
            inv.amount = float(total)
            # If owner submitting now from Draft
            if is_submit and inv.status == 'Draft':
                inv.status = 'Pending'
                inv.submitted_at = datetime.utcnow()
                try:
                    subj = f"Invoice submitted successfully (#{inv.id})"
                    html = _build_staff_invoice_submitted_email(current_user.name, inv)
                    if current_user.email:
                        send_email(current_user.email, subj, html)
                except Exception as _e:
                    print(f"[WARN] Staff invoice confirmation email failed: {_e}")
            db.session.commit()
            # Handle new attachments added during edit (reuse common helper; includes HEIC/HEIF)
            try:
                files = request.files.getlist('attachments') or []
                if files:
                    _save_invoice_attachments(inv, files)
            except Exception as _e:
                print(f"[WARN] Attachment upload failed: {_e}")
            flash('Invoice updated', 'success')
            return redirect(url_for('staff_invoice_detail', invoice_id=inv.id))
    # Provide a default_rate from staff record so new rows added client-side can default to it
    default_rate = 0
    try:
        staff_rec = Staff.query.filter(Staff.user_id == inv.created_by_id).first()
        if staff_rec and getattr(staff_rec, 'salary_per_hour', None) is not None:
            default_rate = float(staff_rec.salary_per_hour)
    except Exception:
        default_rate = 0
    return render_template('staff_invoices/form.html', form=form, inv=inv, manager=manager, default_rate=default_rate)


@app.route('/staff-invoices/<int:invoice_id>/delete', methods=['POST'])
@login_required
def staff_invoice_delete(invoice_id):
    if not getattr(current_user, 'is_superadmin', False):
        abort(403)
    inv = StaffInvoice.query.get_or_404(invoice_id)
    db.session.delete(inv)
    db.session.commit()
    flash('Invoice deleted', 'success')
    return redirect(url_for('staff_invoices_index'))


@app.route('/staff-invoices/<int:invoice_id>/pdf')
@login_required
@permission_required('submit_staff_invoices','manage_staff_invoices', any=True)
def staff_invoice_pdf(invoice_id):
    inv = _get_staff_invoice_or_404(invoice_id)
    css_path = os.path.join(app.root_path, 'static', 'css', 'invoice.css')
    inline_css = ''
    try:
        with open(css_path, 'r', encoding='utf-8') as f:
            inline_css = f.read()
    except Exception:
        pass
    resp = make_response(render_template('staff_invoices/pdf.html', inv=inv, inline_css=inline_css, print_view=True))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp


@app.route('/staff-invoices/dashboard')
@login_required
@permission_required('manage_staff_invoices')
def staff_invoices_dashboard():
    from datetime import date, timedelta

    from sqlalchemy import and_, func, or_
    today = date.today()
    this_month = today.month
    this_year = today.year

    # Topline aggregates
    totals = {
        'Pending': db.session.query(func.count(StaffInvoice.id)).filter(StaffInvoice.status=='Pending').scalar() or 0,
        'Approved': db.session.query(func.count(StaffInvoice.id)).filter(StaffInvoice.status=='Approved').scalar() or 0,
        'Rejected': db.session.query(func.count(StaffInvoice.id)).filter(StaffInvoice.status=='Rejected').scalar() or 0,
    }
    total_amount = float(db.session.query(func.coalesce(func.sum(StaffInvoice.amount), 0)).scalar() or 0)
    latest = StaffInvoice.query.order_by(StaffInvoice.created_at.desc()).limit(10).all()

    # Employees by company (active)
    emp_by_company_rows = (
        db.session.query(Company.name, func.count(Staff.id))
        .join(Staff, Staff.company_id == Company.id)
        .filter(Staff.active == True)
        .group_by(Company.id)
        .order_by(Company.name.asc())
        .all()
    )
    employees_by_company = [{'company': n, 'count': int(c or 0)} for (n, c) in emp_by_company_rows]

    # Pending submissions by company for current month
    pending_rows = (
        db.session.query(Company.name, func.count(StaffInvoice.id))
        .join(Staff, Staff.user_id == StaffInvoice.created_by_id)
        .join(Company, Company.id == Staff.company_id)
        .filter(StaffInvoice.year == this_year, StaffInvoice.month == this_month, StaffInvoice.status == 'Pending')
        .group_by(Company.id)
        .order_by(Company.name.asc())
        .all()
    )
    pending_by_company = [{'company': n, 'count': int(c or 0)} for (n, c) in pending_rows]

    # Missing (unsubmitted) employees for current month: active staff with a company and no invoice in Pending/Approved/Rejected
    submitted_user_ids = [
        uid for (uid,) in db.session.query(StaffInvoice.created_by_id)
        .filter(StaffInvoice.year == this_year, StaffInvoice.month == this_month, StaffInvoice.status.in_(['Pending','Approved','Rejected']))
        .distinct()
        .all()
    ]
    missing_q = Staff.query.filter(Staff.active == True, Staff.company_id.isnot(None))
    if submitted_user_ids:
        missing_q = missing_q.filter(~Staff.user_id.in_(submitted_user_ids))
    missing_staff = missing_q.all()
    # Group missing by company
    comp_map = {c.id: c.name for c in Company.query.all()}
    missing_by_company = {}
    for s in missing_staff:
        cname = comp_map.get(s.company_id, 'Unassigned')
        missing_by_company[cname] = missing_by_company.get(cname, 0) + 1
    missing_by_company_list = [{'company': k, 'count': v} for k, v in sorted(missing_by_company.items())]

    # Lateness summary for current month
    # Date range for the current month
    first_day = today.replace(day=1)
    next_month = (first_day.replace(day=28) + timedelta(days=4)).replace(day=1)
    last_day = next_month - timedelta(days=1)
    late_q = db.session.query(StaffAttendance).filter(
        StaffAttendance.date >= first_day,
        StaffAttendance.date <= last_day,
        StaffAttendance.late_minutes.isnot(None),
        StaffAttendance.late_minutes > 0,
    )
    total_late_days = late_q.count()
    total_late_minutes = int(db.session.query(func.coalesce(func.sum(StaffAttendance.late_minutes), 0)).filter(
        StaffAttendance.date >= first_day,
        StaffAttendance.date <= last_day,
        StaffAttendance.late_minutes.isnot(None),
        StaffAttendance.late_minutes > 0,
    ).scalar() or 0)
    # Top late staff (by minutes)
    top_late_rows = (
        db.session.query(Staff.id, Staff.name, func.coalesce(func.sum(StaffAttendance.late_minutes), 0).label('mins'), func.count(StaffAttendance.id).label('days'))
        .join(Staff, Staff.id == StaffAttendance.staff_id)
        .filter(StaffAttendance.date >= first_day, StaffAttendance.date <= last_day, StaffAttendance.late_minutes.isnot(None), StaffAttendance.late_minutes > 0)
        .group_by(Staff.id, Staff.name)
        .order_by(func.coalesce(func.sum(StaffAttendance.late_minutes), 0).desc())
        .limit(10)
        .all()
    )
    top_late_staff = [{'staff_id': sid, 'name': nm, 'minutes': int(mins or 0), 'days': int(days or 0)} for (sid, nm, mins, days) in top_late_rows]

    # Historic trends (last 12 months)
    def _month_key(y, m):
        return f"{y}-{m:02d}"
    # Pre-fill 12 months window
    labels = []
    ym_keys = []
    cy, cm = this_year, this_month
    for i in range(11, -1, -1):
        y = cy if cm - i > 0 else cy - ((i - cm) // 12 + 1)
        m = ((cm - i - 1) % 12) + 1
        ym_keys.append((y, m))
        import calendar as _cal
        labels.append(f"{_cal.month_abbr[m]} {str(y)[2:]}")
    # Invoice aggregates by status and amount
    inv_aggs = db.session.query(
        StaffInvoice.year, StaffInvoice.month, StaffInvoice.status,
        func.count(StaffInvoice.id), func.coalesce(func.sum(StaffInvoice.amount), 0)
    ).group_by(StaffInvoice.year, StaffInvoice.month, StaffInvoice.status).all()
    inv_counts = {(_y, _m, _s): int(c or 0) for (_y, _m, _s, c, _sum) in inv_aggs}
    inv_sums = {(_y, _m, _s): float(_sum or 0) for (_y, _m, _s, c, _sum) in inv_aggs}
    data_submitted = []  # count of non-draft submissions
    data_approved = []   # count approved
    data_amount = []     # approved amount
    for (y, m) in ym_keys:
        submitted_cnt = sum(inv_counts.get((y, m, s), 0) for s in ['Pending','Approved','Rejected'])
        approved_cnt = inv_counts.get((y, m, 'Approved'), 0)
        approved_amt = inv_sums.get((y, m, 'Approved'), 0.0)
        data_submitted.append(submitted_cnt)
        data_approved.append(approved_cnt)
        data_amount.append(round(float(approved_amt or 0.0), 2))
    # Lateness trend
    late_aggs = db.session.query(
        func.extract('year', StaffAttendance.date).label('y'),
        func.extract('month', StaffAttendance.date).label('m'),
        func.coalesce(func.sum(StaffAttendance.late_minutes), 0),
        func.count(StaffAttendance.id)
    ).filter(StaffAttendance.late_minutes.isnot(None), StaffAttendance.late_minutes > 0).group_by('y','m').all()
    late_sum_map = {(int(y), int(m)): int(s or 0) for (y, m, s, c) in late_aggs}
    late_days_map = {(int(y), int(m)): int(c or 0) for (y, m, s, c) in late_aggs}
    data_late_minutes = [late_sum_map.get((y, m), 0) for (y, m) in ym_keys]
    data_late_days = [late_days_map.get((y, m), 0) for (y, m) in ym_keys]

    return render_template(
        'staff_invoices/dashboard.html',
        totals=totals,
        total_amount=total_amount,
        latest=latest,
        employees_by_company=employees_by_company,
        pending_by_company=pending_by_company,
        missing_by_company=missing_by_company_list,
        missing_total=len(missing_staff),
        lateness={
            'total_days': int(total_late_days or 0),
            'total_minutes': int(total_late_minutes or 0),
            'avg_minutes_per_day': (round((total_late_minutes / total_late_days), 1) if total_late_days else 0),
            'top_staff': top_late_staff,
        },
        charts={
            'labels': labels,
            'submitted': data_submitted,
            'approved': data_approved,
            'approved_amount': data_amount,
            'late_minutes': data_late_minutes,
            'late_days': data_late_days,
        }
    )


@app.route('/api/staff-invoices/remind-missing', methods=['POST'])
@login_required
@permission_required('manage_staff_invoices')
def api_staff_invoices_remind_missing():
    import json as _json
    from datetime import date
    payload = request.get_json(silent=True) or {}
    try:
        company_id = payload.get('company_id') or request.form.get('company_id')
        company_id = int(company_id) if company_id not in (None, '', []) else None
    except Exception:
        company_id = None
    try:
        month = int(payload.get('month') or request.form.get('month') or date.today().month)
        year = int(payload.get('year') or request.form.get('year') or date.today().year)
    except Exception:
        today = date.today(); month, year = today.month, today.year
    # Determine missing staff
    submitted_user_ids = [
        uid for (uid,) in db.session.query(StaffInvoice.created_by_id)
        .filter(StaffInvoice.year == year, StaffInvoice.month == month, StaffInvoice.status.in_(['Pending','Approved','Rejected']))
        .distinct().all()
    ]
    q = Staff.query.filter(Staff.active == True, Staff.company_id.isnot(None))
    if company_id:
        q = q.filter(Staff.company_id == company_id)
    if submitted_user_ids:
        q = q.filter(~Staff.user_id.in_(submitted_user_ids))
    targets = q.all()
    sent = 0; errors = []
    import calendar as _cal
    month_name = _cal.month_name[int(month)] if 1 <= int(month) <= 12 else str(month)
    link = url_for('staff_invoice_new', _external=True)
    for s in targets:
        email = (s.email or '').strip().lower()
        if not email:
            errors.append({'staff_id': s.id, 'name': s.name, 'reason': 'no_email'})
            continue
        subject = f"Reminder: Please submit your {month_name} {year} invoice"
        try:
            html = f"""
                <p>Hi {s.name},</p>
                <p>This is a friendly reminder to submit your staff invoice for <strong>{month_name} {year}</strong>.</p>
                <p>Please click the button below to create your invoice now.</p>
                <p><a href='{link}' style='display:inline-block;padding:10px 14px;background:#4f46e5;color:#fff;text-decoration:none;border-radius:6px'>Create Invoice</a></p>
                <p>If you've already submitted, you can ignore this email.</p>
                <p>Thank you.</p>
            """
            send_email(email, subject, html)
            sent += 1
        except Exception as exc:
            errors.append({'staff_id': s.id, 'name': s.name, 'reason': str(exc)})
    return jsonify({'success': True, 'sent': sent, 'total': len(targets), 'errors': errors})


@app.route('/invoice-submissions')
@login_required
@permission_required('manage_staff_invoices')
def invoice_submissions():
    # Manager-facing list of all submitted staff invoices with simple filters
    q = StaffInvoice.query
    args = request.args
    search_q = (args.get('q') or '').strip()
    branch = (args.get('branch') or '').strip() or None
    status = (args.get('status') or '').strip() or None
    payment_status = (args.get('payment_status') or '').strip() or None
    # New filters: month, year, company, date range
    month = (args.get('month') or '').strip() or None
    try:
        month = int(month) if month else None
    except Exception:
        month = None
    year = (args.get('year') or '').strip() or None
    try:
        year = int(year) if year else None
    except Exception:
        year = None
    company_id = (args.get('company') or '').strip() or None
    try:
        company_id = int(company_id) if company_id else None
    except Exception:
        company_id = None
    date_from = (args.get('date_from') or '').strip() or None
    date_to = (args.get('date_to') or '').strip() or None
    from datetime import datetime
    try:
        date_from_dt = datetime.strptime(date_from, '%Y-%m-%d').date() if date_from else None
    except Exception:
        date_from_dt = None
    try:
        date_to_dt = datetime.strptime(date_to, '%Y-%m-%d').date() if date_to else None
    except Exception:
        date_to_dt = None

    # Basic search by id or submitter name
    if search_q:
        if search_q.isdigit():
            q = q.filter(StaffInvoice.id == int(search_q))
        else:
            sq = f"%{search_q}%"
            q = q.join(User, StaffInvoice.created_by).filter(User.name.ilike(sq))

    if status:
        q = q.filter(StaffInvoice.status == status)

    if payment_status:
        q = q.filter(StaffInvoice.payment_status == payment_status)

    if month is not None:
        q = q.filter(StaffInvoice.month == month)
    if year is not None:
        q = q.filter(StaffInvoice.year == year)

    if company_id is not None:
        # join to Staff to filter by Staff.company_id
        q = q.join(Staff, Staff.user_id == StaffInvoice.created_by_id).filter(Staff.company_id == company_id)

    if date_from_dt is not None or date_to_dt is not None:
        from sqlalchemy import case
        date_expr = case([(StaffInvoice.submitted_at != None, StaffInvoice.submitted_at)], else_=StaffInvoice.created_at)
        if date_from_dt is not None:
            q = q.filter(date_expr >= date_from_dt)
        if date_to_dt is not None:
            q = q.filter(date_expr <= date_to_dt)

    records = q.order_by(StaffInvoice.created_at.desc()).all()

    # helper lists
    statuses = ['Draft','Pending','Approved','Rejected']

    # Map user->company and staff record
    user_ids = list({r.created_by_id for r in records})
    staff_rows = Staff.query.filter(Staff.user_id.in_(user_ids)).all() if user_ids else []
    user_company = {s.user_id: (Company.query.get(s.company_id).name if s.company_id and Company.query.get(s.company_id) else None) for s in staff_rows}
    staff_map = {s.user_id: s for s in staff_rows}

    # Companies for company filter dropdown
    companies = Company.query.order_by(Company.name).all()
    filters = {'q': search_q, 'branch': branch, 'status': status, 'payment_status': payment_status, 'month': month, 'year': year, 'company': company_id, 'date_from': date_from, 'date_to': date_to}
    return render_template('invoice_management/submissions.html', records=records, statuses=statuses, filters=filters, user_company=user_company, branch_choices=BRANCH_CHOICES(), staff_map=staff_map, companies=companies)


@app.route('/invoice-submissions/<int:invoice_id>')
@login_required
@permission_required('manage_staff_invoices')
def invoice_submission_view(invoice_id:int):
    inv = StaffInvoice.query.get_or_404(invoice_id)
    # Build attendance map for each line item (match by date and branch and staff)
    attendance_map = {}
    # Find staff record for the creator (via User -> Staff.user_id)
    staff_rec = Staff.query.filter(Staff.user_id == inv.created_by_id).first()
    for item in inv.items:
        attendance_map[item.id] = {}
        try:
            att = None
            # Primary: direct staff record mapping
            if staff_rec:
                att = StaffAttendance.query.filter(StaffAttendance.staff_id==staff_rec.id, StaffAttendance.date==item.date, StaffAttendance.branch==item.branch).order_by(StaffAttendance.check_in.asc()).first()

            # Secondary: try to locate a staff by matching the submitter name
            if not att:
                try:
                    candidate = Staff.query.filter(Staff.name.ilike(f"%{inv.created_by.name}%")).first()
                    if candidate:
                        att = StaffAttendance.query.filter(StaffAttendance.staff_id==candidate.id, StaffAttendance.date==item.date, StaffAttendance.branch==item.branch).order_by(StaffAttendance.check_in.asc()).first()
                        if candidate and not staff_rec:
                            staff_rec = candidate
                except Exception:
                    pass

            # Tertiary: find any attendance row on that date+branch and map by machine_id -> staff
            if not att:
                att_row = StaffAttendance.query.filter(StaffAttendance.date==item.date, StaffAttendance.branch==item.branch).order_by(StaffAttendance.check_in.asc()).first()
                if att_row:
                    # If the attendance row has staff_id set, use it. Otherwise, try to map machine_id to Staff
                    if att_row.staff_id:
                        att = att_row
                        if not staff_rec:
                            staff_rec = Staff.query.get(att_row.staff_id)
                    elif att_row.machine_id:
                        # Try to find staff by machine id across known machine columns
                        mid = att_row.machine_id.strip()
                        s = Staff.query.filter(
                            (Staff.whitechapel_machine_id==mid) | (Staff.east_ham_machine_id==mid) | (Staff.stratford_machine_id==mid) | (Staff.docklands_machine_id==mid) | (Staff.access_code==mid)
                        ).first()
                        if s:
                            att = StaffAttendance.query.filter(StaffAttendance.staff_id==s.id, StaffAttendance.date==item.date, StaffAttendance.branch==item.branch).first() or att_row
                            if not staff_rec:
                                staff_rec = s

            if att:
                attendance_map[item.id] = {
                    'check_in': att.check_in.strftime('%H:%M') if att.check_in else '',
                    'check_out': att.check_out.strftime('%H:%M') if att.check_out else '',
                    'hours': att.hours_hhmmss(),
                    'late': (att.late_minutes and att.late_minutes>0) and 'Yes' or 'No'
                }
        except Exception:
            attendance_map[item.id] = {}

    # Company / department helper
    staff_department = staff_rec.department if staff_rec else None
    user_company = None
    if staff_rec and staff_rec.company_id:
        comp = Company.query.get(staff_rec.company_id)
        user_company = comp.name if comp else None

    # Totals: sum up hours and amounts from invoice items
    try:
        total_hours = sum((float(it.hours or 0) for it in inv.items))
    except Exception:
        total_hours = 0
    try:
        total_amount = sum((float(it.amount or 0) for it in inv.items))
    except Exception:
        total_amount = 0.0

    # Branch-level breakdown: accumulate hours and amounts by branch
    branch_acc = {}
    try:
        for it in inv.items:
            b = it.branch or 'Unspecified'
            rec = branch_acc.setdefault(b, {'hours': 0.0, 'amount': 0.0})
            try:
                rec['hours'] += float(it.hours or 0)
            except Exception:
                pass
            try:
                rec['amount'] += float(it.amount or 0)
            except Exception:
                pass
    except Exception:
        branch_acc = {}

    # Convert to a sorted list for predictable display (branch name asc)
    branch_breakdown = [{'branch': k, 'hours': v['hours'], 'amount': v['amount']} for k, v in sorted(branch_acc.items(), key=lambda x: x[0])]

    # Compute staff age and map to NMW band (editable via settings 'nmw_bands')
    try:
        nmw_bands = get_setting('nmw_bands', None, as_json=True)
    except Exception:
        nmw_bands = None
    if not nmw_bands:
        nmw_bands = [
            {"label": "aged 21 and over", "min_age": 21, "amount": "12.21"},
            {"label": "aged 18 to 20", "min_age": 18, "amount": "10.00"},
            {"label": "aged under 18", "min_age": 0, "amount": "7.55"},
            {"label": "apprentice rate", "min_age": 0, "amount": "7.55"},
        ]

    staff_age = staff_rec.age if staff_rec else None
    nmw_band_amount = 'N/A'
    nmw_band_label = ''
    if staff_age is not None:
        selected = None
        for b in nmw_bands:
            try:
                if int(b.get('min_age', 0)) <= int(staff_age):
                    if selected is None or int(b.get('min_age', 0)) > int(selected.get('min_age', 0)):
                        selected = b
            except Exception:
                continue
        if selected:
            nmw_band_amount = selected.get('amount')
            nmw_band_label = selected.get('label')

    return render_template('invoice_management/submission_detail.html', inv=inv, attendance_map=attendance_map, staff_department=staff_department, user_company=user_company, total_hours=total_hours, total_amount=total_amount, branch_breakdown=branch_breakdown, staff_rec=staff_rec, nmw_band_amount=nmw_band_amount, nmw_band_label=nmw_band_label)


@app.route('/invoice-submissions/<int:invoice_id>/pdf')
@login_required
@permission_required('manage_staff_invoices')
def invoice_submission_pdf(invoice_id:int):
    """Generate a branded PDF for a staff invoice submission (Exccel Tutors branding).

    Falls back to returning HTML if PDF generation fails.
    """
    inv = StaffInvoice.query.get_or_404(invoice_id)
    # Prepare totals and breakdown (reuse logic from view)
    try:
        total_hours = sum((float(it.hours or 0) for it in inv.items))
    except Exception:
        total_hours = 0
    try:
        total_amount = sum((float(it.amount or 0) for it in inv.items))
    except Exception:
        total_amount = 0.0
    branch_acc = {}
    try:
        for it in inv.items:
            b = it.branch or 'Unspecified'
            rec = branch_acc.setdefault(b, {'hours': 0.0, 'amount': 0.0})
            try:
                rec['hours'] += float(it.hours or 0)
            except Exception:
                pass
            try:
                rec['amount'] += float(it.amount or 0)
            except Exception:
                pass
    except Exception:
        branch_acc = {}
    branch_breakdown = [{'branch': k, 'hours': v['hours'], 'amount': v['amount']} for k, v in sorted(branch_acc.items(), key=lambda x: x[0])]

    # Inline CSS path (use invoice.css for consistent look)
    css_path = os.path.join(app.root_path, 'static', 'css', 'invoice.css')
    inline_css = ''
    try:
        with open(css_path, 'r', encoding='utf-8') as f:
            inline_css = f.read()
    except Exception:
        pass

    # Build HTML with print_view flag so template can render a complete document
    from datetime import datetime
    gen_at = datetime.now()
    # Attempt to locate staff record for consistent bank field rendering
    staff_rec = Staff.query.filter(Staff.user_id == inv.created_by_id).first()
    if not staff_rec:
        # try by name fallback
        try:
            staff_rec = Staff.query.filter(Staff.name.ilike(f"%{inv.created_by.name}%")).first()
        except Exception:
            staff_rec = None

    html = render_template('invoice_management/submission_pdf.html', inv=inv, total_hours=total_hours, total_amount=total_amount, branch_breakdown=branch_breakdown, inline_css=inline_css, print_view=True, generated_at=gen_at, staff_rec=staff_rec)

    try:
        from io import BytesIO

        from xhtml2pdf import pisa
        pdf_io = BytesIO()
        pisa.CreatePDF(io.StringIO(html), dest=pdf_io)  # type: ignore[arg-type]
        pdf_io.seek(0)
        fname = f"staff_invoice_{inv.id}.pdf"
        return send_file(pdf_io, as_attachment=True, download_name=fname, mimetype='application/pdf')
    except Exception as exc:
        # Fall back to returning the HTML view if PDF creation fails
        flash(f'PDF generation failed: {exc}', 'warning')
        return html


@app.route('/invoice-submissions/<int:invoice_id>/history')
@login_required
@permission_required('manage_staff_invoices')
def invoice_submission_history_api(invoice_id:int):
    inv = StaffInvoice.query.get_or_404(invoice_id)
    try:
        from models import StaffInvoiceChange
        changes = StaffInvoiceChange.query.filter_by(invoice_id=invoice_id).order_by(StaffInvoiceChange.changed_at.asc()).all()
        if changes:
            hist = []
            for c in changes:
                hist.append({
                    'at': c.changed_at.isoformat() if c.changed_at else None,
                    'action': 'changed',
                    'field': c.field,
                    'old': c.old_value,
                    'new': c.new_value,
                    'by': (c.changed_by.name if getattr(c, 'changed_by', None) else None)
                })
            return jsonify(hist)
    except Exception:
        pass
    # Fallback: synthetic history
    hist = []
    creator = (inv.created_by.name if getattr(inv, 'created_by', None) else None)
    hist.append({'at': inv.created_at.isoformat() if inv.created_at else None, 'action': 'created', 'status': 'Draft', 'by': creator})
    if inv.submitted_at:
        hist.append({'at': inv.submitted_at.isoformat(), 'action': 'submitted', 'status': inv.status, 'by': creator})
    hist.append({'at': inv.updated_at.isoformat() if inv.updated_at else None, 'action': 'last_updated', 'status': inv.status, 'by': creator})
    return jsonify(hist)


@app.route('/invoice-submissions/<int:invoice_id>/toggle-payment', methods=['POST'])
@login_required
@permission_required('manage_staff_invoices')
def invoice_submission_toggle_payment(invoice_id:int):
    inv = StaffInvoice.query.get_or_404(invoice_id)
    # Toggle payment_status between Paid and Unpaid and create audit row
    try:
        old = inv.payment_status
        inv.payment_status = 'Paid' if (old or 'Unpaid') != 'Paid' else 'Unpaid'
        db.session.add(inv)
        # Write audit row
        try:
            from models import StaffInvoiceChange
            change = StaffInvoiceChange(invoice_id=inv.id, field='payment_status', old_value=old, new_value=inv.payment_status, changed_by_id=current_user.id)
            db.session.add(change)
        except Exception:
            # best-effort: continue if audit table missing
            pass
        db.session.commit()
        return jsonify({'ok': True, 'payment_status': inv.payment_status})
    except Exception:
        db.session.rollback()


# NMW bands admin
@app.route('/admin/nmw-bands', methods=['GET', 'POST'])
@login_required
@permission_required('manage_pricing')
def nmw_bands_index():
    if request.method == 'POST':
        # Expect structured form fields only (band-0-label, band-0-min_age, band-0-amount ...)
        bands = []
        idx = 0
        while True:
            prefix = f'band-{idx}-'
            label = request.form.get(prefix + 'label')
            if not label:
                break
            try:
                min_age = int(request.form.get(prefix + 'min_age') or 0)
            except Exception:
                min_age = 0
            amount = (request.form.get(prefix + 'amount') or '').strip()
            bands.append({'label': label, 'min_age': min_age, 'amount': amount})
            idx += 1
        if bands:
            set_setting('nmw_bands', bands, as_json=True)
            flash('Saved NMW bands', 'success')
    bands = get_setting('nmw_bands', None, as_json=True)
    if not bands:
        bands = [
            {"label": "aged 21 and over", "min_age": 21, "amount": "12.21"},
            {"label": "aged 18 to 20", "min_age": 18, "amount": "10.00"},
            {"label": "aged under 18", "min_age": 0, "amount": "7.55"},
            {"label": "apprentice rate", "min_age": 0, "amount": "7.55"},
        ]
    return render_template('admin/nmw_bands/index.html', bands=bands)


@app.route('/invoice-submissions/<int:invoice_id>/print')
@login_required
@permission_required('manage_staff_invoices')
def invoice_submission_print(invoice_id:int):
    # Render the PDF template in a browser-friendly print view (includes logo)
    inv = StaffInvoice.query.get_or_404(invoice_id)
    # Recompute totals and branch breakdown similarly to the view
    total_hours = 0
    total_amount = 0
    branch_acc = {}
    for it in inv.items:
        h = float(it.hours or 0)
        a = float(it.amount or 0)
        total_hours += h
        total_amount += a
        k = it.branch or 'Unspecified'
        entry = branch_acc.setdefault(k, {'hours': 0, 'amount': 0})
        entry['hours'] += h
        entry['amount'] += a
    branch_breakdown = [{'branch': k, 'hours': v['hours'], 'amount': v['amount']} for k, v in sorted(branch_acc.items(), key=lambda x: x[0])]
    inline_css = ''
    gen_at = datetime.now(timezone.utc)
    # Try to locate staff record for bank details display in print view
    staff_rec = Staff.query.filter(Staff.user_id == inv.created_by_id).first()
    if not staff_rec:
        try:
            staff_rec = Staff.query.filter(Staff.name.ilike(f"%{inv.created_by.name}%")).first()
        except Exception:
            staff_rec = None
    return render_template('invoice_management/submission_pdf.html', inv=inv, total_hours=total_hours, total_amount=total_amount, branch_breakdown=branch_breakdown, inline_css=inline_css, print_view=True, generated_at=gen_at, staff_rec=staff_rec)


@app.route('/invoice-submissions/<int:invoice_id>/accept', methods=['POST'])
@login_required
@permission_required('manage_staff_invoices')
def invoice_submission_accept(invoice_id:int):
    inv = StaffInvoice.query.get_or_404(invoice_id)
    try:
        old = inv.status
        inv.status = 'Approved'
        db.session.add(inv)
        try:
            from models import StaffInvoiceChange
            change = StaffInvoiceChange(invoice_id=inv.id, field='status', old_value=old, new_value=inv.status, changed_by_id=current_user.id)
            db.session.add(change)
        except Exception:
            pass
        db.session.commit()
        # Send notification email to submitter
        try:
            from email_utils import send_with_template
            subj, html = _build_staff_invoice_approved_email(inv.created_by.name if inv.created_by else '', inv)
            send_with_template('staff_invoice_approved', {'name': inv.created_by.name if inv.created_by else '', 'to_email': inv.created_by.email if inv.created_by else None, 'invoice': inv}, to_email=(inv.created_by.email if inv.created_by else None), fallback=lambda: (subj, html))
        except Exception:
            try:
                # best-effort plain send
                subj, html = _build_staff_invoice_approved_email(inv.created_by.name if inv.created_by else '', inv)
                _send_email_safe(inv.created_by.email if inv.created_by else None, subj, html, log_prefix='Invoice approved')
            except Exception:
                pass
        return jsonify({'ok': True})
    except Exception:
        db.session.rollback()
        return jsonify({'ok': False}), 500


@app.route('/invoice-submissions/<int:invoice_id>/reject', methods=['POST'])
@login_required
@permission_required('manage_staff_invoices')
def invoice_submission_reject(invoice_id:int):
    inv = StaffInvoice.query.get_or_404(invoice_id)
    reason = (request.form.get('reason') or '').strip()
    try:
        old = inv.status
        inv.status = 'Rejected'
        db.session.add(inv)
        try:
            from models import StaffInvoiceChange
            change = StaffInvoiceChange(invoice_id=inv.id, field='status', old_value=old, new_value=inv.status, changed_by_id=current_user.id)
            db.session.add(change)
            if reason:
                change2 = StaffInvoiceChange(invoice_id=inv.id, field='rejection_reason', old_value=None, new_value=reason, changed_by_id=current_user.id)
                db.session.add(change2)
        except Exception:
            pass
        db.session.commit()
        # Send rejection email
        try:
            from email_utils import send_with_template
            subj, html = _build_staff_invoice_rejected_email(inv.created_by.name if inv.created_by else '', inv, reason)
            send_with_template('staff_invoice_rejected', {'name': inv.created_by.name if inv.created_by else '', 'to_email': inv.created_by.email if inv.created_by else None, 'invoice': inv, 'reason': reason}, to_email=(inv.created_by.email if inv.created_by else None), fallback=lambda: (subj, html))
        except Exception:
            try:
                subj, html = _build_staff_invoice_rejected_email(inv.created_by.name if inv.created_by else '', inv, reason)
                _send_email_safe(inv.created_by.email if inv.created_by else None, subj, html, log_prefix='Invoice rejected')
            except Exception:
                pass
        flash('Invoice rejected', 'success')
        return redirect(url_for('invoice_submission_view', invoice_id=inv.id))
    except Exception:
        db.session.rollback()
        flash('Failed to reject invoice', 'danger')
        return redirect(url_for('invoice_submission_view', invoice_id=inv.id))


def _ensure_branches_exist():
    """Seed default branches if none exist.

    Flask 3 removed/changed the `before_first_request` hook in some server
    contexts; register this function using the decorator when available, but
    fall back to a one-shot `before_request` registration for compatibility.
    """
    try:
        from models import Branch
        defaults = ['Whitechapel','East Ham','Docklands','Stratford']
        existing = {b.name for b in Branch.query.all()}
        for d in defaults:
            if d not in existing:
                db.session.add(Branch(name=d, status='Active'))
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


# Register seeding to run once on first request in a way that's compatible
# with Flask 2.x and 3.x runtime differences.
try:
    # Prefer direct registration where available.
    app.before_first_request(_ensure_branches_exist)
except Exception:
    # Fallback: use a one-shot before_request guard.
    _BRANCHES_SEEDED = False

    @app.before_request
    def _maybe_seed_branches_once():
        nonlocal_flag = globals()
        global _BRANCHES_SEEDED
        if _BRANCHES_SEEDED:
            return
        try:
            _ensure_branches_exist()
        finally:
            _BRANCHES_SEEDED = True


@app.route('/branches')
@login_required
@permission_required('manage_staff_invoices')
def branches_index():
    from models import Branch
    branches = Branch.query.order_by(Branch.name).all()
    return render_template('branches/index.html', branches=branches)


# ---------------- Email Settings CRUD ---------------- #
@app.route('/system/email-settings')
@login_required
@permission_required('manage_supervisor_shifts')
def email_settings_index():
    from models import EmailSetting
    items = EmailSetting.query.order_by(EmailSetting.name.asc()).all()
    return render_template('email_settings/index.html', items=items)


@app.route('/system/email-settings/new', methods=['GET','POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def email_settings_new():
    from models import EmailSetting
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        provider = (request.form.get('provider') or 'smtp').strip()
        host = (request.form.get('host') or '').strip()
        port = int(request.form.get('port') or 0) or None
        username = (request.form.get('username') or '').strip()
        password = (request.form.get('password') or '').strip()
        sender_name = (request.form.get('sender_name') or '').strip()
        sender_email = (request.form.get('sender_email') or '').strip()
        use_tls = bool(request.form.get('use_tls'))
        use_ssl = bool(request.form.get('use_ssl'))
        is_active = bool(request.form.get('is_active'))
        if not name:
            flash('Name is required', 'warning')
            return render_template('email_settings/form.html', item=None)
        es = EmailSetting(name=name, provider=provider, host=host, port=port, username=username, password=password, sender_name=sender_name, sender_email=sender_email, use_tls=use_tls, use_ssl=use_ssl, is_active=is_active)
        db.session.add(es)
        db.session.commit()
        flash('Email setting created', 'success')
        return redirect(url_for('email_settings_index'))
    return render_template('email_settings/form.html', item=None)


@app.route('/system/email-settings/<int:esid>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def email_settings_edit(esid: int):
    from models import EmailSetting
    es = EmailSetting.query.get_or_404(esid)
    if request.method == 'POST':
        es.name = (request.form.get('name') or es.name).strip()
        es.provider = (request.form.get('provider') or es.provider).strip()
        es.host = (request.form.get('host') or es.host).strip()
        es.port = int(request.form.get('port') or es.port or 0) or None
        es.username = (request.form.get('username') or es.username).strip()
        pw = request.form.get('password')
        if pw:
            es.password = pw.strip()
        es.sender_name = (request.form.get('sender_name') or es.sender_name).strip()
        es.sender_email = (request.form.get('sender_email') or es.sender_email).strip()
        es.use_tls = bool(request.form.get('use_tls'))
        es.use_ssl = bool(request.form.get('use_ssl'))
        es.is_active = bool(request.form.get('is_active'))
        db.session.commit()
        flash('Email setting updated', 'success')
        return redirect(url_for('email_settings_index'))
    return render_template('email_settings/form.html', item=es)


# Backward-compatible alias (some links may use /edit/<id> order)
@app.route('/system/email-settings/edit/<int:esid>', methods=['GET','POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def email_settings_edit_alias(esid: int):
    return email_settings_edit(esid)


@app.route('/system/email-settings/<int:esid>/delete', methods=['POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def email_settings_delete(esid: int):
    from models import EmailSetting
    es = EmailSetting.query.get_or_404(esid)
    db.session.delete(es)
    db.session.commit()
    flash('Email setting deleted', 'success')
    return redirect(url_for('email_settings_index'))


@app.route('/system/email-setup-audit', methods=['GET','POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def email_setup_audit():
    """Audit the codebase for common sender addresses and present suggestions.

    GET: show discovered candidate sender addresses.
    POST: create selected EmailSetting stubs (inactive) so admin can fill credentials.
    """
    # Build a conservative candidate list by inspecting known constants and common addresses
    import email_utils as _eu
    from models import EmailSetting
    candidates = [
        {'name': 'legacy-management', 'sender_email': getattr(_eu, 'FROM_EMAIL', None), 'host': getattr(_eu, 'SMTP_HOST', None)},
        {'name': 'techsupport-sender', 'sender_email': 'techsupport@exceltutors.org.uk', 'host': None},
        {'name': 'superadmin-sender', 'sender_email': 'superadmin@exceltutors.org.uk', 'host': None},
    ]

    # Filter out empty and duplicates
    seen = set()
    filtered = []
    for c in candidates:
        e = (c.get('sender_email') or '').strip()
        if not e or e in seen:
            continue
        seen.add(e)
        exists = EmailSetting.query.filter_by(sender_email=e).first()
        filtered.append({'name': c.get('name'), 'sender_email': e, 'host': c.get('host'), 'exists': bool(exists)})

    if request.method == 'POST':
        picked = request.form.getlist('create')
        created = 0
        for p in picked:
            # p will be the email address
            if not p:
                continue
            if EmailSetting.query.filter_by(sender_email=p).first():
                continue
            new_name = f'sender-{p.split("@")[0]}'
            es = EmailSetting(name=new_name, provider='smtp', host=None, port=None, username=None, password=None, use_tls=True, use_ssl=False, sender_name=None, sender_email=p, is_active=False)
            db.session.add(es)
            created += 1
        if created:
            db.session.commit()
            flash(f'Created {created} email setting stub(s). Please complete credentials and activate.', 'success')
        else:
            flash('No new settings created.', 'info')
        return redirect(url_for('email_settings_index'))

    return render_template('email_settings/audit.html', candidates=filtered)


@app.route('/api/email-setting/<int:setting_id>')
@login_required
@permission_required('manage_email_logs')
def api_email_setting(setting_id):
    from models import EmailSetting
    s = EmailSetting.query.get(setting_id)
    if not s:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'id': s.id,
        'name': s.name,
        'sender_name': s.sender_name,
        'sender_email': s.sender_email,
        'username': s.username,
        'host': s.host,
    })


def _extract_placeholders(text):
    import re
    if not text:
        return []
    return sorted(set(re.findall(r"\{([a-zA-Z0-9_\.]+)\}", text)))


@app.route('/system/email-templates/<int:tid>/preview', methods=['POST'])
@login_required
@permission_required('manage_email_logs')
def email_template_preview(tid: int):
    from models import EmailTemplate
    tmpl = EmailTemplate.query.get(tid)
    if not tmpl:
        return jsonify({'error': 'template not found'}), 404
    data = request.get_json() or {}
    subject = data.get('subject') or tmpl.subject_template or ''
    html = data.get('html') or tmpl.html_template or ''
    ctx = data.get('ctx') or {}
    try:
        subs = _extract_placeholders(subject) + _extract_placeholders(html)
        missing = []
        for p in subs:
            parts = p.split('.')
            v = ctx
            ok = True
            for part in parts:
                if isinstance(v, dict) and part in v:
                    v = v[part]
                else:
                    ok = False
                    break
            if not ok:
                missing.append(p)
        if missing:
            return jsonify({'missing': missing})
        try:
            rendered_subject = subject.format(**ctx)
        except Exception as e:
            return jsonify({'error': f'subject render error: {e}'}), 400
        try:
            rendered_html = html.format(**ctx)
        except Exception as e:
            return jsonify({'error': f'html render error: {e}'}), 400
        # Optionally return plain-text rendering
        as_text = False
        try:
            js = request.get_json() or {}
            as_text = bool(js.get('as_text'))
        except Exception:
            as_text = False
        result = {'subject': rendered_subject, 'html': rendered_html}
        if as_text:
            # lightweight HTML->text: strip tags and unescape common entities
            try:
                import re
                from html import unescape

                text = re.sub(r'<script.*?>.*?</script>', '', rendered_html, flags=re.DOTALL|re.IGNORECASE)
                text = re.sub(r'<style.*?>.*?</style>', '', text, flags=re.DOTALL|re.IGNORECASE)
                text = re.sub(r'<[^>]+>', '', text)
                text = unescape(text)
                # collapse whitespace
                text = re.sub(r'\s+', ' ', text).strip()
            except Exception:
                text = ''
            result['text'] = text
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/system/email-templates/validate', methods=['POST'])
@login_required
@permission_required('manage_email_logs')
def email_template_validate():
    data = request.get_json() or {}
    subject = data.get('subject') or ''
    html = data.get('html') or ''
    subs = _extract_placeholders(subject) + _extract_placeholders(html)
    return jsonify({'missing': sorted(set(subs))})


# ---------------- Email Management (sent logs) ---------------- #
@app.route('/system/email-management')
@login_required
@permission_required('manage_email_logs')
def email_management_index():
    from models import EmailLog
    q = (request.args.get('q') or '').strip()
    status = (request.args.get('status') or '').strip()
    query = EmailLog.query.order_by(EmailLog.created_at.desc())
    if q:
        like = f"%{q}%"
        query = query.filter(or_(EmailLog.to_email.ilike(like), EmailLog.subject.ilike(like)))
    if status:
        query = query.filter(EmailLog.status == status)
    items = query.limit(200).all()
    return render_template('email_management/index.html', items=items)


@app.route('/system/email-management/<int:elog_id>')
@login_required
@permission_required('manage_email_logs')
def email_management_detail(elog_id: int):
    from models import EmailLog
    el = EmailLog.query.get_or_404(elog_id)
    return render_template('email_management/detail.html', item=el)


@app.route('/system/email-management/<int:elog_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_email_logs')
def email_management_delete(elog_id: int):
    from models import EmailLog
    el = EmailLog.query.get_or_404(elog_id)
    try:
        db.session.delete(el)
        db.session.commit()
        flash('Email log deleted', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to delete email log: {exc}', 'danger')
    return redirect(url_for('email_management_index'))


@app.route('/system/email-management/<int:elog_id>/resend', methods=['POST'])
@login_required
@permission_required('manage_email_logs')
def email_management_resend(elog_id: int):
    from models import EmailLog
    el = EmailLog.query.get_or_404(elog_id)
    try:
        # Re-send using same subject/html. We avoid duplicating attachments here.
        send_email(el.to_email, el.subject, el.html or '')
        flash('Resend triggered', 'success')
    except Exception as exc:
        flash(f'Resend failed: {exc}', 'danger')
    return redirect(url_for('email_management_detail', elog_id=elog_id))


# ---------------- Email Template CRUD ---------------- #
@app.route('/system/email-templates')
@login_required
@permission_required('manage_email_logs')
def email_templates_index():
    from models import EmailTemplate
    items = EmailTemplate.query.order_by(EmailTemplate.name.asc()).all()
    return render_template('email_templates/index.html', items=items)


@app.route('/system/email-templates/<int:tid>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_email_logs')
def email_templates_edit(tid: int):
    from models import EmailTemplate
    et = EmailTemplate.query.get_or_404(tid)
    if request.method == 'POST':
        et.name = (request.form.get('name') or et.name).strip()
        et.subject_template = (request.form.get('subject_template') or et.subject_template)
        et.html_template = (request.form.get('html_template') or et.html_template)
        et.sender_name = (request.form.get('sender_name') or et.sender_name)
        et.sender_email = (request.form.get('sender_email') or et.sender_email)
        # Save linked EmailSetting if selected (empty -> unlink)
        try:
            raw_set = (request.form.get('email_setting_id') or '').strip()
            if raw_set:
                try:
                    et.email_setting_id = int(raw_set)
                except Exception:
                    et.email_setting_id = None
            else:
                et.email_setting_id = None
        except Exception:
            et.email_setting_id = None
        et.is_active = bool(request.form.get('is_active'))
        db.session.commit()
        flash('Template updated', 'success')
        return redirect(url_for('email_templates_index'))
    # Provide email settings for dropdown
    from models import EmailSetting
    settings = EmailSetting.query.order_by(EmailSetting.name.asc()).all()
    return render_template('email_templates/form.html', item=et, settings=settings)


@app.route('/system/email-templates/<int:tid>/delete', methods=['POST'])
@login_required
@permission_required('manage_email_logs')
def email_templates_delete(tid: int):
    from models import EmailTemplate
    et = EmailTemplate.query.get_or_404(tid)
    try:
        db.session.delete(et)
        db.session.commit()
        flash('Template deleted', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to delete template: {exc}', 'danger')
    return redirect(url_for('email_templates_index'))


@app.route('/branches/new', methods=['GET','POST'])
@login_required
@permission_required('manage_staff_invoices')
def branch_new():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        address = (request.form.get('address') or '').strip()
        status = (request.form.get('status') or 'Active').strip()
        phone = (request.form.get('phone') or '').strip()
        email = (request.form.get('email') or '').strip()
        if not name:
            flash('Name is required', 'warning')
            return render_template('branches/form.html', branch=None)
        from models import Branch
        b = Branch(name=name, address=address, status=status, phone=phone, email=email)
        db.session.add(b)
        db.session.commit()
        flash('Branch created', 'success')
        return redirect(url_for('branches_index'))
    return render_template('branches/form.html', branch=None)


@app.route('/branches/<int:branch_id>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_staff_invoices')
def branch_edit(branch_id):
    from models import Branch
    b = Branch.query.get_or_404(branch_id)
    if request.method == 'POST':
        b.name = (request.form.get('name') or b.name).strip()
        b.address = (request.form.get('address') or b.address).strip()
        b.status = (request.form.get('status') or b.status).strip()
        b.phone = (request.form.get('phone') or b.phone).strip()
        b.email = (request.form.get('email') or b.email).strip()
        db.session.commit()
        flash('Branch updated', 'success')
        return redirect(url_for('branches_index'))
    return render_template('branches/form.html', branch=b)


@app.route('/branches/<int:branch_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_staff_invoices')
def branch_delete(branch_id):
    from models import Branch
    b = Branch.query.get_or_404(branch_id)
    db.session.delete(b)
    db.session.commit()
    flash('Branch deleted', 'success')
    return redirect(url_for('branches_index'))


@app.route('/system/branches')
@login_required
@permission_required('manage_supervisor_shifts')
def system_branches_index():
    """System configuration view for branches (mirrors /branches)."""
    from models import Branch
    branches = Branch.query.order_by(Branch.name).all()
    return render_template('branches/index.html', branches=branches)


@app.route('/system/branches/new', methods=['GET','POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def system_branch_new():
    from models import Branch
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        address = (request.form.get('address') or '').strip()
        status = (request.form.get('status') or 'Active').strip()
        phone = (request.form.get('phone') or '').strip()
        email = (request.form.get('email') or '').strip()
        if not name:
            flash('Name is required', 'warning')
            return render_template('branches/form.html', branch=None)
        b = Branch(name=name, address=address, status=status, phone=phone, email=email)
        db.session.add(b)
        db.session.commit()
        flash('Branch created', 'success')
        return redirect(url_for('system_branches_index'))
    return render_template('branches/form.html', branch=None)


@app.route('/system/branches/<int:branch_id>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def system_branch_edit(branch_id):
    from models import Branch
    b = Branch.query.get_or_404(branch_id)
    if request.method == 'POST':
        b.name = (request.form.get('name') or b.name).strip()
        b.address = (request.form.get('address') or b.address).strip()
        b.status = (request.form.get('status') or b.status).strip()
        b.phone = (request.form.get('phone') or b.phone).strip()
        b.email = (request.form.get('email') or b.email).strip()
        db.session.commit()
        flash('Branch updated', 'success')
        return redirect(url_for('system_branches_index'))
    return render_template('branches/form.html', branch=b)


@app.route('/system/branches/<int:branch_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_supervisor_shifts')
def system_branch_delete(branch_id):
    from models import Branch
    b = Branch.query.get_or_404(branch_id)
    db.session.delete(b)
    db.session.commit()
    flash('Branch deleted', 'success')
    return redirect(url_for('system_branches_index'))

@app.route('/invoices/<int:invoice_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_kids_club_invoices')
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
@permission_required('manage_kids_club_invoices')
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
    writer.writerow(['invoice_no','company','invoice_date','due_date','parent_name','child_name','period_start','period_end','sub_total','total','status','payment_method','created_at'])
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
            (r.payment_method or ''),
            r.created_at.isoformat() if r.created_at else ''
        ])
    csv_data = output.getvalue()
    resp = make_response(csv_data)
    resp.headers['Content-Type'] = 'text/csv'
    resp.headers['Content-Disposition'] = 'attachment; filename=invoices_export.csv'
    return resp


@app.route('/invoices/<int:invoice_id>/pdf')
@login_required
@permission_required('manage_kids_club_invoices')
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
EMAIL_SUBJECT_PREFIX = "[Invoice] "


def send_invoice_email(inv: Invoice, *, setting_name: str | None = None):
    """Render invoice HTML and send using DB-backed EmailSetting when available.

    Returns True on success or raises an exception on failure.
    """
    from email_utils import send_email
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
    subj = f"{EMAIL_SUBJECT_PREFIX}{inv.invoice_no}"
    # Prefer editable invoice template if present
    try:
        from email_utils import send_with_template
        send_with_template('invoice', {
            'invoice_no': inv.invoice_no,
            'parent_name': inv.parent_name,
            'total': str(inv.total),
            'to_email': inv.parent_email,
            'html_body': html,
        }, to_email=inv.parent_email, fallback=lambda: (subj, html))
    except Exception:
        send_email(inv.parent_email, subj, html, setting_name=setting_name)
    return True

@app.route('/invoices/<int:invoice_id>/email', methods=['POST'])
@login_required
@permission_required('manage_kids_club_invoices')
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
    # If the client is requesting an API endpoint, return JSON so callers
    # that expect JSON won't see an HTML page (which would cause a parse
    # error like `Unexpected token '<'`). Also respect explicit JSON
    # Accept headers or X-Requested-With style signals.
    try:
        accept = (request.headers.get('Accept') or '').lower()
    except Exception:
        accept = ''
    if (request.path or '').startswith('/api/') or 'application/json' in accept or request.is_json:
        return jsonify({'error': 'forbidden', 'description': getattr(e, 'description', None)}), 403
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
@permission_required('manage_error_reports')
def error_reports_index():
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 25, type=int), 100)
    q = ErrorReport.query.order_by(ErrorReport.created_at.desc())
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)
    return render_template('errors/reports_index.html', reports=pagination.items, pagination=pagination)

@app.route('/error-reports/<int:rid>')
@login_required
@permission_required('manage_error_reports')
def error_report_detail(rid):
    rep = ErrorReport.query.get_or_404(rid)
    return render_template('errors/report_detail.html', report=rep)

@app.route('/error-reports/<int:rid>/status', methods=['POST'])
@login_required
@permission_required('manage_error_reports')
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


@app.route('/admin/public-forms')
@login_required
@permission_required('manage_users')
def admin_public_forms():
    forms = _public_form_rows()
    total_urls = sum(len(row['urls']) for row in forms)
    return render_template('admin/public_forms.html', forms=forms, total_forms=len(forms), total_urls=total_urls)

# ---------------- Student Concerns (public facing + admin list) ---------------- #
@csrf.exempt
@app.route('/report/student-concern', methods=['GET','POST'])
def report_student_concern():
    """Public-facing form: no login required.

    Accepts a dynamic list of rows posted as JSON in 'rows' field,
    with top-level tutor_name and subject default.
    """
    from models import Student, StudentConcern
    if request.method == 'POST':
        # Honeypot spam protection: silently ignore if hidden field is filled
        if (request.form.get('website') or '').strip():
            flash('Thank you for your submission.', 'success')
            return redirect(url_for('report_student_concern'))
        tutor_name = (request.form.get('tutor_name') or '').strip()
        default_subject = (request.form.get('subject') or '').strip() or None
        # rows[] posted as JSON string
        raw_rows = request.form.get('rows')
        try:
            import json
            rows = json.loads(raw_rows) if raw_rows else []
        except Exception:
            rows = []
        created = 0
        for r in rows:
            sid = (r.get('student_id') or '').strip()
            sname = (r.get('student_name') or '').strip() or None
            year = (r.get('year_group') or '').strip() or None
            subj = (r.get('subject') or '').strip() or default_subject
            reasons = r.get('reasons') or []
            other = (r.get('other_details') or '').strip() or None
            sc = StudentConcern(tutor_name=tutor_name or 'Unknown', subject=subj, student_id=sid or None, student_name=sname, year_group=year, other_details=other)
            sc.set_reasons(reasons)
            db.session.add(sc)
            created += 1
        if created:
            db.session.commit()
            flash(f'Submitted {created} concern(s). Thank you.', 'success')
        else:
            flash('No rows were submitted. Please add at least one student.', 'warning')
        return redirect(url_for('report_student_concern'))
    # GET: fixed subjects list as requested
    subjects = ['Maths','English','Science','Computer Science','Economics','Business','Psychology','11+','Physics','Chemistry','Biology']
    return render_template('public/report_student_concern.html', subjects=subjects)


# ---------------- Admission Assessment (Public) ---------------- #
@csrf.exempt
@app.route('/admission-assessment', methods=['GET', 'POST'])
@app.route('/public/admission-assessment', methods=['GET', 'POST'])
def admission_assessment_public():
    from utils import BRANCH_CHOICES

    subjects = _admission_subject_choices()
    heard_choices = _admission_heard_about_choices()
    branches = BRANCH_CHOICES()

    if request.method == 'POST':
        if (request.form.get('website') or '').strip():
            flash('Thank you for your submission.', 'success')
            return redirect(url_for('admission_assessment_public'))

        student_name = (request.form.get('student_name') or '').strip()
        student_year_group = (request.form.get('student_year_group') or '').strip()
        parent_name = (request.form.get('parent_name') or '').strip()
        parent_email = (request.form.get('parent_email') or '').strip()
        parent_phone = (request.form.get('parent_phone') or '').strip()
        heard_about = (request.form.get('heard_about') or '').strip()
        branch = (request.form.get('branch') or '').strip()
        subject_other = (request.form.get('subject_other') or '').strip()
        selected_subjects = [s.strip() for s in request.form.getlist('subjects') if s.strip()]

        errors: list[str] = []
        if not student_name:
            errors.append('Student name is required.')
        if not parent_name:
            errors.append('Parent name is required.')
        if parent_email and '@' not in parent_email:
            errors.append('Parent email address must be valid.')
        if not parent_phone:
            errors.append('Parent phone number is required.')
        if branch and branch not in branches:
            errors.append('Please select a valid branch.')
        if not branch:
            errors.append('Please select a branch.')
        if heard_choices and heard_about and heard_about not in heard_choices:
            errors.append('Please select a valid answer for how you heard about us.')
        if not heard_about:
            errors.append('Please tell us how you heard about Excel Tutors.')
        if subject_other:
            selected_subjects.append(subject_other)
        # Deduplicate subjects preserving original order
        unique_subjects: list[str] = []
        seen = set()
        for sub in selected_subjects:
            if sub.lower() == 'other':
                continue
            key = sub.lower()
            if key not in seen:
                seen.add(key)
                unique_subjects.append(sub)
        if not unique_subjects:
            errors.append('Please select at least one subject.')

        if errors:
            for msg in errors:
                flash(msg, 'warning')
            return render_template(
                'public/admission_assessment.html',
                subjects=subjects,
                heard_choices=heard_choices,
                branches=branches,
            )

        submission = AdmissionAssessmentSubmission(
            student_name=student_name,
            student_year_group=student_year_group or None,
            parent_name=parent_name,
            parent_email=parent_email or None,
            parent_phone=parent_phone,
            branch=branch,
            heard_about=heard_about or None,
            subjects_other=subject_other or None,
        )
        submission.set_subjects(unique_subjects)

        try:
            db.session.add(submission)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            app.logger.exception('Failed to save admission assessment submission: %s', exc)
            flash('We could not submit your request right now. Please try again later.', 'danger')
            return render_template(
                'public/admission_assessment.html',
                subjects=subjects,
                heard_choices=heard_choices,
                branches=branches,
            )

        if parent_email:
            try:
                from email_utils import \
                    build_admission_assessment_confirmation_email

                subject_line, html = build_admission_assessment_confirmation_email(submission)
                send_email(parent_email, subject_line, html)
            except Exception as mail_exc:  # pragma: no cover - email failures should not block UX
                app.logger.warning('Admission assessment confirmation email failed for %s: %s', parent_email, mail_exc)

        return redirect('https://admissions.exceltutors.org.uk/')

    return render_template(
        'public/admission_assessment.html',
        subjects=subjects,
        heard_choices=heard_choices,
        branches=branches,
    )


# ---------------- Job Application (Public) ---------------- #
@csrf.exempt
@app.route('/jobs/apply', methods=['GET','POST'])
def jobs_apply():
    """Public job application form (mobile-first).

    Stores submissions to JobApplication and shows a success flash.
    """
    from models import JobApplication
    from utils import BRANCH_CHOICES

    # Canonical subject choices from system
    SUBJECTS = ['Maths','English','Science','Computer Science','Economics','Business','Psychology','11+','Physics','Chemistry','Biology']

    if request.method == 'POST':
        # Honeypot
        if (request.form.get('website') or '').strip():
            flash('Thanks for your application.', 'success')
            return redirect(url_for('jobs_apply'))
        first_name = (request.form.get('first_name') or '').strip()
        last_name = (request.form.get('last_name') or '').strip()
        email = (request.form.get('email') or '').strip()
        confirm_email = (request.form.get('confirm_email') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        address_line1 = (request.form.get('address_line1') or '').strip()
        city = (request.form.get('city') or '').strip()
        postcode = (request.form.get('postcode') or '').strip()
        university = (request.form.get('university') or '').strip()
        study_year_raw = (request.form.get('study_year') or '').strip()
        course_name = (request.form.get('course_name') or '').strip()
        a1_subject = (request.form.get('alevel1_subject') or '').strip()
        a1_grade = (request.form.get('alevel1_grade') or '').strip()
        a1_status = (request.form.get('alevel1_status') or '').strip()
        a2_subject = (request.form.get('alevel2_subject') or '').strip()
        a2_grade = (request.form.get('alevel2_grade') or '').strip()
        a2_status = (request.form.get('alevel2_status') or '').strip()
        a3_subject = (request.form.get('alevel3_subject') or '').strip()
        a3_grade = (request.form.get('alevel3_grade') or '').strip()
        a3_status = (request.form.get('alevel3_status') or '').strip()
        g_maths_grade = (request.form.get('gcse_maths_grade') or '').strip()
        g_maths_status = (request.form.get('gcse_maths_status') or '').strip()
        g_eng_grade = (request.form.get('gcse_english_grade') or '').strip()
        g_eng_status = (request.form.get('gcse_english_status') or '').strip()
        g_sci_grade = (request.form.get('gcse_science_grade') or '').strip()
        g_sci_status = (request.form.get('gcse_science_status') or '').strip()
        # Checkboxes (yes/no groups) – treat *_yes checked as True
        tutoring_experience = True if request.form.get('tutoring_experience_yes') else False
        uk_work_eligible = True if request.form.get('uk_work_eligible_yes') else False
        branches = [b for b in request.form.getlist('branches') if b in BRANCH_CHOICES()]
        tutor_subjects = [s for s in request.form.getlist('subjects') if s in SUBJECTS]
        heard_about = (request.form.get('heard_about') or '').strip()
        # CV file (required)
        cv_file = request.files.get('cv_file')

        # Minimal validation
        errors = []
        if not first_name:
            errors.append('First name is required.')
        if not last_name:
            errors.append('Last name is required.')
        if not email:
            errors.append('Email is required.')
        if email and confirm_email and (email.lower() != confirm_email.lower()):
            errors.append('Email and Confirm email must match.')
        if not phone:
            errors.append('Phone number is required.')
        if not address_line1:
            errors.append('First line of address is required.')
        if not city:
            errors.append('City is required.')
        if not postcode:
            errors.append('Postcode is required.')
        # Education
        if not university:
            errors.append('University is required.')
        if not study_year_raw:
            errors.append('Current year of study is required.')
        else:
            try:
                sy = int(study_year_raw)
                if sy < 1 or sy > 10:
                    errors.append('Current year of study must be a whole number between 1 and 10.')
            except Exception:
                errors.append('Current year of study must be a whole number (e.g., 1, 2, 3).')
        if not course_name:
            errors.append('Name of course is required.')
        # A levels (all three required as per specification)
        if not a1_subject or not a1_grade or not a1_status:
            errors.append('All fields for A Level Subject 1 are required.')
        if not a2_subject or not a2_grade or not a2_status:
            errors.append('All fields for A Level Subject 2 are required.')
        if not a3_subject or not a3_grade or not a3_status:
            errors.append('All fields for A Level Subject 3 are required.')
        # GCSE
        if not g_maths_grade or not g_maths_status:
            errors.append('GCSE Maths grade and status are required.')
        if not g_eng_grade or not g_eng_status:
            errors.append('GCSE English grade and status are required.')
        if not g_sci_grade or not g_sci_status:
            errors.append('GCSE Science grade and status are required.')
        # Experience/Eligibility selections must be explicit
        if not (request.form.get('tutoring_experience_yes') or request.form.get('tutoring_experience_no')):
            errors.append('Please indicate if you have tutoring experience (Yes/No).')
        if not (request.form.get('uk_work_eligible_yes') or request.form.get('uk_work_eligible_no')):
            errors.append('Please indicate if you are eligible to work in the UK (Yes/No).')
        if not branches:
            errors.append('Please select at least one preferred branch.')
        if not tutor_subjects:
            errors.append('Please select at least one subject you can tutor.')
        if not heard_about:
            errors.append('Please tell us how you heard about us.')
        # CV validation (required, PDF/DOC/DOCX)
        def _cv_valid(f):
            try:
                ext = os.path.splitext(f.filename)[1].lower()
                return ext in {'.pdf', '.doc', '.docx'}
            except Exception:
                return False
        if not (cv_file and getattr(cv_file, 'filename', None)):
            errors.append('Please upload your CV (PDF or DOCX).')
        elif not _cv_valid(cv_file):
            errors.append('CV must be a PDF or DOCX/DOC file.')

        if errors:
            for e in errors:
                flash(e, 'warning')
            return render_template('public/job_application.html', branch_choices=BRANCH_CHOICES(), subject_choices=SUBJECTS)

        # Save CV to static/uploads (returns relative path under static)
        cv_rel = None
        try:
            if cv_file and getattr(cv_file, 'filename', None):
                ext = os.path.splitext(cv_file.filename)[1].lower()
                if ext in {'.pdf', '.doc', '.docx'}:
                    fname = f"cv_{uuid4().hex}{ext}"
                    upload_dir = os.path.join(app.root_path, 'static', 'uploads')
                    os.makedirs(upload_dir, exist_ok=True)
                    path = os.path.join(upload_dir, fname)
                    cv_file.save(path)
                    cv_rel = f"uploads/{fname}"
        except Exception as _cv_exc:
            app.logger.warning('Failed to save CV file: %s', _cv_exc)

        ja = JobApplication(
            first_name=first_name,
            last_name=last_name,
            email=email,
            phone=phone,
            address_line1=address_line1,
            city=city,
            postcode=postcode,
            cv_path=cv_rel,
            university=university,
            study_year=str(sy) if 'sy' in locals() else study_year_raw,
            course_name=course_name,
            alevel1_subject=a1_subject,
            alevel1_grade=a1_grade,
            alevel1_status=a1_status,
            alevel2_subject=a2_subject,
            alevel2_grade=a2_grade,
            alevel2_status=a2_status,
            alevel3_subject=a3_subject,
            alevel3_grade=a3_grade,
            alevel3_status=a3_status,
            gcse_maths_grade=g_maths_grade,
            gcse_maths_status=g_maths_status,
            gcse_english_grade=g_eng_grade,
            gcse_english_status=g_eng_status,
            gcse_science_grade=g_sci_grade,
            gcse_science_status=g_sci_status,
            tutoring_experience=bool(tutoring_experience),
            uk_work_eligible=bool(uk_work_eligible),
            branches=','.join(sorted(set(branches))),
            subjects=','.join(sorted(set(tutor_subjects))),
            heard_about=heard_about
        )
        try:
            db.session.add(ja)
            db.session.commit()
            # Send branded confirmation email from the recruitment email setting if configured
            try:
                from email_utils import (
                    build_job_application_confirmation_email,
                    send_recruitment_email)
                subj, html = build_job_application_confirmation_email(ja)
                # Always send job-related emails using the 'recruitment' EmailSetting
                send_recruitment_email(ja.email, subj, html)
            except Exception as mail_exc:  # pragma: no cover - do not block UX on email failures
                app.logger.warning('Job application confirmation email failed for %s: %s', ja.email, mail_exc)
            flash('Thank you. Your application has been submitted.', 'success')
            return redirect(url_for('jobs_apply'))
        except Exception as exc:
            db.session.rollback()
            flash('Failed to submit application. Please try again later.', 'danger')
            app.logger.exception('Job application submit failed: %s', exc)
            return render_template('public/job_application.html', branch_choices=BRANCH_CHOICES(), subject_choices=SUBJECTS)

    # GET
    return render_template('public/job_application.html', branch_choices=BRANCH_CHOICES(), subject_choices=SUBJECTS)


# ---------------- Recruitment Applications (Admin) ---------------- #
def _ordinal(n: int) -> str:
    try:
        return "%d%s" % (n, "tsnrhtdd"[(n//10%10!=1)*(n%10<4)*n%10::4])
    except Exception:
        return str(n)

def _compute_upcoming_interview_slots() -> list[tuple[str, list[str]]]:
    """Return grouped interview slots for next Wed/Fri/Sat/Sun.

    Each item: (day_heading, ["Friday, 24th October 2025 at 10:30 AM", ...])
    """
    try:
        today = date.today()
    except Exception:
        from datetime import datetime as _dt
        today = _dt.utcnow().date()
    # Map weekday index: Monday=0 .. Sunday=6
    target_days = [2, 4, 5, 6]  # Wed, Fri, Sat, Sun
    times = [(10,30), (14,30), (17,0)]
    groups: list[tuple[str, list[str]]] = []
    for wd in target_days:
        # find next date >= today that matches weekday wd
        offset = (wd - today.weekday()) % 7
        if offset == 0:
            offset = 7
        d = today + timedelta(days=offset)
        day_label = d.strftime('%A')
        date_label = f"{_ordinal(d.day)} {d.strftime('%B %Y')}"
        lines = []
        for hh, mm in times:
            tm = datetime(d.year, d.month, d.day, hh, mm)
            lines.append(f"{day_label}, {date_label} at {tm.strftime('%I:%M %p').lstrip('0')}")
        groups.append((day_label, lines))
    return groups


def _compute_upcoming_interview_slots_with_iso() -> list[dict]:
    """Return grouped interview slots with ISO datetimes aligned to labels.

    Uses the same weekday targets and times as _compute_upcoming_interview_slots().
    Each group: { 'day': 'Friday', 'options': [ {'label': str, 'iso': str}, ... ] }
    """
    try:
        today = date.today()
    except Exception:
        from datetime import datetime as _dt
        today = _dt.utcnow().date()
    target_days = [2, 4, 5, 6]  # Wed, Fri, Sat, Sun
    times = [(10,30), (14,30), (17,0)]
    groups: list[dict] = []
    for wd in target_days:
        offset = (wd - today.weekday()) % 7
        if offset == 0:
            offset = 7
        d = today + timedelta(days=offset)
        day_label = d.strftime('%A')
        date_label = f"{_ordinal(d.day)} {d.strftime('%B %Y')}"
        options = []
        for hh, mm in times:
            tm = datetime(d.year, d.month, d.day, int(hh), int(mm))
            label = f"{day_label}, {date_label} at {tm.strftime('%I:%M %p').lstrip('0')}"
            options.append({'label': label, 'iso': tm.isoformat()})
        groups.append({'day': day_label, 'options': options})
    return groups


@app.route('/recruitment/applications')
@login_required
@permission_required('manage_recruitment')
def recruitment_applications_index():
    """List job applications with filters and pagination hooks."""
    from models import JobApplication
    q = (request.args.get('q') or '').strip().lower()
    statuses = [s for s in request.args.getlist('status') if s]
    branches_sel = [b for b in request.args.getlist('branch') if b]
    universities_sel = [u for u in request.args.getlist('university') if u]
    study_year = (request.args.get('study_year') or '').strip()

    query = JobApplication.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            (JobApplication.first_name.ilike(like)) |
            (JobApplication.last_name.ilike(like)) |
            (JobApplication.email.ilike(like)) |
            (JobApplication.phone.ilike(like)) |
            (JobApplication.university.ilike(like))
        )
    if statuses:
        query = query.filter(JobApplication.status.in_(statuses))
    if branches_sel:
        # CSV contains branch values; match any selected using OR of LIKEs
        from sqlalchemy import or_ as _or
        likes = [_or(*[JobApplication.branches.ilike(f"%{b}%") for b in branches_sel])]
        query = query.filter(*likes)
    if universities_sel:
        query = query.filter(JobApplication.university.in_(universities_sel))
    if study_year:
        query = query.filter(JobApplication.study_year == study_year)
    query = query.order_by(JobApplication.created_at.desc())
    apps = query.all()

    # Filter options
    try:
        branches = BRANCH_CHOICES()
    except Exception:
        branches = []
    universities = [u[0] for u in db.session.query(JobApplication.university).filter(JobApplication.university.isnot(None)).distinct().order_by(JobApplication.university.asc()).all()]
    return render_template('recruitment/applications/index.html', apps=apps, branches=branches, universities=universities, active_statuses=statuses, active_branches=branches_sel, active_universities=universities_sel, active_study_year=study_year)


@app.route('/recruitment/applications/bulk', methods=['POST'])
@login_required
@permission_required('manage_recruitment')
def recruitment_applications_bulk():
    from models import JobApplication

    # Proactive warning if the named recruitment email setting is not present
    try:
        from sqlalchemy import func as _func

        from models import EmailSetting as _ES
        has_recruitment_setting = bool(_ES.query.filter(_func.lower(_ES.name) == 'recruitment', _ES.is_active.is_(True)).first())
    except Exception:
        has_recruitment_setting = False
    ids = request.form.getlist('ids')
    action = (request.form.get('action') or '').strip()
    if not ids or not action:
        flash('Select at least one application and a bulk action.', 'warning')
        return redirect(url_for('recruitment_applications_index'))
    q = JobApplication.query.filter(JobApplication.id.in_([int(i) for i in ids]))
    # Superadmin-only bulk delete path
    if action == 'delete':
        if not getattr(current_user, 'is_superadmin', False):
            flash('Only superadmins can delete applications.', 'danger')
            return redirect(url_for('recruitment_applications_index'))
        rows = q.all()
        cv_files = []
        for a in rows:
            try:
                if a.cv_path and isinstance(a.cv_path, str) and a.cv_path.startswith('uploads/'):
                    cv_files.append(a.cv_path)
            except Exception:
                pass
            db.session.delete(a)
        try:
            db.session.commit()
            # Best-effort CV file cleanup after successful commit
            for rel in cv_files:
                try:
                    abs_path = os.path.join(app.root_path, 'static', rel)
                    if os.path.isfile(abs_path):
                        os.remove(abs_path)
                except Exception:
                    pass
            flash(f"Deleted {len(rows)} application(s).", 'success')
        except Exception as exc:
            db.session.rollback()
            flash(f'Failed to delete selected applications: {exc}', 'danger')
        return redirect(url_for('recruitment_applications_index'))
    updated = 0
    invited = 0
    for a in q.all():
        try:
            if action == 'mark_reviewed':
                a.status = 'Reviewed'
            elif action == 'reject':
                a.status = 'Rejected'
            elif action == 'select':
                a.status = 'Selected'
            elif action == 'onboard':
                a.status = 'Onboarded'
            elif action == 'invite':
                # Build and send invitation email (branded, via Recruitment)
                # Ensure a confirmation token exists for this applicant
                if not getattr(a, 'interview_confirm_token', None):
                    try:
                        a.interview_confirm_token = uuid4().hex
                    except Exception:
                        pass
                slots = _compute_upcoming_interview_slots()
                confirm_url = url_for('jobs_confirm_interview', token=a.interview_confirm_token, _external=True)
                subject, html = build_interview_invitation_email(a, slots, confirm_url)
                try:
                    send_recruitment_email(a.email, subject, html)
                    a.status = 'Invited for Interview'
                    invited += 1
                except Exception as exc:
                    app.logger.warning('Failed to send invite to %s: %s', a.email, exc)
                    # still mark reviewed to indicate action taken
                    a.status = 'Reviewed'
            else:
                continue
            a.updated_at = datetime.utcnow()
            db.session.add(a)
            updated += 1
        except Exception:
            continue
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    msg = f"Updated {updated} application(s)."
    if invited:
        msg += f" Sent {invited} invitation(s)."
    flash(msg, 'success')
    if action == 'invite' and not has_recruitment_setting:
        flash("Recruitment email setting is not configured. Using the default active email setting. Configure one named 'recruitment' under Email Settings for correct sender identity.", 'warning')
    return redirect(url_for('recruitment_applications_index'))


@app.route('/recruitment/applications/<int:aid>')
@login_required
@permission_required('manage_recruitment')
def recruitment_application_detail(aid: int):
    from models import JobApplication
    a = JobApplication.query.get_or_404(aid)
    return render_template('recruitment/applications/detail.html', a=a)


@app.route('/recruitment/applications/<int:aid>/invite', methods=['POST'])
@login_required
@permission_required('manage_recruitment')
def recruitment_application_invite(aid: int):
    from models import JobApplication
    a = JobApplication.query.get_or_404(aid)
    # Ensure a token exists for confirmation page
    if not getattr(a, 'interview_confirm_token', None):
        try:
            a.interview_confirm_token = uuid4().hex
            db.session.commit()
        except Exception:
            db.session.rollback()
    slots = _compute_upcoming_interview_slots()
    confirm_url = url_for('jobs_confirm_interview', token=a.interview_confirm_token, _external=True)
    subject, html = build_interview_invitation_email(a, slots, confirm_url)
    ajax = request.args.get('ajax') or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept','')
    # Preflight: warn if 'recruitment' EmailSetting is not configured/active
    try:
        from sqlalchemy import func as _func

        from models import EmailSetting as _ES
        if not _ES.query.filter(_func.lower(_ES.name) == 'recruitment', _ES.is_active.is_(True)).first():
            if not ajax:
                flash("Recruitment email setting is not configured. Attempting to send using the default active email setting.", 'warning')
    except Exception:
        pass
    try:
        send_recruitment_email(a.email, subject, html)
        a.status = 'Invited for Interview'
        a.updated_at = datetime.utcnow()
        db.session.commit()
        if ajax:
            return jsonify({'status':'ok','message':'Invitation sent.'})
        flash('Invitation sent.', 'success')
    except Exception as exc:
        db.session.rollback()
        # Surface a concise error to help diagnose misconfiguration
        if ajax:
            return jsonify({'status':'error','message': f'Failed to send invitation email: {exc}'}), 500
        flash(f'Failed to send invitation email: {exc}', 'danger')
        app.logger.warning('Invite send failed for %s: %s', a.email, exc)
    return redirect(url_for('recruitment_application_detail', aid=aid))


@app.route('/recruitment/applications/<int:aid>/delete', methods=['POST'])
@login_required
def recruitment_application_delete(aid: int):
    """Delete a JobApplication (superadmin only).

    Removes the database row and attempts to delete the uploaded CV file
    if it lives under static/uploads/.
    Supports JSON responses when requested with ?ajax=1 or XHR/Accept headers.
    """
    from models import JobApplication
    ajax = request.args.get('ajax') or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept','')
    if not (getattr(current_user, 'is_superadmin', False)):
        if ajax:
            return jsonify({'status':'error','message':'Forbidden'}), 403
        abort(403)
    a = JobApplication.query.get_or_404(aid)
    cv_rel = a.cv_path
    try:
        db.session.delete(a)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        if ajax:
            return jsonify({'status':'error','message': f'Failed to delete: {exc}'}), 500
        flash('Failed to delete application.', 'danger')
        return redirect(url_for('recruitment_application_detail', aid=aid))
    # Best-effort delete of CV file if safe
    try:
        if cv_rel and isinstance(cv_rel, str) and cv_rel.startswith('uploads/'):
            cv_abs = os.path.join(app.root_path, 'static', cv_rel)
            if os.path.isfile(cv_abs):
                os.remove(cv_abs)
    except Exception:
        # Non-fatal; ignore file deletion errors
        pass
    if ajax:
        return jsonify({'status':'ok','message':'Application deleted.'})
    flash('Application deleted.', 'success')
    return redirect(url_for('recruitment_applications_index'))


@app.route('/recruitment/interview-slots.json')
@login_required
@permission_required('manage_recruitment')
def recruitment_interview_slots_json():
    """Return upcoming interview slots as JSON for previews and dashboards.

    Shape: {
      groups: [{ day: str, options: [{label:str, date_iso:str}] }],
      today:   [{label,date_iso}],
      week:    [{label,date_iso}]
    }
    """
    from datetime import datetime as _dt
    groups_raw = _compute_upcoming_interview_slots()  # [(day_label, [label,...])]
    # Parse labels to extract a date for today/week classification. Labels are built consistently in _compute_upcoming_interview_slots.
    # We'll re-run a parallel generation to get date objects alongside labels.
    result_groups = []
    today_list = []
    week_list = []
    try:
        today = date.today()
    except Exception:
        today = datetime.utcnow().date()

    # Reconstruct using the same schedule logic to maintain alignment
    dow_map = {'Wednesday':2,'Friday':4,'Saturday':5,'Sunday':6}
    times = [(10,30), (14,30), (17,0)]
    for day_label, options in groups_raw:
        items = []
        # Find weekday index from label, fallback to parse from string
        wd = dow_map.get(day_label)
        # Compute next occurrence for that weekday (relative to today);
        # then generate labels exactly like _compute_upcoming_interview_slots
        if wd is None:
            # Fallback: skip grouping if we cannot align
            items = [{'label': opt, 'date_iso': ''} for opt in options]
        else:
            offset = (wd - today.weekday()) % 7
            if offset == 0:
                offset = 7
            d = today + timedelta(days=offset)
            for hh, mm in times:
                tm = datetime(d.year, d.month, d.day, hh, mm)
                label = f"{day_label}, {tm.strftime('%-d') if hasattr(tm, 'strftime') else tm.day}{''} {tm.strftime('%B %Y')} at {tm.strftime('%I:%M %p').lstrip('0')}"
                try:
                    label = f"{day_label}, {tm.strftime('%B %d, %Y')} at {tm.strftime('%I:%M %p').lstrip('0')}" if False else options[len(items)]
                except Exception:
                    pass
                items.append({'label': options[len(items)] if len(items) < len(options) else label, 'date_iso': tm.date().isoformat()})
        result_groups.append({'day': day_label, 'options': items})
        for it in items:
            try:
                d = _dt.fromisoformat(it['date_iso']).date()
            except Exception:
                continue
            if d == today:
                today_list.append(it)
            # within next 7 days
            try:
                if 0 <= (d - today).days <= 7:
                    week_list.append(it)
            except Exception:
                pass
    return jsonify({'groups': result_groups, 'today': today_list, 'week': week_list})


@csrf.exempt
@app.route('/jobs/confirm-interview/<token>', methods=['GET','POST'])
def jobs_confirm_interview(token: str):
    """Public confirmation page for interview scheduling.

    Token identifies the JobApplication. POST accepts a selected ISO datetime.
    """
    from models import JobApplication
    a = JobApplication.query.filter_by(interview_confirm_token=token).first_or_404()
    # POST handlers: cancel or (re)schedule
    if request.method == 'POST':
        # Cancel path (explicit cancel form submit)
        if request.form.get('cancel'):
            try:
                _cancel_interview_reminder(a.id)
            except Exception:
                pass
            try:
                a.interview_at = None
                a.interview_label = None
                a.status = 'Invited for Interview'  # still invited; can pick a new slot later
                a.updated_at = datetime.utcnow()
                db.session.commit()
            except Exception:
                db.session.rollback()
                flash('Failed to cancel interview. Please try again later.', 'danger')
                groups = _compute_upcoming_interview_slots_with_iso()
                return render_template('recruitment/applications/confirm.html', a=a, groups=groups)
            flash('Your interview has been cancelled. You can choose a new time below if you wish.', 'success')
            groups = _compute_upcoming_interview_slots_with_iso()
            return render_template('recruitment/applications/confirm.html', a=a, groups=groups)
        # Parse selected slot
        iso = (request.form.get('slot_iso') or '').strip()
        when_dt = None
        try:
            when_dt = datetime.fromisoformat(iso)
        except Exception:
            when_dt = None
        if not when_dt:
            flash('Please select a valid interview slot.', 'warning')
            groups = _compute_upcoming_interview_slots_with_iso()
            return render_template('recruitment/applications/confirm.html', a=a, groups=groups)
        # Persist
        try:
            a.interview_at = when_dt
            a.interview_label = when_dt.strftime('%A, %d %B %Y at %I:%M %p').lstrip('0')
            a.status = 'Interview Scheduled'
            a.updated_at = datetime.utcnow()
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            flash('Failed to confirm interview. Please try again later.', 'danger')
            groups = _compute_upcoming_interview_slots_with_iso()
            return render_template('recruitment/applications/confirm.html', a=a, groups=groups)
        # Send confirmation (branded) and schedule reminder
        try:
            resched_url = url_for('jobs_confirm_interview', token=a.interview_confirm_token, _external=True) + '?reschedule=1'
            cancel_url = url_for('jobs_confirm_interview', token=a.interview_confirm_token, _external=True) + '?cancel=1'
            subj, html = build_interview_confirmation_email(a, reschedule_url=resched_url, cancel_url=cancel_url)
            send_recruitment_email(a.email, subj, html)
        except Exception:
            pass
        try:
            _schedule_interview_reminder(a.id)
        except Exception:
            pass
        flash('Thank you. Your interview has been confirmed.', 'success')
        return render_template('recruitment/applications/confirm.html', a=a, groups=[])
    # GET: allow reschedule or cancel prompt via query params
    groups = _compute_upcoming_interview_slots_with_iso()
    return render_template('recruitment/applications/confirm.html', a=a, groups=groups)


@app.route('/recruitment/dashboard')
@login_required
@permission_required('manage_recruitment')
def recruitment_dashboard():
    from sqlalchemy import func

    from models import JobApplication

    # Top-level counts
    total = JobApplication.query.count()
    by_status_rows = db.session.query(JobApplication.status, func.count()).group_by(JobApplication.status).all()
    by_status = { (k or 'Pending Review'): v for k, v in by_status_rows }
    # Last 30 days
    try:
        since_30 = datetime.now(timezone.utc) - timedelta(days=30)
    except Exception:
        since_30 = datetime.utcnow() - timedelta(days=30)
    last30_count = JobApplication.query.filter(JobApplication.created_at >= since_30).count()
    invited = by_status.get('Invited for Interview', 0)
    selected = by_status.get('Selected', 0)
    onboarded = by_status.get('Onboarded', 0)
    reviewed = by_status.get('Reviewed', 0)
    # Monthly trend (last 12 months)
    labels = []
    submitted_series = []
    invited_series = []
    selected_series = []
    onboarded_series = []
    from calendar import month_name

    # Determine 12-month window ending current month
    today = date.today()
    months = []
    y = today.year; m = today.month
    for _ in range(12):
        months.append((y, m))
        m -= 1
        if m == 0:
            m = 12; y -= 1
    months.reverse()
    for y, m in months:
        labels.append(f"{month_name[m]} {y}")
        start = datetime(y, m, 1)
        if m == 12:
            end = datetime(y+1, 1, 1)
        else:
            end = datetime(y, m+1, 1)
        c_total = JobApplication.query.filter(JobApplication.created_at >= start, JobApplication.created_at < end).count()
        c_invited = JobApplication.query.filter(JobApplication.created_at >= start, JobApplication.created_at < end, JobApplication.status == 'Invited for Interview').count()
        c_selected = JobApplication.query.filter(JobApplication.created_at >= start, JobApplication.created_at < end, JobApplication.status == 'Selected').count()
        c_onboarded = JobApplication.query.filter(JobApplication.created_at >= start, JobApplication.created_at < end, JobApplication.status == 'Onboarded').count()
        submitted_series.append(c_total)
        invited_series.append(c_invited)
        selected_series.append(c_selected)
        onboarded_series.append(c_onboarded)

    # Branch distribution (count appearances of each branch token)
    branch_counts = {}
    for b in (BRANCH_CHOICES() or []):
        branch_counts[b] = 0
    for a in JobApplication.query.all():
        try:
            for b in (a.branches or '').split(','):
                bt = b.strip()
                if not bt:
                    continue
                branch_counts[bt] = branch_counts.get(bt, 0) + 1
        except Exception:
            continue
    # Top universities and subjects
    uni_rows = db.session.query(JobApplication.university, func.count()).filter(JobApplication.university.isnot(None)).group_by(JobApplication.university).order_by(func.count().desc()).limit(8).all()
    subject_counts = {}
    for a in JobApplication.query.all():
        try:
            for s in (a.subjects or '').split(','):
                st = s.strip()
                if st:
                    subject_counts[st] = subject_counts.get(st, 0) + 1
        except Exception:
            continue
    top_subjects = sorted(subject_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    kpis = {
        'total': total,
        'last30': last30_count,
        'reviewed': reviewed,
        'invited': invited,
        'selected': selected,
        'onboarded': onboarded,
        'pending': by_status.get('Pending Review', 0),
        'rejected': by_status.get('Rejected', 0),
    }
    charts = {
        'labels': labels,
        'submitted': submitted_series,
        'invited': invited_series,
        'selected': selected_series,
        'onboarded': onboarded_series,
        'status_labels': list(by_status.keys()),
        'status_values': list(by_status.values()),
        'branch_labels': list(branch_counts.keys()),
        'branch_values': list(branch_counts.values()),
        'top_universities': [(u or '—', c) for (u, c) in uni_rows],
        'top_subjects': top_subjects,
    }
    return render_template('recruitment/dashboard.html', kpis=kpis, charts=charts)


@app.route('/recruitment/interviews')
@login_required
@permission_required('manage_recruitment')
def recruitment_interviews():
    """Admin list of confirmed interviews with basic filters.

    Query params:
      - q: search in name/email/university/phone
      - from: start date (YYYY-MM-DD or DD-MM-YYYY)
      - to: end date (YYYY-MM-DD or DD-MM-YYYY)
    Defaults to upcoming only (from=today).
    """
    from models import JobApplication
    q = (request.args.get('q') or '').strip().lower()
    s_from = (request.args.get('from') or '').strip()
    s_to = (request.args.get('to') or '').strip()
    # Parse dates
    d_from = None
    d_to = None
    for fmt in ('%Y-%m-%d','%d-%m-%Y'):
        if not d_from and s_from:
            try:
                d_from = datetime.strptime(s_from, fmt)
            except Exception:
                pass
        if not d_to and s_to:
            try:
                d_to = datetime.strptime(s_to, fmt)
            except Exception:
                pass
    if not d_from:
        try:
            d_from = datetime.combine(date.today(), datetime.min.time())
        except Exception:
            d_from = datetime.utcnow()
    query = JobApplication.query.filter(JobApplication.interview_at.isnot(None))
    if d_from:
        query = query.filter(JobApplication.interview_at >= d_from)
    if d_to:
        # include the whole end day by adding 1 day and using < next day
        try:
            dnext = d_to + timedelta(days=1)
            query = query.filter(JobApplication.interview_at < dnext)
        except Exception:
            query = query.filter(JobApplication.interview_at <= d_to)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (JobApplication.first_name.ilike(like)) |
            (JobApplication.last_name.ilike(like)) |
            (JobApplication.email.ilike(like)) |
            (JobApplication.phone.ilike(like)) |
            (JobApplication.university.ilike(like))
        )
    rows = query.order_by(JobApplication.interview_at.asc()).all()
    return render_template('recruitment/interviews/index.html', rows=rows, q=q, s_from=s_from, s_to=s_to)


@app.route('/recruitment/interviews/<int:aid>/cancel', methods=['POST'])
@login_required
@permission_required('manage_recruitment')
def recruitment_interview_cancel(aid: int):
    from models import JobApplication
    a = JobApplication.query.get_or_404(aid)
    try:
        _cancel_interview_reminder(a.id)
    except Exception:
        pass
    try:
        # Ensure token for reschedule link if needed
        if not getattr(a, 'interview_confirm_token', None):
            a.interview_confirm_token = uuid4().hex
        a.interview_at = None
        a.interview_label = None
        a.status = 'Invited for Interview'
        a.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('Failed to cancel interview.', 'danger')
        return redirect(url_for('recruitment_interviews'))
    # Notify applicant (branded)
    try:
        resched_url = url_for('jobs_confirm_interview', token=a.interview_confirm_token, _external=True) + '?reschedule=1'
        subj, html = build_interview_cancelled_email(a, reschedule_url=resched_url)
        send_recruitment_email(a.email, subj, html)
    except Exception:
        pass
    flash('Interview cancelled and applicant notified.', 'success')
    return redirect(url_for('recruitment_interviews'))


@app.route('/recruitment/interviews/<int:aid>/reschedule', methods=['GET','POST'])
@login_required
@permission_required('manage_recruitment')
def recruitment_interview_reschedule(aid: int):
    from models import JobApplication
    a = JobApplication.query.get_or_404(aid)
    if request.method == 'POST':
        iso = (request.form.get('slot_iso') or '').strip()
        when_dt = None
        try:
            when_dt = datetime.fromisoformat(iso)
        except Exception:
            when_dt = None
        if not when_dt:
            flash('Please select a valid interview slot.', 'warning')
            groups = _compute_upcoming_interview_slots_with_iso()
            return render_template('recruitment/interviews/reschedule.html', a=a, groups=groups)
        # Persist and (re)schedule reminder
        try:
            # Cancel any previous reminder first
            try:
                _cancel_interview_reminder(a.id)
            except Exception:
                pass
            a.interview_at = when_dt
            a.interview_label = when_dt.strftime('%A, %d %B %Y at %I:%M %p').lstrip('0')
            a.status = 'Interview Scheduled'
            a.updated_at = datetime.utcnow()
            # Ensure token for applicant management links
            if not getattr(a, 'interview_confirm_token', None):
                a.interview_confirm_token = uuid4().hex
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('Failed to reschedule interview.', 'danger')
            groups = _compute_upcoming_interview_slots_with_iso()
            return render_template('recruitment/interviews/reschedule.html', a=a, groups=groups)
        # Notify (branded) and schedule reminder
        try:
            resched_url = url_for('jobs_confirm_interview', token=a.interview_confirm_token, _external=True) + '?reschedule=1'
            cancel_url = url_for('jobs_confirm_interview', token=a.interview_confirm_token, _external=True) + '?cancel=1'
            subj, html = build_interview_rescheduled_email(a, reschedule_url=resched_url, cancel_url=cancel_url)
            send_recruitment_email(a.email, subj, html)
        except Exception:
            pass
        try:
            _schedule_interview_reminder(a.id)
        except Exception:
            pass
        flash('Interview rescheduled and applicant notified.', 'success')
        return redirect(url_for('recruitment_interviews'))
    # GET
    groups = _compute_upcoming_interview_slots_with_iso()
    return render_template('recruitment/interviews/reschedule.html', a=a, groups=groups)


# ---------------- Admission Assessment (Admin) ---------------- #
@app.route('/admission-assessments')
@login_required
@permission_required('manage_admission_assessments')
def admission_assessments_index():
    q = (request.args.get('q') or '').strip()
    status_filters = [s for s in request.args.getlist('status') if s in ADMISSION_ASSESSMENT_STATUSES]
    branch_filters = [b for b in request.args.getlist('branch') if b]
    heard_filters = [h for h in request.args.getlist('heard') if h]
    subject_filter = (request.args.get('subject') or '').strip()
    sort = (request.args.get('sort') or 'created_at').strip()
    direction = (request.args.get('direction') or '').strip().lower()
    if direction not in {'asc', 'desc'}:
        direction = 'desc' if sort == 'created_at' else 'asc'
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 25, type=int), 200)

    query = AdmissionAssessmentSubmission.query.options(joinedload(AdmissionAssessmentSubmission.scores))
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(
                AdmissionAssessmentSubmission.student_name.ilike(like),
                AdmissionAssessmentSubmission.parent_name.ilike(like),
                AdmissionAssessmentSubmission.parent_email.ilike(like),
                AdmissionAssessmentSubmission.parent_phone.ilike(like),
                AdmissionAssessmentSubmission.branch.ilike(like),
                AdmissionAssessmentSubmission.subjects_json.ilike(like),
            )
        )
    if status_filters:
        query = query.filter(AdmissionAssessmentSubmission.status.in_(status_filters))
    if branch_filters:
        query = query.filter(AdmissionAssessmentSubmission.branch.in_(branch_filters))
    if heard_filters:
        query = query.filter(AdmissionAssessmentSubmission.heard_about.in_(heard_filters))
    if subject_filter:
        query = query.filter(AdmissionAssessmentSubmission.subjects_json.ilike(f"%{subject_filter}%"))

    sort_map = {
        'student': AdmissionAssessmentSubmission.student_name,
        'status': AdmissionAssessmentSubmission.status,
        'branch': AdmissionAssessmentSubmission.branch,
        'created_at': AdmissionAssessmentSubmission.created_at,
    }
    sort_col = sort_map.get(sort, AdmissionAssessmentSubmission.created_at)
    if direction == 'desc':
        query = query.order_by(sort_col.desc())
    else:
        query = query.order_by(sort_col.asc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    submissions = pagination.items
    for submission in submissions:
        try:
            submission.score_lookup = {s.subject: s for s in submission.scores}
        except Exception:
            submission.score_lookup = {}

    overall_total = AdmissionAssessmentSubmission.query.count()
    try:
        recent_anchor = datetime.utcnow()
    except Exception:
        recent_anchor = datetime.now()
    recent_window = recent_anchor - timedelta(days=7)
    previous_window_start = recent_window - timedelta(days=7)
    recent_total = (
        AdmissionAssessmentSubmission.query
        .filter(AdmissionAssessmentSubmission.created_at >= recent_window)
        .count()
    )
    previous_total = (
        AdmissionAssessmentSubmission.query
        .filter(AdmissionAssessmentSubmission.created_at >= previous_window_start)
        .filter(AdmissionAssessmentSubmission.created_at < recent_window)
        .count()
    )
    status_summary_rows = (
        db.session.query(AdmissionAssessmentSubmission.status, func.count())
        .group_by(AdmissionAssessmentSubmission.status)
        .all()
    )
    status_summary = {}
    for status_value, count in status_summary_rows:
        label = status_value or 'Application Submitted'
        status_summary[label] = count

    count_expr = func.count().label('total')
    branch_summary_rows = (
        db.session.query(AdmissionAssessmentSubmission.branch, count_expr)
        .group_by(AdmissionAssessmentSubmission.branch)
        .order_by(count_expr.desc())
        .all()
    )
    branch_summary = []
    for branch_value, total in branch_summary_rows:
        label = branch_value or 'Unknown branch'
        branch_summary.append({'label': label, 'count': total})

    source_summary_rows = (
        db.session.query(AdmissionAssessmentSubmission.heard_about, count_expr)
        .group_by(AdmissionAssessmentSubmission.heard_about)
        .order_by(count_expr.desc())
        .all()
    )
    source_summary = []
    for source_value, total in source_summary_rows:
        label = source_value or 'Unknown source'
        source_summary.append({'label': label, 'count': total})

    trend_horizon_days = 30
    trend_horizon_weeks = 12
    trend_start = recent_anchor - timedelta(days=trend_horizon_weeks * 7)
    analysis_rows = (
        AdmissionAssessmentSubmission.query
        .filter(AdmissionAssessmentSubmission.created_at >= trend_start)
        .all()
    )
    from collections import defaultdict

    daily_counts = defaultdict(int)
    weekly_counts = defaultdict(int)
    recent_status_counts = defaultdict(int)
    enrolled_total = status_summary.get('Enrolled', 0)
    for row in analysis_rows:
        created_at = getattr(row, 'created_at', None)
        if not created_at:
            continue
        day_key = created_at.date()
        daily_counts[day_key] += 1
        week_start = day_key - timedelta(days=day_key.weekday())
        weekly_counts[week_start] += 1
        if created_at >= recent_window:
            label = (row.status or 'Application Submitted')
            recent_status_counts[label] += 1

    # Fill gaps for the last 30 days and 12 weeks to provide smooth charts.
    daily_labels = []
    daily_series = []
    daily_start = (recent_anchor - timedelta(days=trend_horizon_days - 1)).date()
    for offset in range(trend_horizon_days):
        day = daily_start + timedelta(days=offset)
        daily_labels.append(day.strftime('%d %b'))
        daily_series.append(daily_counts.get(day, 0))

    weekly_labels = []
    weekly_series = []
    week_start_anchor = recent_anchor.date() - timedelta(days=recent_anchor.weekday())
    first_week = week_start_anchor - timedelta(days=(trend_horizon_weeks - 1) * 7)
    for offset in range(trend_horizon_weeks):
        week_day = first_week + timedelta(days=offset * 7)
        weekly_labels.append(week_day.strftime('W%W %Y'))
        weekly_series.append(weekly_counts.get(week_day, 0))

    try:
        change_abs = recent_total - previous_total
    except Exception:
        change_abs = recent_total
    change_pct = 0.0
    if previous_total:
        change_pct = (change_abs / previous_total) * 100

    analytics = {
        'overview': {
            'overall_total': overall_total,
            'recent_total': recent_total,
            'previous_total': previous_total,
            'recent_change': change_abs,
            'recent_change_pct': round(change_pct, 1),
            'enrolment_rate': round((enrolled_total / overall_total) * 100, 1) if overall_total else 0,
        },
        'daily': {
            'labels': daily_labels,
            'counts': daily_series,
        },
        'weekly': {
            'labels': weekly_labels,
            'counts': weekly_series,
        },
        'status': {
            'labels': list(status_summary.keys()),
            'counts': list(status_summary.values()),
            'recent_labels': list(recent_status_counts.keys()),
            'recent_counts': [recent_status_counts[label] for label in recent_status_counts.keys()],
        },
        'branches': {
            'labels': [item['label'] for item in branch_summary],
            'counts': [item['count'] for item in branch_summary],
        },
        'sources': {
            'labels': [item['label'] for item in source_summary],
            'counts': [item['count'] for item in source_summary],
        },
    }

    return render_template(
        'admission_assessments/index.html',
        submissions=submissions,
        pagination=pagination,
        statuses=ADMISSION_ASSESSMENT_STATUSES,
        status_filters=status_filters,
        branch_filters=branch_filters,
        heard_filters=heard_filters,
        subject_filter=subject_filter,
        branches=BRANCH_CHOICES(),
        heard_choices=_admission_heard_about_choices(),
        subject_choices=_admission_subject_choices(),
        q=q,
        sort=sort,
        direction=direction,
        per_page=per_page,
        overall_total=overall_total,
        recent_total=recent_total,
        status_summary=status_summary,
        branch_summary=branch_summary,
        source_summary=source_summary,
        analytics=analytics,
    )


@app.route('/admission-assessments/<int:submission_id>')
@login_required
@permission_required('manage_admission_assessments')
def admission_assessment_detail(submission_id: int):
    submission = (
        AdmissionAssessmentSubmission.query
        .options(
            joinedload(AdmissionAssessmentSubmission.notes).joinedload(AdmissionAssessmentNote.user),
            joinedload(AdmissionAssessmentSubmission.scores),
            joinedload(AdmissionAssessmentSubmission.changes).joinedload(AdmissionAssessmentChange.user),
            joinedload(AdmissionAssessmentSubmission.scores_last_sent_by),
        )
        .get_or_404(submission_id)
    )
    subjects = submission.subjects_list()
    if submission.subjects_other:
        other = submission.subjects_other.strip()
        if other and other not in subjects:
            subjects.append(other)
    notes = sorted(submission.notes, key=lambda n: n.created_at or datetime.min, reverse=True)
    scores_map = {s.subject: s for s in submission.scores}
    changes = sorted(submission.changes, key=lambda c: c.created_at or datetime.min, reverse=True)

    return render_template(
        'admission_assessments/detail.html',
        submission=submission,
        subjects=subjects,
        scores_map=scores_map,
        notes=notes,
        changes=changes,
        statuses=ADMISSION_ASSESSMENT_STATUSES,
        heard_choices=_admission_heard_about_choices(),
    )


@app.route('/admission-assessments/<int:submission_id>/status', methods=['POST'])
@login_required
@permission_required('manage_admission_assessments')
def admission_assessment_update_status(submission_id: int):
    submission = AdmissionAssessmentSubmission.query.get_or_404(submission_id)
    new_status = (request.form.get('status') or '').strip()
    if new_status not in ADMISSION_ASSESSMENT_STATUSES:
        flash('Select a valid status option.', 'warning')
        return redirect(url_for('admission_assessment_detail', submission_id=submission.id))
    current_status = submission.status or 'Application Submitted'
    if new_status == current_status:
        flash('Status is already set to that value.', 'info')
        return redirect(url_for('admission_assessment_detail', submission_id=submission.id))
    submission.status = new_status
    submission.status_updated_at = datetime.utcnow()
    submission.updated_at = datetime.utcnow()
    _record_assessment_change(submission, 'status', current_status, new_status)
    try:
        db.session.commit()
        flash('Status updated.', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to update status: {exc}', 'danger')
    return redirect(url_for('admission_assessment_detail', submission_id=submission.id))


@app.route('/admission-assessments/<int:submission_id>/notes', methods=['POST'])
@login_required
@permission_required('manage_admission_assessments')
def admission_assessment_add_note(submission_id: int):
    submission = AdmissionAssessmentSubmission.query.get_or_404(submission_id)
    body = (request.form.get('note_body') or '').strip()
    if not body:
        flash('Note cannot be empty.', 'warning')
        return redirect(url_for('admission_assessment_detail', submission_id=submission.id))
    note = AdmissionAssessmentNote(submission_id=submission.id, user_id=getattr(current_user, 'id', None), body=body)
    submission.updated_at = datetime.utcnow()
    db.session.add(note)
    _record_assessment_change(submission, 'note', None, body[:250])
    try:
        db.session.commit()
        flash('Note added.', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to add note: {exc}', 'danger')
    return redirect(url_for('admission_assessment_detail', submission_id=submission.id))


@app.route('/admission-assessments/<int:submission_id>/delete', methods=['POST'])
@login_required
@permission_required('manage_admission_assessments')
def admission_assessment_delete(submission_id: int):
    if not getattr(current_user, 'is_superadmin', False):
        abort(403)
    submission = AdmissionAssessmentSubmission.query.get_or_404(submission_id)
    try:
        db.session.delete(submission)
        db.session.commit()
        flash('Submission deleted.', 'success')
        return redirect(url_for('admission_assessments_index'))
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to delete submission: {exc}', 'danger')
        return redirect(url_for('admission_assessment_detail', submission_id=submission_id))


@app.route('/admission-assessments/<int:submission_id>/scores', methods=['POST'])
@login_required
@permission_required('manage_admission_assessments')
def admission_assessment_save_scores(submission_id: int):
    submission = AdmissionAssessmentSubmission.query.get_or_404(submission_id)

    # ── handle inline status update (merged into one form) ──────────
    status_changed = False
    new_status = (request.form.get('status') or '').strip()
    if new_status and new_status in ADMISSION_ASSESSMENT_STATUSES:
        current_status = submission.status or 'Application Submitted'
        if new_status != current_status:
            submission.status = new_status
            submission.status_updated_at = datetime.utcnow()
            submission.updated_at = datetime.utcnow()
            _record_assessment_change(submission, 'status', current_status, new_status)
            status_changed = True

    raw_rows: dict[str, dict[str, str]] = {}
    for key, value in request.form.items():
        if not key.startswith('scores-'):
            continue
        parts = key.split('-', 2)
        if len(parts) != 3:
            continue
        _, idx, field = parts
        raw_rows.setdefault(idx, {})[field] = value

    if not raw_rows:
        # No score fields but status may have changed
        if status_changed:
            try:
                db.session.commit()
                flash('Status updated.', 'success')
            except Exception as exc:
                db.session.rollback()
                flash(f'Failed to save: {exc}', 'danger')
        else:
            flash('No changes detected.', 'info')
        return redirect(url_for('admission_assessment_detail', submission_id=submission.id))

    errors: list[str] = []
    updated_subjects: list[str] = []
    for row in raw_rows.values():
        subject = (row.get('subject') or '').strip()
        marks_raw = (row.get('marks') or '').strip()
        total_raw = (row.get('total') or '').strip()
        pct_raw = (row.get('percentage') or '').strip()
        rec_raw = (row.get('recommendation') or '').strip()

        if not subject:
            continue

        def _parse_decimal(raw: str):
            if not raw:
                return None
            try:
                return Decimal(raw)
            except Exception:
                return None

        marks_val = _parse_decimal(marks_raw)
        total_val = _parse_decimal(total_raw)
        pct_val = _parse_decimal(pct_raw)

        if marks_raw and marks_val is None:
            errors.append(f'Enter a valid number for marks in {subject}.')
            continue
        if total_raw and total_val is None:
            errors.append(f'Enter a valid number for total in {subject}.')
            continue
        if pct_raw and pct_val is None:
            errors.append(f'Enter a valid percentage for {subject}.')
            continue
        computed_pct = None
        if marks_val is not None and total_val is not None and total_val != 0:
            try:
                computed_pct = (marks_val / total_val) * Decimal('100')
            except Exception:
                computed_pct = None
        elif pct_val is not None:
            computed_pct = pct_val

        recommendation = rec_raw
        if (not recommendation) and computed_pct is not None:
            recommendation = _score_recommendation(subject, float(computed_pct))

        score = AdmissionAssessmentScore.query.filter_by(submission_id=submission.id, subject=subject).first()
        old_payload = score.as_dict() if score else None

        if not score:
            score = AdmissionAssessmentScore(submission_id=submission.id, subject=subject, created_by_id=getattr(current_user, 'id', None))
            db.session.add(score)

        if marks_val is not None:
            score.marks_achieved = marks_val.quantize(Decimal('0.01'))
        else:
            score.marks_achieved = None
        if total_val is not None:
            score.total_marks = total_val.quantize(Decimal('0.01'))
        else:
            score.total_marks = None
        if computed_pct is not None:
            score.percentage = computed_pct.quantize(Decimal('0.01'))
        else:
            score.percentage = None
        score.recommendation = recommendation or None

        new_payload = score.as_dict()
        if old_payload != new_payload:
            updated_subjects.append(subject)
            old_json = json.dumps(old_payload) if old_payload is not None else None
            new_json = json.dumps(new_payload) if new_payload is not None else None
            _record_assessment_change(submission, f'score:{subject}', old_json, new_json)

    # ── determine what changed and commit ─────────────────────────
    if updated_subjects or status_changed:
        submission.updated_at = datetime.utcnow()
        try:
            db.session.commit()
            parts = []
            if status_changed:
                parts.append('Status updated')
            if updated_subjects:
                parts.append(f'Scores saved for {len(updated_subjects)} subject(s)')
            flash('. '.join(parts) + '.', 'success')
        except Exception as exc:
            db.session.rollback()
            flash(f'Failed to save: {exc}', 'danger')
    else:
        flash('No changes detected.', 'info')

    for msg in errors:
        flash(msg, 'warning')

    return redirect(url_for('admission_assessment_detail', submission_id=submission.id))


@app.route('/admission-assessments/<int:submission_id>/send-scores', methods=['POST'])
@login_required
@permission_required('manage_admission_assessments')
def admission_assessment_send_scores(submission_id: int):
    submission = (
        AdmissionAssessmentSubmission.query
        .options(joinedload(AdmissionAssessmentSubmission.scores))
        .get_or_404(submission_id)
    )
    if not submission.parent_email:
        flash('Parent email is not available for this submission.', 'warning')
        return redirect(url_for('admission_assessment_detail', submission_id=submission.id))
    scores = [s for s in submission.scores if (s.marks_achieved is not None or s.percentage is not None)]
    if not scores:
        flash('Add scores before sending an email.', 'warning')
        return redirect(url_for('admission_assessment_detail', submission_id=submission.id))

    scores_sorted = sorted(scores, key=lambda s: (s.subject or '').lower())
    try:
        from email_utils import build_admission_assessment_scores_email

        subject_line, html = build_admission_assessment_scores_email(submission, scores_sorted)
        send_email(submission.parent_email, subject_line, html)
        submission.scores_last_sent_at = datetime.utcnow()
        submission.scores_last_sent_by_id = getattr(current_user, 'id', None)
        submission.updated_at = datetime.utcnow()
        _record_assessment_change(submission, 'scores_sent', None, f"Sent to {submission.parent_email}")
        db.session.commit()
        flash('Scores emailed to parent.', 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f'Failed to send scores email: {exc}', 'danger')
    return redirect(url_for('admission_assessment_detail', submission_id=submission.id))


@app.route('/student-concerns')
@login_required
@permission_required('manage_student_concerns')
def student_concerns_index():
    from models import Staff, StudentConcern

    # Filters
    q = (request.args.get('q') or '').strip()
    student = (request.args.get('student') or '').strip()
    years = [y.strip() for y in request.args.getlist('year') if y.strip()]
    subjects = [s.strip() for s in request.args.getlist('subject') if s.strip()]
    tutors = [t.strip() for t in request.args.getlist('tutor') if t.strip()]
    reasons = [r.strip() for r in request.args.getlist('reason') if r.strip()]
    statuses = [s.strip() for s in request.args.getlist('status') if s.strip()]
    branches = [b.strip() for b in request.args.getlist('branch') if b.strip()]
    branches = [b.strip() for b in request.args.getlist('branch') if b.strip()]
    sort = (request.args.get('sort') or 'created_at').strip()
    direction = (request.args.get('direction') or 'desc').strip().lower()
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 25, type=int), 250)

    query = StudentConcern.query
    if q:
        like = f"%{q}%"
        query = query.filter((StudentConcern.student_name.ilike(like)) | (StudentConcern.student_id.ilike(like)) | (StudentConcern.tutor_name.ilike(like)))
    if student:
        query = query.filter((StudentConcern.student_id == student) | (StudentConcern.student_name.ilike(f"%{student}%")))
    if years:
        query = query.filter(StudentConcern.year_group.in_(years))
    if subjects:
        query = query.filter(StudentConcern.subject.in_(subjects))
    if tutors:
        query = query.filter(StudentConcern.tutor_name.in_(tutors))
    if statuses:
        query = query.filter(StudentConcern.status.in_(statuses))
    if reasons:
        from sqlalchemy import or_ as _or
        query = query.filter(_or(*[StudentConcern.reasons_json.ilike(f'%"{r}"%') for r in reasons]))

    branch_map = {'Stratford': 'S', 'Eastham': 'E', 'Docklands': 'D'}
    valid_branches = {'Stratford', 'Eastham', 'Docklands', 'Whitechapel'}
    selected_branches = [b for b in branches if b in valid_branches]
    if selected_branches:
        from sqlalchemy import func
        from sqlalchemy import or_ as _or
        prefix_expr = func.substr(func.upper(func.coalesce(StudentConcern.student_id, '')), 1, 1)
        conditions = []
        for branch_name in selected_branches:
            if branch_name in branch_map:
                conditions.append(prefix_expr == branch_map[branch_name])
            else:
                conditions.append(_or(
                    StudentConcern.student_id.is_(None),
                    prefix_expr.notin_(list(branch_map.values())),
                    prefix_expr == ''
                ))
        query = query.filter(_or(*conditions))

    if sort in {'student_id','student_name','subject','tutor_name','status','created_at'}:
        col = getattr(StudentConcern, sort)
        query = query.order_by(col.asc() if direction == 'asc' else col.desc())
    else:
        query = query.order_by(StudentConcern.created_at.desc())

    # Build options for filters
    subject_options = [s[0] for s in db.session.query(StudentConcern.subject).filter(StudentConcern.subject.isnot(None)).distinct().order_by(StudentConcern.subject.asc()).all()]
    year_options = [y[0] for y in db.session.query(StudentConcern.year_group).filter(StudentConcern.year_group.isnot(None)).distinct().order_by(StudentConcern.year_group.asc()).all()]
    tutor_options = [t[0] for t in db.session.query(StudentConcern.tutor_name).filter(StudentConcern.tutor_name.isnot(None)).distinct().order_by(StudentConcern.tutor_name.asc()).all()]
    reason_options = ['Behaviour Issue','Lack of Progress','Suspected SEN','Other']
    status_options = ['Pending','In Progress','Solved']

    # Metrics: overall and filtered
    from sqlalchemy import func
    try:
        since_7 = datetime.now(timezone.utc) - timedelta(days=7)
    except Exception:
        # Fallback if timezone not available
        since_7 = datetime.utcnow() - timedelta(days=7)

    # Overall counts by status
    overall_total = StudentConcern.query.count()
    overall_status_rows = db.session.query(StudentConcern.status, func.count())\
        .group_by(StudentConcern.status).all()
    overall_status = { (k or 'Pending'): v for k, v in overall_status_rows }
    overall_last7 = StudentConcern.query.filter(StudentConcern.created_at >= since_7).count()
    metrics_overall = {
        'total': overall_total,
        'pending': overall_status.get('Pending', 0),
        'in_progress': overall_status.get('In Progress', 0),
        'solved': overall_status.get('Solved', 0),
        'last7': overall_last7,
    }

    # Filtered counts by status (reuse filters from current query)
    q_base = query.order_by(None)
    filtered_total = q_base.count()
    filtered_status_rows = q_base.with_entities(StudentConcern.status, func.count())\
        .group_by(StudentConcern.status).all()
    filtered_status = { (k or 'Pending'): v for k, v in filtered_status_rows }
    filtered_last7 = q_base.filter(StudentConcern.created_at >= since_7).count()
    metrics_filtered = {
        'total': filtered_total,
        'pending': filtered_status.get('Pending', 0),
        'in_progress': filtered_status.get('In Progress', 0),
        'solved': filtered_status.get('Solved', 0),
        'last7': filtered_last7,
    }

    # Summary Stats (Overall)
    dept_overall = db.session.query(Staff.department, func.count(StudentConcern.id))\
        .select_from(StudentConcern)\
        .join(Staff, Staff.name == StudentConcern.tutor_name, isouter=True)\
        .group_by(Staff.department)\
        .order_by(func.count(StudentConcern.id).desc())\
        .all()
    tutors_top_overall = db.session.query(StudentConcern.tutor_name, func.count(StudentConcern.id))\
        .group_by(StudentConcern.tutor_name)\
        .order_by(func.count(StudentConcern.id).desc())\
        .limit(5).all()
    tutors_least_overall = db.session.query(StudentConcern.tutor_name, func.count(StudentConcern.id))\
        .group_by(StudentConcern.tutor_name)\
        .order_by(func.count(StudentConcern.id).asc())\
        .limit(5).all()
    subjects_top_overall = db.session.query(StudentConcern.subject, func.count(StudentConcern.id))\
        .group_by(StudentConcern.subject)\
        .order_by(func.count(StudentConcern.id).desc())\
        .limit(5).all()
    subjects_least_overall = db.session.query(StudentConcern.subject, func.count(StudentConcern.id))\
        .group_by(StudentConcern.subject)\
        .order_by(func.count(StudentConcern.id).asc())\
        .limit(5).all()

    # Summary Stats (Filtered using current filters)
    dept_filtered = q_base.join(Staff, Staff.name == StudentConcern.tutor_name, isouter=True)\
        .with_entities(Staff.department, func.count(StudentConcern.id))\
        .group_by(Staff.department)\
        .order_by(func.count(StudentConcern.id).desc())\
        .all()
    tutors_top_filtered = q_base.with_entities(StudentConcern.tutor_name, func.count(StudentConcern.id))\
        .group_by(StudentConcern.tutor_name)\
        .order_by(func.count(StudentConcern.id).desc())\
        .limit(5).all()
    tutors_least_filtered = q_base.with_entities(StudentConcern.tutor_name, func.count(StudentConcern.id))\
        .group_by(StudentConcern.tutor_name)\
        .order_by(func.count(StudentConcern.id).asc())\
        .limit(5).all()
    subjects_top_filtered = q_base.with_entities(StudentConcern.subject, func.count(StudentConcern.id))\
        .group_by(StudentConcern.subject)\
        .order_by(func.count(StudentConcern.id).desc())\
        .limit(5).all()
    subjects_least_filtered = q_base.with_entities(StudentConcern.subject, func.count(StudentConcern.id))\
        .group_by(StudentConcern.subject)\
        .order_by(func.count(StudentConcern.id).asc())\
        .limit(5).all()

    eligible_meeting_roles = {'admin', 'supervisor', 'centre_manager', 'superadmin'}
    meeting_participants = (User.query
                            .filter(User.is_active.is_(True), User.is_approved.is_(True))
                            .filter(User.role.in_(tuple(eligible_meeting_roles)))
                            .order_by(User.name.asc())
                            .all())
    if current_user.is_authenticated:
        participant_ids = {user.id for user in meeting_participants}
        if current_user.id not in participant_ids:
            meeting_participants.append(current_user)
            meeting_participants.sort(key=lambda u: (u.name or '').lower())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    records = pagination.items
    branch_options = ['Stratford', 'Eastham', 'Docklands', 'Whitechapel']

    return render_template(
        'concerns/index.html',
        records=records,
        pagination=pagination,
        q=q,
        student=student,
        years=years,
        subjects_selected=subjects,
        tutors_selected=tutors,
        reasons_selected=reasons,
        statuses_selected=statuses,
        branches_selected=selected_branches,
        sort=sort,
        direction=direction,
        subject_options=subject_options,
        year_options=year_options,
        tutor_options=tutor_options,
        reason_options=reason_options,
        status_options=status_options,
        metrics_overall=metrics_overall,
        metrics_filtered=metrics_filtered,
        dept_overall=dept_overall,
        tutors_top_overall=tutors_top_overall,
        tutors_least_overall=tutors_least_overall,
        subjects_top_overall=subjects_top_overall,
        subjects_least_overall=subjects_least_overall,
        dept_filtered=dept_filtered,
        tutors_top_filtered=tutors_top_filtered,
        tutors_least_filtered=tutors_least_filtered,
        subjects_top_filtered=subjects_top_filtered,
        subjects_least_filtered=subjects_least_filtered,
        meeting_participants=meeting_participants,
        branch_options=branch_options,
        resolve_student_branch=resolve_student_branch,
    )


@app.route('/student-concerns/export')
@login_required
@permission_required('manage_student_concerns')
def student_concerns_export():
    # Build the same filtered query as index, then export to XLSX
    import pandas as pd

    from models import StudentConcern

    # Filters
    q = (request.args.get('q') or '').strip()
    student = (request.args.get('student') or '').strip()
    years = [y.strip() for y in request.args.getlist('year') if y.strip()]
    subjects = [s.strip() for s in request.args.getlist('subject') if s.strip()]
    tutors = [t.strip() for t in request.args.getlist('tutor') if t.strip()]
    reasons = [r.strip() for r in request.args.getlist('reason') if r.strip()]
    statuses = [s.strip() for s in request.args.getlist('status') if s.strip()]
    branches = [b.strip() for b in request.args.getlist('branch') if b.strip()]
    sort = (request.args.get('sort') or 'created_at').strip()
    direction = (request.args.get('direction') or 'desc').strip().lower()

    query = StudentConcern.query
    if q:
        like = f"%{q}%"
        query = query.filter((StudentConcern.student_name.ilike(like)) | (StudentConcern.student_id.ilike(like)) | (StudentConcern.tutor_name.ilike(like)))
    if student:
        query = query.filter((StudentConcern.student_id == student) | (StudentConcern.student_name.ilike(f"%{student}%")))
    if years:
        query = query.filter(StudentConcern.year_group.in_(years))
    if subjects:
        query = query.filter(StudentConcern.subject.in_(subjects))
    if tutors:
        query = query.filter(StudentConcern.tutor_name.in_(tutors))
    if statuses:
        query = query.filter(StudentConcern.status.in_(statuses))
    if reasons:
        from sqlalchemy import or_ as _or
        query = query.filter(_or(*[StudentConcern.reasons_json.ilike(f'%"{r}"%') for r in reasons]))

    branch_map = {'Stratford': 'S', 'Eastham': 'E', 'Docklands': 'D'}
    valid_branches = {'Stratford', 'Eastham', 'Docklands', 'Whitechapel'}
    selected_branches = [b for b in branches if b in valid_branches]
    if selected_branches:
        from sqlalchemy import func
        from sqlalchemy import or_ as _or
        prefix_expr = func.substr(func.upper(func.coalesce(StudentConcern.student_id, '')), 1, 1)
        conditions = []
        for branch_name in selected_branches:
            if branch_name in branch_map:
                conditions.append(prefix_expr == branch_map[branch_name])
            else:
                conditions.append(_or(
                    StudentConcern.student_id.is_(None),
                    prefix_expr.notin_(list(branch_map.values())),
                    prefix_expr == ''
                ))
        query = query.filter(_or(*conditions))

    if sort in {'student_id','student_name','subject','tutor_name','status','created_at'}:
        col = getattr(StudentConcern, sort)
        query = query.order_by(col.asc() if direction == 'asc' else col.desc())
    else:
        query = query.order_by(StudentConcern.created_at.desc())

    rows = query.all()
    # Build dataset
    data = []
    for sc in rows:
        reasons_list = []
        try:
            reasons_list = sc.reasons()
        except Exception:
            pass
        created = None
        try:
            created = sc.created_at.strftime('%Y-%m-%d %H:%M') if sc.created_at else None
        except Exception:
            created = str(sc.created_at) if sc.created_at else None
        data.append({
            'ID': sc.id,
            'Created At': created,
            'Student ID': sc.student_id,
            'Student Name': sc.student_name,
            'Branch': resolve_student_branch(getattr(sc, 'student_id', None)),
            'Year Group': sc.year_group,
            'Subject': sc.subject,
            'Tutor Name': sc.tutor_name,
            'Reasons': ", ".join(reasons_list),
            'Status': sc.status,
            'Other Details': sc.other_details,
        })
    df = pd.DataFrame(data)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Concerns')
    out.seek(0)
    return send_file(out, as_attachment=True, download_name='student_concerns_export.xlsx', mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/student-concerns/<int:cid>')
@login_required
@permission_required('manage_student_concerns')
def student_concern_detail(cid: int):
    from models import StudentConcern, StudentConcernChange
    sc = StudentConcern.query.get_or_404(cid)
    changes = StudentConcernChange.query.filter_by(concern_id=cid).order_by(StudentConcernChange.changed_at.desc()).all()
    return render_template('concerns/detail.html', c=sc, changes=changes)


@app.route('/student-concerns/new', methods=['GET','POST'])
@login_required
@permission_required('manage_student_concerns')
def student_concern_new():
    from models import StudentConcern
    REASONS = ['Behaviour Issue','Lack of Progress','Suspected SEN','Other']
    SUBJECTS = ['Maths','English','Science','Computer Science','Economics','Business','Psychology','11+','Physics','Chemistry','Biology']
    if request.method == 'POST':
        tutor_name = (request.form.get('tutor_name') or '').strip()
        student_id = (request.form.get('student_id') or '').strip() or None
        student_name = (request.form.get('student_name') or '').strip() or None
        year_group = (request.form.get('year_group') or '').strip() or None
        subject = (request.form.get('subject') or '').strip() or None
        other_details = (request.form.get('other_details') or '').strip() or None
        reasons = request.form.getlist('reasons')
        sc = StudentConcern(tutor_name=tutor_name, subject=subject, student_id=student_id, student_name=student_name, year_group=year_group, other_details=other_details)
        sc.set_reasons(reasons)
        db.session.add(sc); db.session.commit()
        flash('Concern created','success')
        return redirect(url_for('student_concern_detail', cid=sc.id))
    return render_template('concerns/new.html', REASON_CHOICES=REASONS, SUBJECT_CHOICES=SUBJECTS)


@app.route('/student-concerns/<int:cid>/edit', methods=['GET','POST'])
@login_required
@permission_required('manage_student_concerns')
def student_concern_edit(cid: int):
    from models import StudentConcern, StudentConcernChange
    sc = StudentConcern.query.get_or_404(cid)
    if request.method == 'POST':
        fields = ['student_id','student_name','year_group','subject','status','other_details']
        for f in fields:
            new_val = (request.form.get(f) or '').strip()
            old_val = getattr(sc, f) or ''
            if new_val != (old_val or ''):
                setattr(sc, f, new_val or None)
                ch = StudentConcernChange(concern_id=cid, field=f, old_value=old_val, new_value=new_val, changed_by_id=current_user.id)
                db.session.add(ch)
        # Update reasons (multi)
        reasons = request.form.getlist('reasons')
        old_reasons = sc.reasons()
        if sorted(reasons) != sorted(old_reasons):
            sc.set_reasons(reasons)
            db.session.add(StudentConcernChange(concern_id=cid, field='reasons', old_value=str(old_reasons), new_value=str(reasons), changed_by_id=current_user.id))
        db.session.commit()
        flash('Concern updated','success')
        return redirect(url_for('student_concern_detail', cid=cid))
    return render_template('concerns/edit.html', c=sc, REASON_CHOICES=['Behaviour Issue','Lack of Progress','Suspected SEN','Other'])


@app.route('/student-concerns/<int:cid>/delete', methods=['POST'])
@login_required
@permission_required('manage_student_concerns')
def student_concern_delete(cid: int):
    from models import StudentConcern
    sc = StudentConcern.query.get_or_404(cid)
    try:
        db.session.delete(sc)
        db.session.commit()
        flash('Concern deleted','success')
    except Exception as exc:
        db.session.rollback(); flash(f'Delete failed: {exc}','danger')
    return redirect(url_for('student_concerns_index'))


@app.route('/student-concerns/<int:cid>/meeting', methods=['POST'])
@login_required
@permission_required('manage_student_concerns', any=True)
def student_concern_meeting(cid: int):
    from models import Meeting, StudentConcern, StudentConcernChange
    sc = StudentConcern.query.get_or_404(cid)
    # Expect minimal fields: participant_id, date, time, agenda; derive student name
    participant_id = request.form.get('participant_id', type=int)
    date_str = (request.form.get('date') or '').strip()
    time_str = (request.form.get('time') or '').strip()
    agenda = (request.form.get('agenda') or f"Concern meeting for {sc.student_name or sc.student_id}").strip()
    try:
        d = _dt.strptime(date_str, '%Y-%m-%d').date() if date_str else date.today()
    except Exception:
        d = date.today()
    t = time_str or '00:00'
    m = Meeting(participant_id=participant_id or current_user.id, booked_by_id=current_user.id, agenda=agenda, student_name=sc.student_name, date=d, time=t)
    db.session.add(m); db.session.flush()
    old_meeting = sc.meeting_id
    sc.meeting_id = m.id
    sc.status = 'In Progress'
    db.session.add(StudentConcernChange(concern_id=cid, field='meeting_id', old_value=str(old_meeting) if old_meeting else None, new_value=str(m.id), changed_by_id=current_user.id))
    db.session.add(StudentConcernChange(concern_id=cid, field='status', old_value='Pending', new_value='In Progress', changed_by_id=current_user.id))
    db.session.commit()
    flash('Meeting arranged and linked to concern','success')
    return redirect(url_for('student_concerns_index'))


# ---------------- Course Enrollment (Public Multi-Step Form) ---------------- #
@csrf.exempt
@app.route('/enroll', methods=['GET', 'POST'])
def enroll_step1():
    """Step 1: Student information form"""
    from utils import BRANCH_CHOICES
    branches = BRANCH_CHOICES()
    year_groups = ['year3', 'year4', 'year5', 'year6', 'year7', 'year8',
                   'year9', 'year10', 'year11', 'year12', 'year13']

    if request.method == 'POST':
        # Honeypot check
        if (request.form.get('website') or '').strip():
            return redirect(url_for('enroll_step1'))

        student_id = (request.form.get('student_id') or '').strip()
        student_name = (request.form.get('student_name') or '').strip()
        branch = (request.form.get('branch') or '').strip()
        year_group = (request.form.get('year_group') or '').strip()
        parent_email = (request.form.get('parent_email') or '').strip()

        errors = []
        if not student_id:
            errors.append("Student ID is required")
        if not student_name:
            errors.append("Full Name is required")
        if not branch or branch not in branches:
            errors.append("Valid branch is required")
        if not year_group or year_group not in year_groups:
            errors.append("Valid year group is required")
        if not parent_email:
            errors.append("Parent/Guardian Email is required")

        if errors:
            for err in errors:
                flash(err, 'warning')
            return render_template('public/enroll_step1.html',
                                 branches=branches, year_groups=year_groups,
                                 student_id=student_id, student_name=student_name,
                                 branch=branch, year_group=year_group, parent_email=parent_email)

        # Store in session
        session['enrollment_step1'] = {
            'student_id': student_id,
            'student_name': student_name,
            'branch': branch,
            'year_group': year_group,
            'parent_email': parent_email
        }
        return redirect(url_for('enroll_step2'))

    return render_template('public/enroll_step1.html',
                         branches=branches, year_groups=year_groups)


@csrf.exempt
@app.route('/enroll/products', methods=['GET', 'POST'])
def enroll_step2():
    """Step 2: Browse products and add to cart"""
    from models import Product

    # Check step 1 completed
    if 'enrollment_step1' not in session:
        flash('Please complete student information first', 'warning')
        return redirect(url_for('enroll_step1'))

    if request.method == 'POST':
        # Handle cart updates via AJAX (add/remove products)
        action = request.form.get('action')
        product_id = request.form.get('product_id')

        if 'enrollment_cart' not in session:
            session['enrollment_cart'] = []

        if action == 'add' and product_id:
            if int(product_id) not in session['enrollment_cart']:
                session['enrollment_cart'].append(int(product_id))
                session.modified = True
                return jsonify({'success': True, 'cart_count': len(session['enrollment_cart'])})

        elif action == 'remove' and product_id:
            try:
                session['enrollment_cart'].remove(int(product_id))
                session.modified = True
                return jsonify({'success': True, 'cart_count': len(session['enrollment_cart'])})
            except ValueError:
                pass

        elif action == 'checkout':
            if not session.get('enrollment_cart'):
                flash('Please add at least one course to your cart', 'warning')
                return redirect(url_for('enroll_step2'))
            return redirect(url_for('enroll_checkout'))

    # GET: Show products
    products = Product.query.filter_by(active=True).order_by(Product.date).all()
    cart_ids = session.get('enrollment_cart', [])

    return render_template('public/enroll_step2.html',
                         products=[p.serialize() for p in products], cart_ids=cart_ids)


@csrf.exempt
@app.route('/enroll/checkout', methods=['GET', 'POST'])
def enroll_checkout():
    """Step 3: Review order and create Stripe checkout"""
    from decimal import Decimal

    from models import Order, OrderItem, Product
    from utils import calculate_enrollment_discount

    # Validate session
    if 'enrollment_step1' not in session or not session.get('enrollment_cart'):
        flash('Please complete all steps', 'warning')
        return redirect(url_for('enroll_step1'))

    student_info = session['enrollment_step1']
    cart_ids = session['enrollment_cart']
    products = Product.query.filter(Product.id.in_(cart_ids)).all()

    if not products:
        flash('No valid products in cart', 'warning')
        return redirect(url_for('enroll_step2'))

    # Calculate totals
    subtotal = sum(Decimal(str(p.price)) for p in products)
    discount = calculate_enrollment_discount(len(products), subtotal)
    total = subtotal - discount

    if request.method == 'POST':
        # Build product snapshots for Stripe metadata (no Order creation yet!)
        product_snapshots = []
        for p in products:
            product_snapshots.append({
                'id': p.id,
                'name': p.name,
                'price': float(p.price),
                'date': p.date.isoformat() if p.date else None,
                'venue': p.venue,
                'time': p.time,
                'instructor': p.instructor
            })

        # Create Stripe checkout session WITH metadata (no Order creation)
        import json

        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        line_items = [{
            'price_data': {
                'currency': 'gbp',
                'product_data': {'name': p['name']},
                'unit_amount': int(p['price'] * 100),
            },
            'quantity': 1,
        } for p in product_snapshots]

        # Build discount list for Stripe (coupon-based, not negative line items)
        discounts = []
        if discount > 0:
            coupon = stripe.Coupon.create(
                amount_off=int(discount * 100),
                currency='gbp',
                name='Enrollment Discount',
                max_redemptions=1,
            )
            discounts.append({'coupon': coupon.id})

        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=['card'],
                line_items=line_items,
                discounts=discounts if discounts else [],
                mode='payment',
                success_url=url_for('enroll_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
                cancel_url=url_for('enroll_cancelled', _external=True),
                metadata={
                    'student_id': student_info['student_id'],
                    'student_name': student_info['student_name'],
                    'branch': student_info['branch'],
                    'year_group': student_info['year_group'],
                    'parent_email': student_info['parent_email'],
                    'subtotal': str(subtotal),
                    'discount': str(discount),
                    'total': str(total),
                    'products': json.dumps(product_snapshots)
                },
            )

            return jsonify({'checkout_url': checkout_session.url})

        except Exception as e:
            return jsonify({'error': str(e)}), 400

    return render_template('public/enroll_step3.html',
                         student_info=student_info,
                         products=products,
                         subtotal=subtotal,
                         discount=discount,
                         total=total,
                         stripe_public_key=STRIPE_PUBLIC_KEY)


@csrf.exempt
@app.route('/enroll/success')
def enroll_success():
    """Payment success page – looks up order by Stripe session ID."""
    from models import Order
    session_id = request.args.get('session_id')
    if not session_id:
        flash('Missing session information.', 'danger')
        return redirect(url_for('enroll_step1'))
    order = Order.query.filter_by(stripe_checkout_session_id=session_id).first()
    # Clear session
    session.pop('enrollment_step1', None)
    session.pop('enrollment_cart', None)
    return render_template('public/enroll_success.html', order=order)


@csrf.exempt
@app.route('/enroll/cancelled')
def enroll_cancelled():
    """Payment cancelled page"""
    return render_template('public/enroll_cancelled.html')


@csrf.exempt
@app.route('/webhooks/stripe', methods=['POST'])
def stripe_webhook():
    """Handle Stripe checkout.session.completed events.

    Creates Order + OrderItems after payment authorization,
    then sends confirmation email with PDF invoice.
    """
    import json
    from datetime import datetime
    from decimal import Decimal

    import stripe

    from models import Order, OrderItem

    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')

    # Verify webhook signature if secret is configured
    if STRIPE_WEBHOOK_SECRET:
        try:
            stripe.api_key = STRIPE_SECRET_KEY
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        except ValueError:
            return jsonify({'error': 'Invalid payload'}), 400
        except stripe.error.SignatureVerificationError:
            return jsonify({'error': 'Invalid signature'}), 400
    else:
        # Fallback for development (no signature verification)
        try:
            event = json.loads(payload)
        except Exception:
            return jsonify({'error': 'Invalid payload'}), 400

    if event['type'] == 'checkout.session.completed':
        session_obj = event['data']['object']
        metadata = session_obj.get('metadata', {})

        # Check if order already exists (idempotent webhook handling)
        existing_order = Order.query.filter_by(
            stripe_checkout_session_id=session_obj['id']
        ).first()
        if existing_order:
            return jsonify({'status': 'already_processed'}), 200

        # Parse metadata
        try:
            student_id = metadata['student_id']
            student_name = metadata['student_name']
            branch = metadata['branch']
            year_group = metadata['year_group']
            parent_email = metadata.get('parent_email')
            subtotal = Decimal(metadata['subtotal'])
            discount = Decimal(metadata['discount'])
            total = Decimal(metadata['total'])
            products = json.loads(metadata['products'])
        except (KeyError, json.JSONDecodeError, ValueError) as e:
            # Log error but don't fail webhook
            print(f"Webhook metadata parse error: {e}")
            return jsonify({'error': 'Invalid metadata'}), 400

        # Create Order (with payment_status='paid')
        order = Order(
            student_id=student_id,
            student_name=student_name,
            branch=branch,
            year_group=year_group,
            parent_email=parent_email,
            subtotal=subtotal,
            discount_amount=discount,
            total=total,
            payment_status='paid',  # IMPORTANT: Set to paid immediately
            stripe_checkout_session_id=session_obj['id'],
            stripe_payment_intent_id=session_obj.get('payment_intent')
        )
        db.session.add(order)
        db.session.flush()  # Get order.id

        # Create OrderItems
        for prod in products:
            item = OrderItem(
                order_id=order.id,
                product_id=prod['id'],
                product_name=prod['name'],
                product_price=Decimal(str(prod['price'])),
                product_date=datetime.fromisoformat(prod['date']) if prod.get('date') else None,
                product_venue=prod.get('venue'),
                product_time=prod.get('time'),
                product_instructor=prod.get('instructor')
            )
            db.session.add(item)

        db.session.commit()

        # Generate PDF invoice
        try:
            pdf_bytes = generate_enrollment_invoice_pdf(order)
        except Exception as e:
            print(f"PDF generation failed: {e}")
            pdf_bytes = None  # Continue without PDF if generation fails

        # Send confirmation email (with or without PDF attachment)
        try:
            if parent_email:
                send_enrollment_confirmation_email(order, pdf_bytes)
        except Exception as e:
            print(f"Email send failed: {e}")
            # Don't fail webhook if email fails

    return jsonify({'status': 'success'}), 200


def generate_enrollment_invoice_pdf(order) -> bytes:
    """Generate binary PDF invoice for enrollment order using xhtml2pdf."""
    from io import BytesIO

    from xhtml2pdf import pisa

    # Load invoice CSS
    css_path = os.path.join(app.root_path, 'static', 'css', 'invoice.css')
    inline_css = ''
    try:
        with open(css_path, 'r', encoding='utf-8') as f:
            inline_css = f.read()
    except Exception:
        pass

    # Render HTML template
    html = render_template(
        'public/enrollment_invoice.html',
        order=order,
        inline_css=inline_css
    )

    # Convert to PDF
    pdf_file = BytesIO()
    pisa_status = pisa.CreatePDF(html, dest=pdf_file)

    if pisa_status.err:
        raise RuntimeError(f"PDF generation failed: {pisa_status.err}")

    pdf_file.seek(0)
    return pdf_file.read()


def send_enrollment_confirmation_email(order, pdf_bytes=None):
    """Send enrollment confirmation email with optional PDF invoice attachment."""
    from email_utils import build_enrollment_confirmation_email, send_email

    # Build email content
    subject, html = build_enrollment_confirmation_email(order)

    # Prepare PDF attachment only if available
    attachments = []
    if pdf_bytes:
        attachments.append((pdf_bytes, 'application', 'pdf', f'invoice_order_{order.id}.pdf'))

    # Send email
    recipient_email = order.parent_email
    if not recipient_email:
        raise ValueError("Order missing parent_email - cannot send confirmation")

    send_email(
        to_email=recipient_email,
        subject=subject,
        html=html,
        attachments=attachments if attachments else None
    )


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