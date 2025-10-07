import os

from werkzeug.utils import secure_filename

BRANCH_CHOICES = ["Whitechapel", "East Ham", "Stratford", "Docklands"]

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
        items = [i for i in items if i in BRANCH_CHOICES]
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
