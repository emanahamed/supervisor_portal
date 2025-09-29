from datetime import datetime

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
    role = db.Column(db.String(80), default='staff')  # logical application role (e.g. staff, lead, observer)
    picture = db.Column(db.String(255))  # path to profile picture relative to /static/uploads or external URL
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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

