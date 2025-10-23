import json as _json
import os

from werkzeug.utils import secure_filename

from models import Branch, Setting, db


def BRANCH_CHOICES():
    """Return list of branch names (ordered) from DB; fall back to hardcoded list if DB not accessible."""
    try:
        rows = Branch.query.order_by(Branch.name).all()
        if rows:
            return [r.name for r in rows]
    except Exception:
        pass
    return ["Whitechapel", "East Ham", "Stratford", "Docklands"]

def allowed_file(filename):
    return "." in filename and os.path.splitext(filename)[1].lower() in {".xlsx", ".xls", ".csv"}

def normalize_staff_dataframe(df):
    # try to find likely columns (case-insensitive, tolerate spaces/underscores)
    def pick(colnames):
        lower = {c.lower().replace(" ", "").replace("_",""): c for c in df.columns}
        for name in colnames:
            key = name.lower().replace(" ","").replace("_","")
            if key in lower:
                return lower[key]
        return None

    mapping = {
        "name": pick(["name","full name","staff name","tutor name"]),
        "department": pick(["department","dept","subject area","role"]),
        "email": pick(["email","email address","e-mail"]),
        "phone": pick(["phone","phone number","mobile","contact"]),
        "branch": pick(["branch","branches","location"]),
    }
    out = {}
    for k, source in mapping.items():
        if source and source in df.columns:
            out[k] = df[source]
        else:
            out[k] = ""
    out_df = df.assign(**out)[["name","department","email","phone","branch"]].copy()
    # normalise branch to CSV list of allowed choices
    def clean_branch(x):
        if not isinstance(x, str): return ""
        items = [i.strip() for i in str(x).replace(";",",").split(",") if i.strip()]
        items = [i for i in items if i in BRANCH_CHOICES()]
        return ",".join(sorted(set(items)))
    out_df["branch"] = out_df["branch"].apply(clean_branch)
    return out_df

# ------------- Student Utilities ------------- #
import re as _re


def parse_preferred_contact(raw: str | None):
    """Extract (email, phone) tuple from a combined preferred contact string.

    Heuristics: first RFC-like email; first phone-like digit sequence (7+ digits,
    allows +, spaces, dashes, parentheses). Returns (email, phone) or (None,None).
    """
    if not raw:
        return (None, None)
    raw = raw.strip()
    email = None; phone = None
    email_match = _re.search(r'[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}', raw, _re.I)
    if email_match:
        email = email_match.group(0)
    phone_match = _re.search(r'(\+?\d[\d \-()]{6,}\d)', raw)
    if phone_match:
        phone = _re.sub(r'[^0-9+]', '', phone_match.group(1))
    return (email, phone)

# ------------- Dynamic Settings Helpers ------------- #

def get_setting(key: str, default=None, *, as_json: bool = False):
    s = Setting.query.filter_by(key=key).first()
    if not s:
        return default
    if as_json:
        try:
            return _json.loads(s.value) if s.value is not None else default
        except Exception:
            return default
    return s.value if s.value is not None else default

def set_setting(key: str, value, *, as_json: bool = False) -> None:
    if as_json:
        stored = _json.dumps(value)
    else:
        stored = str(value) if value is not None else None
    s = Setting.query.filter_by(key=key).first()
    if not s:
        s = Setting(key=key, value=stored)
        db.session.add(s)
    else:
        s.value = stored
    db.session.commit()


def ensure_form_branch_choices(form):
    """Recursively walk a WTForms form/formfield and populate any SelectField
    or SelectMultipleField named 'branch' or 'branches' with choices from the
    DB-driven BRANCH_CHOICES(). This is a safe no-op if fields are already set.
    Views can call this as a small safety check before rendering forms.
    """
    try:
        choices = [(b, b) for b in BRANCH_CHOICES()]
    except Exception:
        choices = [(b, b) for b in ["Whitechapel", "East Ham", "Stratford", "Docklands"]]

    # Work recursively through attributes that are FormFields or FieldList
    from wtforms import FieldList, FormField, SelectField, SelectMultipleField

    def _fill(f):
        # If form provides fields via _fields dict
        try:
            fields = getattr(f, '_fields', {})
        except Exception:
            fields = {}
        for name, fld in fields.items():
            # Exact name matches
            if name in ('branch', 'branches') and isinstance(fld, (SelectField, SelectMultipleField)):
                if not getattr(fld, 'choices', None):
                    fld.choices = choices
            # Recurse into nested FormField
            if isinstance(fld, FormField):
                _fill(fld.form)
            # FieldList of FormField
            if isinstance(fld, FieldList):
                # FieldList.entries may be empty if no entries; if entries exist, recurse
                for entry in getattr(fld, 'entries', []):
                    try:
                        _fill(entry.form)
                    except Exception:
                        pass

    try:
        _fill(form)
    except Exception:
        # Defensive: never raise from this helper
        return


# ------------- Schedule Message Parsing (Student timetable message) ------------- #

def parse_schedule_message(raw: str, *, class_start_date: str | None = None) -> dict:
    """Parse a raw pasted student schedule block into components.

    Expected input example (tabs or multiple spaces as delimiters):

        S397Student Photo\tFairuz Faiza Tulip\tOnline\t11\t07429324173
        fahmidafaizaprapti@gmail.com\tFlat 31
        E6 6JE
        Subject\tDay\tTime\tTutor
        English\tWednesday\t05:00 PM - 07:00 PM\tTahira Yasmin
        Maths\tSaturday\t09:00 AM - 11:00 AM\tAsifur Rahman
        Science\tSaturday\t11:10 AM - 01:10 PM\tPatrizia Hossain

    Returns dict: {student_id, student_name, entries:[{subject,day,time,tutor}], message:str}
    Robust to: tabs or 2+ spaces, stray blank lines, missing columns.
    """
    lines = [l.rstrip() for l in (raw or '').splitlines() if l.strip()]
    student_id = None
    student_name = None
    entries = []
    header_idx = None
    # Identify header line
    for i, line in enumerate(lines):
        low = line.lower()
        if 'subject' in low and 'day' in low and 'time' in low and 'tutor' in low:
            header_idx = i
            break
    # Extract student meta from lines before header
    if header_idx is not None:
        pre = lines[:header_idx]
        if pre:
            first = pre[0]
            import re as _re
            m = _re.match(r'(s\d+)', first.lower())
            if m:
                student_id = m.group(1).upper()
            # Try tab or multi-space split
            parts = [p for p in _split_row(first) if p]
            if len(parts) >= 2:
                # Name likely second column; guard against placeholder words
                candidate = parts[1].strip()
                if candidate and not candidate.lower().startswith('online'):
                    student_name = candidate
        # If not found, attempt second line
        if (not student_name) and len(pre) > 1:
            parts2 = [p for p in _split_row(pre[1]) if p]
            if parts2 and '@' not in parts2[0]:  # avoid picking email line
                student_name = parts2[0].strip()
    # Parse schedule entries
    if header_idx is not None:
        for line in lines[header_idx+1:]:
            # Skip if looks like email/address residue
            low = line.lower()
            if 'subject' in low and 'day' in low and 'time' in low:
                continue
            parts = [p.strip() for p in _split_row(line) if p.strip()]
            if len(parts) < 3:
                continue
            # Heuristic mapping: subject, day, time, tutor(optional)
            subject = parts[0]
            day = parts[1]
            time = parts[2]
            tutor = parts[3] if len(parts) > 3 else ''
            # Basic validation for time pattern
            import re as _re
            if not _re.search(r'\d{1,2}:\d{2}\s*(AM|PM).*?-.*?\d{1,2}:\d{2}\s*(AM|PM)', time, _re.I):
                # Maybe columns shifted (time last)
                for p in parts[2:]:
                    if _re.search(r'\d{1,2}:\d{2}\s*(AM|PM).*?-.*?\d{1,2}:\d{2}\s*(AM|PM)', p, _re.I):
                        time = p
                        break
            entries.append({'subject': subject, 'day': day, 'time': time, 'tutor': tutor})
    # Fallbacks
    if not student_id:
        student_id = 'N/A'
    if not student_name:
        student_name = 'Student'
    # Build message
    lines_out = [
        'Welcome to Excel Tutors. Thank you for choosing us for your child\'s tutoring.',
        f'Student ID: {student_id},',
        f'Student Name: {student_name}',
        '',
        'Your class time is as follows:',
        ''
    ]
    if class_start_date:
        lines_out.insert(4, f'Class Start Date: {class_start_date}')
    for e in entries:
        lines_out.append(f'Subject: {e["subject"]}')
        lines_out.append(f'Day: {e["day"]}')
        lines_out.append(f'Time: {e["time"]}')
        lines_out.append('')
    lines_out.append("Please send your child according to this time only as we won't be sending class reminders. Also, please send them on time otherwise they will get 15 min detention after their lesson.")
    message = '\n'.join(lines_out).strip() + '\n'
    return {
        'student_id': student_id,
        'student_name': student_name,
        'entries': entries,
        'message': message,
    }

def _split_row(line: str):  # helper kept non-public
    import re as _re
    if '\t' in line:
        return line.split('\t')
    # Split on 2+ spaces, keep single spaces inside tokens
    return _re.split(r' {2,}', line.strip())
