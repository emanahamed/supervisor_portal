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
