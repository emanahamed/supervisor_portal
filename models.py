from datetime import datetime
from uuid import uuid4

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# Roles: superadmin (can approve users), user (pending until approved)
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_superadmin = db.Column(db.Boolean, default=False)
    is_approved = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True, index=True)  # soft activation flag (login blocked if False)
    role = db.Column(db.String(80), default='staff')  # logical application role (e.g. staff, lead, observer)
    picture = db.Column(db.String(255))  # path to profile picture relative to /static/uploads or external URL
    theme_preference = db.Column(db.String(20), default='system')  # 'light' | 'dark' | 'system' (since 0.9.8)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Permission(db.Model):
    """Discrete capability that can be granted to a role or user override (since 0.9.0)."""
    key = db.Column(db.String(120), primary_key=True)
    description = db.Column(db.String(255))


class RolePermission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(80), index=True, nullable=False)
    permission_key = db.Column(db.String(120), db.ForeignKey('permission.key', ondelete='CASCADE'), index=True, nullable=False)
    __table_args__ = (db.UniqueConstraint('role', 'permission_key', name='uq_role_perm'), )


class UserPermission(db.Model):
    """Per-user override; allow=True grants, allow=False denies (overrides role)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='CASCADE'), index=True, nullable=False)
    permission_key = db.Column(db.String(120), db.ForeignKey('permission.key', ondelete='CASCADE'), index=True, nullable=False)
    allow = db.Column(db.Boolean, default=True, nullable=False)
    __table_args__ = (db.UniqueConstraint('user_id', 'permission_key', name='uq_user_perm'), )


class PermissionAudit(db.Model):
    """Audit trail for permission mutations (since 0.9.2).

    Records who changed what (actor), the target (user or role), the permission key
    and the action semantic (added, removed, allow, deny, inherit). Either role or
    target_user_id will be populated (never both None). Superadmin implicit grants
    are not logged here – only explicit configuration changes.
    """
    id = db.Column(db.Integer, primary_key=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), index=True)  # nullable for role-level changes
    role = db.Column(db.String(80), index=True)
    permission_key = db.Column(db.String(120), db.ForeignKey('permission.key', ondelete='CASCADE'), nullable=False, index=True)
    action = db.Column(db.String(40), nullable=False)  # added|removed|allow|deny|inherit
    changed_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    actor = db.relationship('User', foreign_keys=[actor_user_id])
    target_user = db.relationship('User', foreign_keys=[target_user_id])

class Staff(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    department = db.Column(db.String(120))
    email = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    branch = db.Column(db.String(255))  # CSV of branches
    active = db.Column(db.Boolean, default=True, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ObservationCycle(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    observations = db.relationship("Observation", backref="cycle", lazy=True, cascade="all, delete-orphan")

class Observation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cycle_id = db.Column(db.Integer, db.ForeignKey("observation_cycle.id"), nullable=False)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id"), nullable=False)
    observer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    score = db.Column(db.Float, nullable=False)

    staff = db.relationship("Staff", lazy=True)
    observer = db.relationship("User", lazy=True)

    detail = db.relationship("ObservationDetail", backref="observation", uselist=False, cascade="all, delete-orphan")

class ObservationDetail(db.Model):
    """Extended structured data for an observation (since 0.7.0).

    Stores grouped checklist booleans as JSON blobs plus narrative sections.
    JSON columns stored as text (SQLite) containing serialized dict/list.
    """
    id = db.Column(db.Integer, primary_key=True)
    observation_id = db.Column(db.Integer, db.ForeignKey('observation.id'), nullable=False, unique=True, index=True)
    timeslot = db.Column(db.String(20))
    weekly_test = db.Column(db.Text)        # JSON: {flag: bool, ...}
    weekly_test_comment = db.Column(db.Text)
    homework = db.Column(db.Text)           # JSON
    homework_comment = db.Column(db.Text)
    classwork = db.Column(db.Text)          # JSON
    classwork_comment = db.Column(db.Text)
    org_mgmt = db.Column(db.Text)           # JSON
    org_mgmt_comment = db.Column(db.Text)
    positives = db.Column(db.Text)          # JSON list of strings
    improvements = db.Column(db.Text)       # JSON list of strings
    target_set = db.Column(db.Text)
    actions_taken = db.Column(db.Text)
    notes = db.Column(db.Text)
    next_review_date = db.Column(db.Date)

    # ---------------- JSON helper methods ----------------
    import json as _json  # local alias

    def _parse(self, raw, default):
        try:
            return self._json.loads(raw) if raw else default
        except Exception:
            return default

    def get_checklist(self, attr):
        from checklist_utils import normalize_mapping
        raw = self._parse(getattr(self, attr), {})
        if not isinstance(raw, dict):
            return {}
        # Coerce string boolean forms before passing
        coerced = {}
        for rk, rv in raw.items():
            if isinstance(rv, str):
                lv = rv.lower()
                if lv in ('true','yes','1','y','t'): rv = True
                elif lv in ('false','no','0','n','f',''): rv = False
            coerced[rk] = rv
        return normalize_mapping(attr, coerced)

    def set_checklist(self, attr, mapping: dict):
        setattr(self, attr, self._json.dumps(mapping or {}))

    def get_list(self, attr):
        return self._parse(getattr(self, attr), [])

    def set_list(self, attr, seq):
        setattr(self, attr, self._json.dumps(seq or []))

    def serialize_all(self):
        return {
            'weekly_test': self.get_checklist('weekly_test'),
            'homework': self.get_checklist('homework'),
            'classwork': self.get_checklist('classwork'),
            'org_mgmt': self.get_checklist('org_mgmt'),
            'positives': self.get_list('positives'),
            'improvements': self.get_list('improvements'),
        }

class Availability(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, index=True)
    department = db.Column(db.String(120), index=True)
    branches = db.Column(db.String(255), index=True)  # CSV list of branches
    days = db.Column(db.Text)  # Raw textual representation of availability days/time slots
    subjects = db.Column(db.Text)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def branch_list(self):
        return [b.strip() for b in (self.branches or '').split(',') if b.strip()]


class Issue(db.Model):
    """Issue / ticket tracking model (since 0.4.0).

    Lightweight tracker for internal operational / academic issues with
    status workflow and prioritisation taxonomy.
    """
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False, index=True)
    details = db.Column(db.Text)
    status = db.Column(db.String(50), index=True)  # Pending, In Progress, Resolved
    criticality = db.Column(db.String(50), index=True)  # Minor, Significant, Medium, Critical
    urgency = db.Column(db.String(50), index=True)  # Low, Medium, High
    branch = db.Column(db.String(120), index=True)  # Single branch association for now
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    action_taken = db.Column(db.Text)  # Optional narrative of remediation / next steps

    created_by = db.relationship('User', lazy=True)

    def is_resolved(self):
        return (self.status or '').lower() == 'resolved'


class IssueChange(db.Model):
    """Audit log of field-level changes for Issues (since 0.4.1)."""
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey('issue.id'), index=True, nullable=False)
    field = db.Column(db.String(120), nullable=False)
    old_value = db.Column(db.Text)
    new_value = db.Column(db.Text)
    changed_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    changed_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    issue = db.relationship('Issue', lazy=True)
    changed_by = db.relationship('User', lazy=True)


class ErrorReport(db.Model):
        """Captured application error reports (user or system initiated) since 0.9.7.

        Two creation flows:
            1. Automatic 500 page -> user clicks 'Report this error' and we persist cached traceback details
            2. Manual top-nav 'Report Issue' form (no traceback unless provided)
        """
        id = db.Column(db.Integer, primary_key=True)
        title = db.Column(db.String(255), nullable=False, index=True)
        description = db.Column(db.Text)  # Extended description (manual) or synthesized from traceback
        reporter_comment = db.Column(db.Text)  # Optional free-text comment supplied at report time
        status = db.Column(db.String(40), default='Open', index=True)  # Open, In Progress, Resolved
        reporter_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
        created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
        updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
        resolved_at = db.Column(db.DateTime)
        resolved_by_id = db.Column(db.Integer, db.ForeignKey('user.id'))
        # Technical diagnostic fields (populated for automatic error flow)
        error_type = db.Column(db.String(200))
        error_message = db.Column(db.Text)
        traceback = db.Column(db.Text)
        request_path = db.Column(db.String(500))
        request_method = db.Column(db.String(10))
        user_agent = db.Column(db.String(400))
        screenshot_path = db.Column(db.String(400))  # relative to /static
        fingerprint = db.Column(db.String(64), index=True)  # optional grouping/future de-dupe

        reporter = db.relationship('User', foreign_keys=[reporter_id])
        resolved_by = db.relationship('User', foreign_keys=[resolved_by_id])

        def is_resolved(self):
                return (self.status or '').lower() == 'resolved'


class Meeting(db.Model):
    """Scheduled meeting between a user and (optionally) another user/staff (since 0.5.0)."""
    id = db.Column(db.Integer, primary_key=True)
    participant_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)  # Person the meeting is with
    booked_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    agenda = db.Column(db.String(500), nullable=False)
    student_name = db.Column(db.String(200))  # optional free-text
    parent_name = db.Column(db.String(200))   # optional free-text
    outcome = db.Column(db.Text)              # optional notes / outcome
    date = db.Column(db.Date, nullable=False, index=True)
    time = db.Column(db.String(10), nullable=False, index=True)  # HH:MM 24h simple string
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    participant = db.relationship('User', foreign_keys=[participant_id])
    booked_by = db.relationship('User', foreign_keys=[booked_by_id])

    def starts_at(self):
        """Return combined datetime object (naive UTC/local) for sorting if needed."""
        try:
            hh, mm = self.time.split(':')
            return datetime(self.date.year, self.date.month, self.date.day, int(hh), int(mm))
        except Exception:
            return datetime(self.date.year, self.date.month, self.date.day)


class AppointmentSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    superadmin_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    start_at = db.Column(db.DateTime, nullable=False, index=True)
    end_at = db.Column(db.DateTime, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    notes = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    superadmin = db.relationship('User', foreign_keys=[superadmin_id], lazy=True)
    created_by = db.relationship('User', foreign_keys=[created_by_id], lazy=True)

    __table_args__ = (
        db.CheckConstraint('end_at > start_at', name='ck_slot_duration_positive'),
    )

    def active_booking(self):
        return next((b for b in self.bookings if b.is_active()), None)

    def is_available(self):
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        return self.is_active and self.active_booking() is None and self.start_at >= _dt.now(_tz.utc)


class AppointmentBooking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slot_id = db.Column(db.Integer, db.ForeignKey('appointment_slot.id', ondelete='CASCADE'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='booked', index=True)
    name = db.Column(db.String(200), nullable=False)
    student_ref = db.Column(db.String(200), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    language = db.Column(db.String(5), nullable=False, default='en')
    cancel_token = db.Column(db.String(64), unique=True, nullable=False, default=lambda: uuid4().hex)
    cancel_url = db.Column(db.String(500), nullable=True)
    confirmation_sent_at = db.Column(db.DateTime)
    reminder_sent_at = db.Column(db.DateTime)
    cancelled_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    slot = db.relationship('AppointmentSlot', backref=db.backref('bookings', lazy=True, cascade='all, delete-orphan'))

    __table_args__ = (
        db.CheckConstraint("status IN ('booked','cancelled')", name='ck_booking_status'),
    )

    def is_active(self):
        return self.status == 'booked' and self.cancelled_at is None


class Todo(db.Model):
    """Task / To-Do item assigned to a user (since 0.6.0).

    Fields:
      description (short summary)
      notes (extended notes)
      actions_taken (optional narrative of progress)
      criticality (Minor / Significant / Medium / Critical)
      urgency (Low / Medium / High)
      status (Pending / Done)
      due_date (optional date)
      created_on (timestamp) auto
      created_by (FK user.id)
      assigned_to (FK user.id)
    """
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(400), nullable=False, index=True)
    notes = db.Column(db.Text)
    actions_taken = db.Column(db.Text)
    criticality = db.Column(db.String(50), index=True)
    urgency = db.Column(db.String(50), index=True)
    status = db.Column(db.String(30), index=True, default='Pending')  # Pending, Done
    due_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)
    assigned_to_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, index=True)

    created_by = db.relationship('User', foreign_keys=[created_by_id])
    assigned_to = db.relationship('User', foreign_keys=[assigned_to_id])

    def is_done(self):
        return (self.status or '').lower() == 'done'


# ---------------- Invoicing (Company & Invoice) ---------------- #
class Company(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True, index=True)
    tagline = db.Column(db.String(200))
    ofsted_reg_no = db.Column(db.String(64))
    address = db.Column(db.String(400))
    phone = db.Column(db.String(64))
    email = db.Column(db.String(255))
    website = db.Column(db.String(255))
    logo_path = db.Column(db.String(300))
    invoice_prefix = db.Column(db.String(20), default='INV-')
    next_invoice_seq = db.Column(db.Integer, default=1)
    payment_footer = db.Column(db.String(300), default='Thank you for your business')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    invoices = db.relationship('Invoice', backref='company', lazy=True)

    def generate_invoice_no(self):
        prefix = (self.invoice_prefix or 'INV-').strip()
        num = self.next_invoice_seq or 1
        return f"{prefix}{num:04d}"


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_no = db.Column(db.String(50), unique=True, nullable=False, index=True)
    company_id = db.Column(db.Integer, db.ForeignKey('company.id'), nullable=False, index=True)
    invoice_date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    due_date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    parent_name = db.Column(db.String(200), nullable=False)
    parent_phone = db.Column(db.String(64))
    parent_email = db.Column(db.String(255))
    parent_address = db.Column(db.String(400))
    child_name = db.Column(db.String(200), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    sub_total = db.Column(db.Numeric(10,2), nullable=False)
    total = db.Column(db.Numeric(10,2), nullable=False)
    status = db.Column(db.String(20), default='PAID', index=True)  # PAID / UNPAID
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def period_label(self):
        try:
            return f"{self.period_start.strftime('%d %b %Y')} – {self.period_end.strftime('%d %b %Y')}"
        except Exception:
            return ''


