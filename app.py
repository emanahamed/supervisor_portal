import base64
import csv
import io
import json
import os
from datetime import date, datetime, timezone
from uuid import uuid4

import pandas as pd
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_file, url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import or_, text
from werkzeug.security import check_password_hash, generate_password_hash

from attendance_utils import (combine_all_sheets, compute_date_range,
                              export_with_custom_header_to_bytes)
from email_utils import build_task_notification_email, send_email
from forms import (AvailabilityForm, CycleForm, IssueForm, LoginForm,
                   MeetingForm, ObservationForm, RegisterForm, StaffForm,
                   TodoForm, UserProfileForm)
from models import (Availability, Issue, IssueChange, Meeting, Observation,
                    ObservationCycle, Staff, Todo, User, db)
from utils import BRANCH_CHOICES, allowed_file, normalize_staff_dataframe
from version_info import VERSION, get_changelog

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

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

ts = URLSafeTimedSerializer(SECRET_KEY)

@app.context_processor
def inject_version():
    return {"APP_VERSION": VERSION}

@app.route('/version-history')
def version_history():
    return jsonify({"version": VERSION, "changelog": get_changelog()})

@app.route('/api/version')
def api_version():
    full = get_changelog()
    current_block = ''
    if full:
        lines = full.splitlines()
        capture = False
        for line in lines:
            if line.startswith(f'## {VERSION} '):
                capture = True
                current_block += line + '\n'
                continue
            if capture and line.startswith('## '):
                break
            if capture:
                current_block += line + '\n'
    return jsonify({
        'version': VERSION,
        'changelog_current': current_block.strip(),
        'changelog_full': full
    })

# ---------------- Attendance Fix Page ---------------- #
@app.route('/attendance/fix')
@login_required
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
    # Lightweight SQLite schema patch for new user columns (role, picture)
    try:
        with db.engine.connect() as conn:
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(user)"))}
            if 'role' not in cols:
                conn.execute(text("ALTER TABLE user ADD COLUMN role VARCHAR(80)"))
            if 'picture' not in cols:
                conn.execute(text("ALTER TABLE user ADD COLUMN picture VARCHAR(255)"))
    except Exception:
        # Silent fail; if this is not SQLite or table absent yet, it will be handled later
        pass
    # Ensure seeded superadmin exists and is flagged correctly
    sa = User.query.filter_by(email="superadmin@exceltutors.org.uk").first()
    if not sa:
        sa = User(
            name="Super Admin",
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
    # Simple backfill for any existing users missing new columns (SQLite tolerant)
    try:
        users_no_role = User.query.filter(User.role.is_(None)).all()
        altered = False
        for u in users_no_role:
            u.role = 'staff'
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
    except Exception:
        pass

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
        subq = db.session.query(Observation.staff_id).filter(Observation.cycle_id.in_(selected_cycle_ids)).distinct().subquery()
    else:
        subq = db.session.query(Observation.staff_id).distinct().subquery()
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
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("auth/approve.html", users=users)

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
    if role not in ['staff','observer','lead','superadmin']:
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

@app.route('/profile', methods=['GET','POST'])
@login_required
def profile():
    form = UserProfileForm()
    user = current_user
    if request.method == 'GET':
        form.name.data = user.name
        form.email.data = user.email
        form.role.data = user.role or 'staff'
        form.is_approved.data = user.is_approved
        form.is_superadmin.data = user.is_superadmin
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
        # Password update if provided
        if form.password.data:
            user.password_hash = generate_password_hash(form.password.data)
        db.session.commit()
        flash('Profile updated', 'success')
        return redirect(url_for('profile'))
    return render_template('auth/profile.html', form=form, user=user)

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
            # keep_default_na=False preserves empty strings instead of converting to NaN
            import_df = pd.read_csv(temp_path, keep_default_na=False)

            def clean(val):
                """Convert a cell value to a stripped string, treating NaN/None as empty string."""
                if val is None:
                    return ''
                # pandas may still give us float('nan') if not using keep_default_na, but guard anyway
                try:
                    import math
                    if isinstance(val, float) and math.isnan(val):
                        return ''
                except Exception:
                    pass
                return str(val).strip()

            added = updated = skipped = 0
            for _, row in import_df.iterrows():
                name = clean(row.get('name'))
                if not name:
                    skipped += 1
                    continue
                email_raw = clean(row.get('email'))
                email = email_raw.lower() if email_raw else None
                dept = clean(row.get('department')) or None
                phone = clean(row.get('phone')) or None
                branch_val = clean(row.get('branch'))

                existing = None
                if email:
                    existing = Staff.query.filter_by(email=email).first()
                if existing:
                    existing.name = name
                    existing.department = dept
                    existing.phone = phone
                    existing.branch = branch_val
                    updated += 1
                else:
                    s = Staff(name=name,
                              department=dept,
                              email=email,
                              phone=phone,
                              branch=branch_val)
                    db.session.add(s)
                    added += 1
            db.session.commit()
            os.remove(temp_path)
            flash(f"Import complete: {added} added, {updated} updated, {skipped} skipped.", "success")
            return redirect(url_for('staff_index'))
        except Exception as e:
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
        flash('Issue created','success')
        return redirect(url_for('issues_index'))
    # Preselect defaults
    if request.method == 'GET':
        form.status.data = 'Pending'
        form.criticality.data = 'Minor'
        form.urgency.data = 'Medium'
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
        flash('Issue updated','success')
        return redirect(url_for('issues_index'))
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
        if k.startswith(prefix + '_') and not k.endswith('_comment'):
            key = k[plen:]
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
    def load_json(text):
        try: return json.loads(text) if text else {}
        except Exception: return {}
    def load_list(text):
        try: return json.loads(text) if text else []
        except Exception: return []
    cycles = ObservationCycle.query.order_by(ObservationCycle.start_date.desc().nullslast()).all()
    staff = Staff.query.order_by(Staff.name.asc()).all()
    return render_template('observations/extended_form.html', obs=obs, detail=detail, staff=staff, cycles=cycles,
                           today=date.today(), weekly_test_data=load_json(detail.weekly_test), homework_data=load_json(detail.homework),
                           classwork_data=load_json(detail.classwork), org_mgmt_data=load_json(detail.org_mgmt),
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
    # Use a PDF-friendly simplified template
    html = render_template('observations/report_pdf.html', obs=obs, detail=detail, data=detail.serialize_all(), logo_data_uri=logo_data_uri, generated_at=datetime.now(timezone.utc))
    try:
        from io import BytesIO

        from xhtml2pdf import pisa
        pdf_io = BytesIO(); pisa.CreatePDF(html, dest=pdf_io); pdf_io.seek(0)
        return send_file(pdf_io, mimetype='application/pdf', as_attachment=True, download_name=f'observation_{oid}.pdf')
    except Exception:
        return html
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
    # Use dedicated email template (richer styling) and PDF template for attachment
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

        # Render PDF-friendly template separately for attachment
        pdf_html = render_template('observations/report_pdf.html', obs=obs, detail=detail, data=detail.serialize_all(), logo_data_uri=logo_data_uri, generated_at=datetime.now(timezone.utc))
        pdf_io = BytesIO(); pisa.CreatePDF(pdf_html, dest=pdf_io); pdf_io.seek(0); pdf_bytes = pdf_io.read()
    except Exception:
        pdf_bytes = None
    body = f"<p>Dear {obs.staff.name},</p><p>Please find your observation summary below:</p>" + email_html + "<p>Best regards,<br>Excel Tutors</p>"
    try:
        if pdf_bytes:
            import smtplib
            from email.message import EmailMessage

            from email_utils import (FROM_EMAIL, FROM_NAME, SMTP_HOST,
                                     SMTP_PASSWORD, SMTP_PORT, SMTP_USERNAME)
            msg = EmailMessage(); msg['Subject'] = f"Observation Report - {obs.staff.name} ({obs.date})"; msg['From'] = f"{FROM_NAME} <{FROM_EMAIL}>"; msg['To'] = tutor_email
            msg.set_content('HTML observation report attached.')
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

# ---------------- Error Handlers ----------------
@app.errorhandler(403)
def forbidden(e):  # noqa: D401
    return render_template("errors/403.html"), 403

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
    return render_template("errors/500.html"), 500

if __name__ == "__main__":
    # Centralised static configuration (edit config.py to change)
    try:
        from config import RUN_DEBUG, RUN_HOST, RUN_PORT
    except Exception:
        # Fallbacks if config.py missing or incomplete
        RUN_HOST, RUN_PORT, RUN_DEBUG = "127.0.0.1", 5000, False
    app.run(host=RUN_HOST, port=RUN_PORT, debug=RUN_DEBUG)