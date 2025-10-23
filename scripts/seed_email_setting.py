"""One-off script to seed a default EmailSetting from legacy email constants.
Run with the project's virtualenv Python:
    .venv/bin/python scripts/seed_email_setting.py
"""
import sys
from pathlib import Path

# Ensure we're running from repo root
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

try:
    import email_utils as _eu
    from app import app
    from models import EmailSetting, db
except Exception as exc:
    print(f"Failed to import app/models/email_utils: {exc}")
    raise

with app.app_context():
    try:
        existing = EmailSetting.query.count()
        if existing:
            print(f"EmailSetting records already exist: {existing}. No action taken.")
            raise SystemExit(0)
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
        print("Inserted EmailSetting 'default-smtp' from legacy constants.")
    except SystemExit:
        pass
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        print(f"Failed to insert EmailSetting: {exc}")
        raise
