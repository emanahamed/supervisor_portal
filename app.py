import csv
import io
import os
from datetime import date, datetime
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

from email_utils import send_email
from forms import (CycleForm, LoginForm, ObservationForm, RegisterForm,
                   StaffForm, UserProfileForm)
from models import Observation, ObservationCycle, Staff, User, db
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

@app.route('/version')
def version_history():
    # Return raw markdown (basic) – could be rendered nicer or converted to HTML client-side
    return jsonify({"version": VERSION, "changelog": get_changelog()})

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

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
                  email=form.email.data, phone=form.phone.data, branch=branches)
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
    if form.validate_on_submit():
        s.name = form.name.data
        s.department = form.department.data
        s.email = form.email.data
        s.phone = form.phone.data
        s.branch = ",".join(form.branches.data) if form.branches.data else ""
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