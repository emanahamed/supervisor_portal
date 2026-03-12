import os
import smtplib
from datetime import datetime
from datetime import datetime as _dt
from decimal import Decimal
from email.message import EmailMessage
from typing import Optional, Sequence, Tuple

from models import EmailLog, EmailSetting, db

# Legacy default constants (used as fallback if DB is unavailable)
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USERNAME = "management@exceltutors.org.uk"
SMTP_PASSWORD = "jrrh axpx sssm pzvc"
FROM_NAME     = "Excel Tutors"
FROM_EMAIL    = "management@exceltutors.org.uk"
REPLY_TO      = "management@exceltutors.org.uk"


def _get_active_email_setting(name: Optional[str] = None) -> Optional[EmailSetting]:
    """Return the named EmailSetting or the first active one.

    Returns None if the DB is not available or no active settings exist.
    """
    try:
        if name:
            # Try exact match first
            s = EmailSetting.query.filter_by(name=name).first()
            if s:
                return s
            # Fallback to case-insensitive match on name
            try:
                from sqlalchemy import func
                s = EmailSetting.query.filter(func.lower(EmailSetting.name) == name.lower()).first()
                if s:
                    return s
            except Exception:
                pass
        # No specific name provided or not found: return most recently updated active setting
        return EmailSetting.query.filter_by(is_active=True).order_by(EmailSetting.updated_at.desc()).first()
    except Exception:
        return None


def _populate_msg_headers(msg: EmailMessage, subject: str, to_email: str, setting: Optional[EmailSetting] = None, *, cc: Optional[Sequence[str]] = None):
    if setting and setting.sender_email:
        from_addr = setting.sender_email
        from_name = setting.sender_name or ''
        msg['From'] = f"{from_name} <{from_addr}>" if from_name else from_addr
    else:
        msg['From'] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg['To'] = to_email
    if cc:
        cc_values = [c.strip() for c in cc if c and c.strip()]
        if cc_values:
            msg['Cc'] = ', '.join(dict.fromkeys(cc_values))
    msg['Subject'] = subject
    if setting and setting.sender_email:
        msg['Reply-To'] = setting.sender_email
    else:
        if REPLY_TO:
            msg['Reply-To'] = REPLY_TO


_LOGO_CACHE: Optional[bytes] = None

def _load_logo_bytes() -> Optional[bytes]:
    """Load logo bytes from static folder once; return None if not available."""
    global _LOGO_CACHE
    if _LOGO_CACHE is not None:
        return _LOGO_CACHE
    try:
        base_dir = os.path.dirname(__file__)
        # Default project logo path
        logo_path = os.path.join(base_dir, 'static', 'img', 'excel tutors logo 2023.png')
        with open(logo_path, 'rb') as f:
            _LOGO_CACHE = f.read()
            return _LOGO_CACHE
    except Exception:
        _LOGO_CACHE = None
        return None


def send_email(to_email: str, subject: str, html: str, *, setting_name: Optional[str] = None, attachments: Optional[list] = None, cc: Optional[Sequence[str]] = None) -> None:
    """Send an HTML email using DB-backed EmailSetting when available.

    attachments: list of tuples (bytes, maintype, subtype, filename)
    """
    setting = _get_active_email_setting(name=setting_name)
    cc_values = [c.strip() for c in (cc or []) if c and c.strip()]
    msg = EmailMessage()
    _populate_msg_headers(msg, subject, to_email, setting, cc=cc_values)
    msg.set_content("This email contains HTML content. Please view in an HTML-capable client.")
    msg.add_alternative(html, subtype='html')

    # Embed logo for branded emails that reference cid:et-logo
    try:
        html_part = None
        for part in msg.iter_parts():
            if part.get_content_type() == 'text/html':
                html_part = part
                break
        if html_part and (('cid:et-logo' in html) or ('cid:et-logo' in str(html_part.get_content()))):
            logo_bytes = _load_logo_bytes()
            if logo_bytes:
                html_part.add_related(logo_bytes, maintype='image', subtype='png', cid='<et-logo>')
    except Exception:
        pass

    # Attach files if provided
    if attachments:
        for data, maintype, subtype, fname in attachments:
            try:
                msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
            except Exception:
                continue

    # If we have a DB-backed SMTP config, use it; otherwise fall back to constants
    host = SMTP_HOST
    port = SMTP_PORT
    username = SMTP_USERNAME
    password = SMTP_PASSWORD
    use_tls = True
    use_ssl = False
    if setting:
        ci = setting.connection_info()
        host = ci.get('host') or host
        port = ci.get('port') or port
        username = ci.get('username') or username
        password = ci.get('password') or password
        use_tls = ci.get('use_tls', True)
        use_ssl = ci.get('use_ssl', False)
    # Create a pending log record before attempting to send. If DB is
    # unavailable, continue without logging.
    log = None
    try:
        log = EmailLog(
            to_email=to_email,
            subject=subject,
            body_snippet=(html[:800] if html else None),
            html=html,
            status='pending',
            provider=(setting.provider if setting else None),
            email_setting_id=(setting.id if setting else None),
            attachments_count=(len(attachments) if attachments else 0),
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass

    # Attempt to send and update the log accordingly.
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port) as server:
                if username and password:
                    server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port) as server:
                if use_tls:
                    try:
                        server.starttls()
                    except Exception:
                        pass
                if username and password:
                    try:
                        server.login(username, password)
                    except Exception:
                        pass
                server.send_message(msg)
        # Mark as sent
        try:
            if log:
                log.status = 'sent'
                log.sent_at = _dt.utcnow()
                db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
    except Exception as exc:  # pragma: no cover - environment specific
        # Mark log as failed and surface the exception
        try:
            if log:
                log.status = 'failed'
                log.error_message = str(exc)
                log.sent_at = _dt.utcnow()
                db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
        raise


def send_recruitment_email(to_email: str, subject: str, html: str, *, attachments: Optional[list] = None, cc: Optional[Sequence[str]] = None) -> None:
    """Shorthand for sending via the 'recruitment' EmailSetting.

    Ensures job-related communications use the Recruitment mailbox identity
    (from name/address) configured in Email Settings.
    """
    send_email(to_email, subject, html, setting_name='recruitment', attachments=attachments, cc=cc)


def send_operations_email(to_email: str, subject: str, html: str, *, attachments: Optional[list] = None, cc: Optional[Sequence[str]] = None) -> None:
    """Shorthand for sending via the 'operations' EmailSetting.

    Use this for internal operational notifications like book orders, stock,
    or printing tasks. Configure an EmailSetting named 'operations' to control
    the From identity and SMTP credentials.
    """
    send_email(to_email, subject, html, setting_name='operations', attachments=attachments, cc=cc)


def build_interview_invitation_email(applicant, slots: list[tuple[str, list[str]]], confirm_url: Optional[str] = None) -> Tuple[str, str]:
    """Build the interview invitation email body with grouped date/time slots.

    slots: list of (day_heading, ["Friday, 24th October, 2025 at 10:30 AM", ...])
    """
    subject = "Invitation to interview – Excel Tutors"
    intro = f"Dear {applicant.first_name},<br/><br/>Thank you for your recent application to Excel Tutors.<br/><br/>We have now progressed to the subsequent stage of our recruitment process, having reviewed and shortlisted candidates for the position.<br/><br/>Consequently, we would like to extend an invitation to you for an interview.<br/><br/>Could you please respond with your availability during one of the following time slots?"
    lines = []
    for day, options in slots:
        for i, opt in enumerate(options):
            # Keep a blank line between day blocks (like the provided example)
            if i == 0 and lines:
                lines.append("&nbsp;")
            lines.append(opt)
    details = (
        "<br/><br/>This is an excellent opportunity for you to meet with our director and head of department, as well as for us to get better acquainted with you."
        "<br/><br/>The interview will last approximately 1 hour and will encompass a basic test on the subject you have elected to teach at Excel Tutors. Please revise the subject you want to teach at GCSE Level."
        "<br/><br/>The interviews will take place at 161-163 Commercial Road, London E1 2DA."
        "<br/><br/>Please ensure you bring the most current version of your CV."
    )
    footer = (
        "<br/><br/>Best Regards<br/><br/>"
        "Recruitment Administrator<br/>Excel Tutors<br/>161-163 Commercial Road<br/>London, E1 2DA<br/>"
        "Tel: 0207 0011 411<br/>"
        "Recruitment Enquiries: recruitment@exceltutors.org.uk<br/>"
        "Exam Enquiries: exams@exceltutors.org.uk<br/>"
        "General Enquiries: info@exceltutors.org.uk<br/>"
        "www.exceltutors.org.uk"
    )
    cta = ''
    if confirm_url:
        cta = (
            "<div style='margin:18px 0;text-align:center'>"
            f"<a href=\"{confirm_url}\" style='display:inline-block;background:#2563eb;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:600'>Confirm your interview</a>"
            "</div>"
        )
    body_inner = [
        f"<p style='margin:0 0 8px 0;font-size:14px;color:#334155;'>{intro}</p>",
        "<div style='font-family:monospace; font-size:13px; line-height:1.6; color:#0f172a;'>" + "<br/>".join(lines) + "</div>",
        cta,
        f"<div style='font-size:13px;color:#334155;'>{details}</div>",
        f"<div style='font-size:13px;color:#334155;'>{footer}</div>",
    ]
    html = _render_email_shell('Interview invitation', 'Interview invitation', '', ''.join(body_inner))
    return (subject, html)


def build_interview_reminder_email(applicant, when_dt) -> Tuple[str, str]:
    """Reminder email 12 hours before an interview."""
    try:
        when_label = when_dt.strftime('%A, %d %B %Y at %I:%M %p').lstrip('0')
    except Exception:
        when_label = str(when_dt)
    subject = "Reminder: Your interview is in 12 hours – Excel Tutors"
    intro = (
        f"Dear {applicant.first_name},<br/><br/>"
        f"This is a friendly reminder of your interview scheduled for {when_label}."
    )
    details = (
        "<br/><br/>The interview will take place at 161-163 Commercial Road, London E1 2DA."
        "<br/><br/>Please arrive 5-10 minutes early and bring a copy of your CV."
    )
    footer = (
        "<br/><br/>Best Regards<br/><br/>Recruitment Administrator<br/>Excel Tutors"
    )
    body_inner = [
        f"<p style='margin:0 0 8px 0;font-size:14px;color:#334155;'>{intro}</p>",
        f"<div style='font-size:13px;color:#334155;'>{details}</div>",
        f"<div style='font-size:13px;color:#334155;'>{footer}</div>",
    ]
    html = _render_email_shell('Interview reminder', 'Interview reminder', '', ''.join(body_inner))
    return (subject, html)


def build_interview_confirmation_email(applicant, *, reschedule_url: str, cancel_url: str) -> Tuple[str, str]:
    """Build a branded email for interview confirmation.

    Uses applicant.first_name and applicant.interview_label.
    """
    when_label = getattr(applicant, 'interview_label', None) or ''
    subject = f"Interview confirmed – {when_label}" if when_label else "Interview confirmed – Excel Tutors"
    intro = f"Dear {getattr(applicant, 'first_name', 'there')},"
    details = (
        f"<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Your interview is confirmed for <strong>{when_label}</strong>.</p>"
        "<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Location: 161-163 Commercial Road, London E1 2DA.</p>"
        "<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Please arrive 5-10 minutes early and bring a copy of your CV.</p>"
        f"<p style='margin:0 0 0;font-size:14px;color:#334155;'>If you need to make changes you can <a href='{reschedule_url}'>reschedule</a> or <a href='{cancel_url}'>cancel</a> your interview.</p>"
    )
    html = _render_email_shell('Interview confirmed', 'Interview confirmed', intro, details)
    return subject, html


def build_interview_rescheduled_email(applicant, *, reschedule_url: str, cancel_url: str) -> Tuple[str, str]:
    """Build a branded email for interview rescheduling by admin.

    Uses applicant.first_name and applicant.interview_label.
    """
    when_label = getattr(applicant, 'interview_label', None) or ''
    subject = f"Interview rescheduled – {when_label}" if when_label else "Interview rescheduled – Excel Tutors"
    intro = f"Dear {getattr(applicant, 'first_name', 'there')},"
    details = (
        f"<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Your interview has been rescheduled to <strong>{when_label}</strong>.</p>"
        "<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Location: 161-163 Commercial Road, London E1 2DA.</p>"
        f"<p style='margin:0 0 0;font-size:14px;color:#334155;'>If you need to make changes you can <a href='{reschedule_url}'>reschedule</a> or <a href='{cancel_url}'>cancel</a>.</p>"
    )
    html = _render_email_shell('Interview rescheduled', 'Interview rescheduled', intro, details)
    return subject, html


def build_interview_cancelled_email(applicant, *, reschedule_url: str) -> Tuple[str, str]:
    """Build a branded email for interview cancellation by admin."""
    subject = "Interview cancelled – Excel Tutors"
    intro = f"Dear {getattr(applicant, 'first_name', 'there')},"
    details = (
        "<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Your upcoming interview has been cancelled by our recruitment team.</p>"
        f"<p style='margin:0 0 0;font-size:14px;color:#334155;'>You can <a href='{reschedule_url}'>choose a new time</a> whenever convenient.</p>"
    )
    html = _render_email_shell('Interview cancelled', 'Interview cancelled', intro, details)
    return subject, html


def send_with_template(template_key: str, ctx: dict, *, to_email: Optional[str] = None, fallback=None, attachments: Optional[list] = None) -> None:
    """Render an EmailTemplate by key and send it.

    - template_key: EmailTemplate.key to find (only active templates considered)
    - ctx: mapping used for Python str.format rendering
    - to_email: optional override recipient; if not provided will use ctx.get('to_email')
    - fallback: optional callable returning (subject, html) when template missing or rendering fails
    - attachments: optional attachments list forwarded to send_email

    This helper is defensive: if DB is unavailable or template rendering fails it will
    fall back to the provided fallback or raise the original exception depending on call.
    """
    try:
        # Import here to avoid circular imports at module import time
        from models import EmailTemplate
        et = EmailTemplate.query.filter_by(key=template_key, is_active=True).first()
    except Exception:
        et = None

    # Determine recipient
    recipient = to_email or (ctx.get('to_email') if isinstance(ctx, dict) else None)

    if et:
        try:
            subject = et.render_subject(**ctx)
            html = et.render_html(**ctx)
            # If template specifies a setting, use it by name
            setting_name = None
            if et.email_setting and et.email_setting.name:
                setting_name = et.email_setting.name
            # Allow template-level sender overrides by injecting headers via send_email's behaviour
            # send_email populates From/Reply-To from the EmailSetting; for simple overrides we rely on the EmailSetting.
            send_email(recipient or ctx.get('to_email'), subject, html, setting_name=setting_name, attachments=attachments)
            return
        except Exception:
            # If rendering or send failed, fall through to fallback
            pass

    # Fallback path: call provided fallback to build subject/html, else raise
    if fallback:
        subj_html = None
        try:
            subj_html = fallback()
        except Exception:
            subj_html = None
        if subj_html and isinstance(subj_html, tuple) and len(subj_html) == 2:
            subj, html = subj_html
            send_email(recipient or ctx.get('to_email'), subj, html, attachments=attachments)
            return
    # If we reach here, no template and no usable fallback; raise an informative error
    raise RuntimeError(f"Email template '{template_key}' not found or failed to render and no fallback provided")

def build_task_notification_email(task, created_by, assigned_to):
        """Return branded HTML for task creation notification."""
        # Basic inline-styled responsive friendly email (no external CSS for broad client support)
        crit_color = {
                'Critical': '#dc2626',
                'Medium': '#d97706',
                'Significant': '#7c3aed',
                'Minor': '#64748b'
        }.get((task.criticality or '').title(), '#334155')
        urg_color = {
                'High': '#dc2626',
                'Medium': '#d97706',
                'Low': '#0d9488'
        }.get((task.urgency or '').title(), '#334155')
        status_color = '#2563eb' if (task.status or '').lower() == 'pending' else '#059669'
        def pill(text, bg):
                return f"<span style='display:inline-block;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600;background:{bg}10;color:{bg};border:1px solid {bg}33;margin-right:6px;'>" \
                             f"{text}</span>"
        due_html = f"<strong>{task.due_date.strftime('%Y-%m-%d')}</strong>" if task.due_date else '<em>No due date</em>'
        actions_html = (task.actions_taken or '').replace('\n','<br/>') or '<em>None yet</em>'
        notes_html = (task.notes or '').replace('\n','<br/>') or '<em>None</em>'
        description_html = (task.description or '').replace('\n','<br/>')
        return f"""
<!DOCTYPE html>
<html lang='en'>
<head><meta charset='utf-8'><title>New Task Assigned</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;">
    <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#f1f5f9;padding:24px 0;'>
        <tr><td align='center'>
            <table role='presentation' width='600' cellpadding='0' cellspacing='0' style='background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0;'>
                <tr>
                    <td style='background:#ffffff;padding:20px 28px;text-align:center;'>
                        <img src='cid:et-logo' alt='' style='display:block;margin:0 auto;max-height:40px;width:auto;'>
                    </td>
                </tr>
                <tr>
                    <td style='padding:28px;'>
                        <p style='margin:0 0 12px;font-size:13px;color:#64748b;'>A new task has been created by {created_by.name}.</p>
                        <h2 style='margin:0 0 12px;font-size:18px;color:#0f172a;'>{description_html}</h2>
                        <p style='margin:0 0 18px;font-size:14px;color:#475569;'>You have been assigned a new task in the Excel Tutors portal.</p>
                        <div style='margin-bottom:18px;'>
                            {pill(task.status or 'Pending', status_color)}
                            {pill(task.criticality or 'N/A', crit_color)}
                            {pill(task.urgency or 'N/A', urg_color)}
                        </div>
                        <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;'>
                            <tr>
                                <td style='padding:8px 0;width:140px;color:#64748b;font-weight:600;'>Created By</td>
                                <td style='padding:8px 0;color:#0f172a;'>{created_by.name}</td>
                            </tr>
                            <tr>
                                <td style='padding:8px 0;width:140px;color:#64748b;font-weight:600;'>Assigned To</td>
                                <td style='padding:8px 0;color:#0f172a;'>{assigned_to.name}</td>
                            </tr>
                            <tr>
                                <td style='padding:8px 0;color:#64748b;font-weight:600;'>Due Date</td>
                                <td style='padding:8px 0;color:#0f172a;'>{due_html}</td>
                            </tr>
                            <tr>
                                <td style='padding:8px 0;color:#64748b;font-weight:600;'>Notes</td>
                                <td style='padding:8px 0;color:#0f172a;'>{notes_html}</td>
                            </tr>
                            <tr>
                                <td style='padding:8px 0;color:#64748b;font-weight:600;'>Actions Taken</td>
                                <td style='padding:8px 0;color:#0f172a;'>{actions_html}</td>
                            </tr>
                        </table>
                        <div style='margin-top:24px;'>
                            <a href='https://portal.exceltutors.org.uk/todos?assigned={assigned_to.id}' style='display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 20px;border-radius:8px;font-size:14px;font-weight:600;'>View Task</a>
                        </div>
                        <p style='margin:28px 0 0;font-size:11px;color:#94a3b8;'>This automated message was sent to {assigned_to.email}. If you believe you received it in error, contact support.</p>
                    </td>
                </tr>
                <tr>
                    <td style='background:#f8fafc;padding:16px 28px;text-align:center;font-size:11px;color:#94a3b8;'>
                        &copy; {datetime.utcnow().year} Excel Tutors. All rights reserved.
                    </td>
                </tr>
            </table>
        </td></tr>
    </table>
</body>
</html>
"""


# ---------------- Appointment Emails ---------------- #

def _format_slot_range(slot_start: datetime, slot_end: datetime) -> Tuple[str, str, str]:
    date_label = slot_start.strftime('%A, %d %B %Y')
    start_label = slot_start.strftime('%H:%M')
    end_label = slot_end.strftime('%H:%M')
    return date_label, start_label, end_label


def _render_email_shell(title: str, headline: str, intro: str, body_inner: str, footer_html: str = '') -> str:
                                return f"""
<!DOCTYPE html>
<html lang='en'>
<head>
  <meta charset='utf-8'/>
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Segoe UI',Arial,sans-serif;">
  <table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='background:#f1f5f9;padding:24px 0;'>
    <tr><td align='center'>
      <table role='presentation' width='640' cellpadding='0' cellspacing='0' style='background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0;'>
                <tr>
                    <td style='background:#ffffff;padding:18px 24px;text-align:center;'>
                        <img src='cid:et-logo' alt='' style='display:block;margin:0 auto;max-height:40px;width:auto;'>
                    </td>
                </tr>
        <tr>
          <td style='padding:32px;'>
            <p style='margin:0 0 16px;font-size:15px;color:#0f172a;'>{intro}</p>
                        {f"<h2 style='margin:0 0 16px;font-size:18px;color:#0f172a;'>{headline}</h2>" if headline else ''}
                        {body_inner}
          </td>
        </tr>
        <tr>
          <td style='background:#f8fafc;padding:18px 32px;text-align:center;font-size:11px;color:#94a3b8;'>
            &copy; {datetime.utcnow().year} Excel Tutors. All rights reserved.{footer_html}
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>
"""


# ---------------- Admission Assessment Emails ---------------- #

def _admissions_public_url() -> str:
    fallback = 'https://admissions.exceltutors.org.uk'
    try:
        from flask import current_app

        for key in (
            'PUBLIC_ADMISSIONS_URL',
            'PUBLIC_ADMISSION_URL',
            'ADMISSIONS_PORTAL_URL',
            'ADMISSIONS_LANDING_URL',
        ):
            value = current_app.config.get(key)
            if value:
                return value
    except Exception:
        pass
    return fallback


def _format_decimal_display(value, *, suffix: str = '') -> str:
    if value is None or value == '':
        return '—'
    try:
        if isinstance(value, Decimal):
            quant = value
        else:
            quant = Decimal(str(value))
        text = format(quant.normalize(), 'f')
    except Exception:
        text = str(value)
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return f"{text}{suffix}"


def build_admission_assessment_confirmation_email(submission) -> Tuple[str, str]:
    parent_name = submission.parent_name or 'Parent/Guardian'
    student_label = submission.student_name or 'your child'
    requested_at = getattr(submission, 'created_at', None)
    if requested_at:
        try:
            requested_label = requested_at.strftime('%A, %d %B %Y at %I:%M %p').lstrip('0')
        except Exception:
            requested_label = str(requested_at)
    else:
        requested_label = 'Just now'
    subjects = list(submission.subjects_list()) if hasattr(submission, 'subjects_list') else []
    other = (submission.subjects_other or '').strip() if getattr(submission, 'subjects_other', None) else ''
    if other and other not in subjects:
        subjects.append(other)
    subjects_label = ', '.join(subjects) if subjects else '—'
    admissions_url = _admissions_public_url()
    subject = (
        f"We've received {submission.student_name}'s admission assessment request"
        if submission.student_name else
        "We've received your admission assessment request"
    )
    intro = (
        f"Dear {parent_name},<br/><br/>Thank you for submitting the admission assessment enquiry "
        f"for {student_label}. Our admissions team will review the details within one working day."
    )

    def _summary_row(label: str, value: str) -> str:
        safe_value = (value or '—').replace('\n', '<br/>')
        return (
            "<tr>"
            f"<td style='padding:6px 0;width:200px;color:#64748b;font-weight:600;'>{label}</td>"
            f"<td style='padding:6px 0;color:#0f172a;'>{safe_value}</td>"
            "</tr>"
        )

    body_parts = [
        "<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Here's a quick summary of what you sent us:</p>",
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>",
        _summary_row('Student name', submission.student_name or '—'),
        _summary_row('Year group', submission.student_year_group or '—'),
        _summary_row('Preferred branch', submission.branch or '—'),
        _summary_row('Parent contact number', submission.parent_phone or '—'),
        _summary_row('Parent email', submission.parent_email or '—'),
        _summary_row('Subjects of interest', subjects_label),
        _summary_row('Heard about Excel Tutors via', submission.heard_about or '—'),
        _summary_row('Submitted on', requested_label),
        "</table>",
        (
            "<div style='margin:24px 0 16px;text-align:center;'>"
            f"<a href='{admissions_url}' style='display:inline-block;background:#2563eb;color:#ffffff;padding:11px 22px;border-radius:10px;font-size:14px;font-weight:600;text-decoration:none;'>Complete the admissions form</a>"
            "</div>"
        ),
        "<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Next steps:</p>",
        (
            "<ul style='margin:0 0 12px 18px;padding:0;font-size:14px;color:#475569;list-style:disc;'>"
            "<li>We will call you to confirm the most suitable assessment slot at your preferred branch.</li>"
            "<li>Please complete the admissions form linked above so we can prepare your child's assessment file.</li>"
            "<li>On the day of the assessment, arrive 5 minutes early and bring any recent school reports if possible.</li>"
            "</ul>"
        ),
        "<p style='margin:0;font-size:13px;color:#64748b;'>If you have any questions before we reach out, reply to this email or phone 0207 0011 411.</p>",
    ]
    html = _render_email_shell('Admission assessment received', 'Admission assessment received', intro, ''.join(body_parts))
    return subject, html


def build_award_ceremony_confirmation_email(registration, ceremony) -> Tuple[str, str]:
    """Branded confirmation email for award ceremony registration."""
    child_label = registration.child_name or 'your child'
    subject = f"Award Ceremony Registration Received \u2013 {ceremony.name}"
    intro = (
        f"Dear Parent/Guardian,<br/><br/>"
        f"Thank you for registering <strong>{child_label}</strong> for "
        f"<strong>{ceremony.name}</strong>. We have received your submission."
    )

    def _row(label: str, value: str) -> str:
        safe_value = (value or '\u2014').replace('\\n', '<br/>')
        return (
            "<tr>"
            f"<td style='padding:6px 0;width:200px;color:#64748b;font-weight:600;'>{label}</td>"
            f"<td style='padding:6px 0;color:#0f172a;'>{safe_value}</td>"
            "</tr>"
        )

    registered_at = 'Just now'
    if getattr(registration, 'created_at', None):
        try:
            registered_at = registration.created_at.strftime('%A, %d %B %Y at %I:%M %p').lstrip('0')
        except Exception:
            registered_at = str(registration.created_at)

    ceremony_date = '\u2014'
    if getattr(ceremony, 'date', None):
        try:
            ceremony_date = ceremony.date.strftime('%A, %d %B %Y')
        except Exception:
            ceremony_date = str(ceremony.date)

    body_parts = [
        "<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Here is a summary of your registration:</p>",
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>",
        _row('Child Name', registration.child_name or '\u2014'),
        _row('Student ID', registration.student_id or '\u2014'),
        _row('Year Group', (registration.year_group or '\u2014').replace('year', 'Year ').title()),
        _row('Event', ceremony.name),
        _row('Date', ceremony_date),
        _row('Venue', ceremony.venue or '\u2014'),
        _row('Time', ceremony.time or '\u2014'),
        _row('Registered At', registered_at),
        "</table>",
        (
            "<p style='margin:16px 0 0;font-size:13px;color:#64748b;'>"
            "<strong>Please note:</strong> Submission of this form is not confirmation of receipt of an award. "
            "All registrations are subject to internal verification by Excel Tutors. "
            "The child's name will appear exactly as entered on any certificates issued."
            "</p>"
        ),
        "<p style='margin:12px 0 0;font-size:13px;color:#64748b;'>If you have any questions, reply to this email or phone 0207 0011 411.</p>",
    ]
    html = _render_email_shell('Award Ceremony Registration', 'Registration Received', intro, ''.join(body_parts))
    return subject, html


def build_admission_assessment_scores_email(submission, scores: list) -> Tuple[str, str]:
    parent_name = submission.parent_name or 'Parent/Guardian'
    student_label = submission.student_name or 'your child'
    admissions_url = _admissions_public_url()
    subject = (
        f"Admission assessment results for {submission.student_name}"
        if submission.student_name else
        "Admission assessment results"
    )
    intro = (
        f"Dear {parent_name},<br/><br/>Thank you for completing the admission assessment with Excel Tutors. "
        f"Below is the breakdown of {student_label}'s performance along with our academic recommendations."
    )

    rows = []
    for score in scores:
        subject_name = getattr(score, 'subject', 'Subject') or 'Subject'
        marks = _format_decimal_display(getattr(score, 'marks_achieved', None))
        total = _format_decimal_display(getattr(score, 'total_marks', None))
        percentage = _format_decimal_display(getattr(score, 'percentage', None), suffix='%')
        recommendation = (getattr(score, 'recommendation', None) or '—').replace('\n', '<br/>')
        rows.append(
            "<tr style='border-top:1px solid #e2e8f0;'>"
            f"<td style='padding:10px 12px;font-weight:600;color:#0f172a;'>{subject_name}</td>"
            f"<td style='padding:10px 12px;color:#0f172a;'>{marks}</td>"
            f"<td style='padding:10px 12px;color:#0f172a;'>{total}</td>"
            f"<td style='padding:10px 12px;color:#0f172a;'>{percentage}</td>"
            f"<td style='padding:10px 12px;color:#334155;'>{recommendation}</td>"
            "</tr>"
        )

    summary_block = []
    summary_block.append(
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:13px;color:#0f172a;margin:0 0 18px;'>"
        "<tr style='background:#f8fafc;'>"
        "<th align='left' style='padding:10px 12px;color:#1e293b;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;'>Subject</th>"
        "<th align='left' style='padding:10px 12px;color:#1e293b;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;'>Marks</th>"
        "<th align='left' style='padding:10px 12px;color:#1e293b;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;'>Out of</th>"
        "<th align='left' style='padding:10px 12px;color:#1e293b;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;'>Percentage</th>"
        "<th align='left' style='padding:10px 12px;color:#1e293b;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;'>Recommendation</th>"
        "</tr>"
        + ''.join(rows)
        + "</table>"
    )

    branch_line = submission.branch or 'your chosen branch'
    closing = (
        f"<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Our admissions team will contact you shortly to discuss the results and outline the best learning plan for {student_label}. "
        f"If you have not already done so, please complete the admissions form so we can secure the most appropriate timetable at {branch_line}.</p>"
    )
    cta_html = (
        "<div style='margin:18px 0 0;text-align:center;'>"
        f"<a href='{admissions_url}' style='display:inline-block;background:#22c55e;color:#ffffff;padding:10px 22px;border-radius:10px;font-size:14px;font-weight:600;text-decoration:none;'>Submit admissions form</a>"
        "</div>"
    )
    support = "<p style='margin:18px 0 0;font-size:13px;color:#64748b;'>Questions? Reply to this email or phone 0207 0011 411 and the admissions desk will be happy to help.</p>"

    html = _render_email_shell(
        'Admission assessment results',
        'Admission assessment results',
        intro,
        ''.join(summary_block + [closing, cta_html, support]),
    )
    return subject, html


def build_job_application_confirmation_email(applicant) -> Tuple[str, str]:
    """Return branded subject/html for job application confirmation.

    Uses the shared _render_email_shell for consistent branding.
    """
    subject = "Application received – Excel Tutors"
    intro = f"Hello {applicant.first_name} {applicant.last_name},<br/><br/>Thanks for applying to join Excel Tutors. We've received your application and our recruitment team will review it shortly."
    # Summarise key details
    def row(label, value):
        safe = (value or '').replace('\n','<br/>')
        return (f"<tr>"
                f"<td style='padding:6px 0;width:200px;color:#64748b;font-weight:600;'>{label}</td>"
                f"<td style='padding:6px 0;color:#0f172a;'>{safe}</td>"
                f"</tr>")
    branches = ', '.join(applicant.branches_list()) or '—'
    subjects = ', '.join([s.strip() for s in (getattr(applicant, 'subjects', '') or '').split(',') if s.strip()]) or '—'
    body_inner = [
        "<p style='margin:0 0 12px;font-size:14px;color:#334155;'>Here is a quick summary of your submission:</p>",
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>",
        row('Email', applicant.email),
        row('Phone', applicant.phone),
        row('Address', f"{applicant.address_line1}, {applicant.city} {applicant.postcode}".strip()),
        row('University', applicant.university),
        row('Year of Study', applicant.study_year),
        row('Course', applicant.course_name),
        row('A Level 1', f"{applicant.alevel1_subject} – {applicant.alevel1_grade} ({applicant.alevel1_status})"),
        row('A Level 2', f"{applicant.alevel2_subject} – {applicant.alevel2_grade} ({applicant.alevel2_status})"),
        row('A Level 3', f"{applicant.alevel3_subject} – {applicant.alevel3_grade} ({applicant.alevel3_status})"),
        row('GCSE Maths', f"{applicant.gcse_maths_grade} ({applicant.gcse_maths_status})"),
        row('GCSE English', f"{applicant.gcse_english_grade} ({applicant.gcse_english_status})"),
    row('GCSE Science', f"{getattr(applicant, 'gcse_science_grade', '')} ({getattr(applicant, 'gcse_science_status', '')})"),
        row('Tutoring Experience', 'Yes' if applicant.tutoring_experience else 'No'),
        row('Eligible to Work in UK', 'Yes' if applicant.uk_work_eligible else 'No'),
    row('Subjects you can tutor', subjects),
        row('Preferred Branches', branches),
        row('How you heard about us', applicant.heard_about or '—'),
        "</table>",
        "<p style='margin:16px 0 0;font-size:13px;color:#475569;'>We aim to get back to you within 7 business days. If you have any questions, reply to this email.</p>",
        "<p style='margin:10px 0 0;font-size:13px;color:#475569;'>Best regards,<br/>Excel Tutors Recruitment Team</p>",
    ]
    html = _render_email_shell('Application received', 'Application received', intro, ''.join(body_inner))
    return subject, html


_CUSTOMER_COPY = {
    'en': {
        'greeting': "Hello {name},",
        'signature': "Best regards,<br>Excel Tutors Admin Team",
        'details_heading': 'Appointment details',
        'fields': {
            'superadmin': 'With',
            'date': 'Date',
            'time': 'Time',
            'student': 'Student Name / ID',
            'reason': 'Reason',
            'email': 'Email',
            'phone': 'Phone',
        },
        'modes': {
            'confirmation': {
                'subject': 'Appointment confirmed with {superadmin}',
                'headline': 'Appointment confirmed',
                'intro': 'Thanks {name}, your appointment has been booked with {superadmin}.',
                'body': 'We will meet on <strong>{date}</strong> at <strong>{time}</strong>. Please arrive a few minutes early if possible.',
                'cta': 'Cancel appointment',
            },
            'reminder': {
                'subject': 'Reminder: appointment with {superadmin} in 12 hours',
                'headline': 'Appointment reminder',
                'intro': 'This is a friendly reminder of your appointment with {superadmin}.',
                'body': 'We are looking forward to seeing you on <strong>{date}</strong> at <strong>{time}</strong>. If you can no longer attend, please cancel so we can reopen the slot.',
                'cta': 'Cancel appointment',
            },
            'cancelled': {
                'subject': 'Appointment cancelled',
                'headline': 'Appointment cancelled',
                'intro': 'Your appointment with {superadmin} has been cancelled.',
                'body': 'If this was a mistake you can book a new slot from the public booking page.',
                'cta': None,
            },
        },
    },
    'bn': {
        'greeting': "প্রিয় {name},",
        'signature': "শুভেচ্ছান্তে,<br>এক্সেল টিউটরস টিম",
        'details_heading': 'অ্যাপয়েন্টমেন্টের বিবরণ',
        'fields': {
            'superadmin': 'কার সাথে',
            'date': 'তারিখ',
            'time': 'সময়',
            'student': 'শিক্ষার্থীর নাম / আইডি',
            'reason': 'কারণ',
            'email': 'ইমেইল',
            'phone': 'ফোন',
        },
        'modes': {
            'confirmation': {
                'subject': '{superadmin}-এর সাথে আপনার অ্যাপয়েন্টমেন্ট নিশ্চিত হয়েছে',
                'headline': 'অ্যাপয়েন্টমেন্ট নিশ্চিত',
                'intro': '{superadmin}-এর সাথে আপনার অ্যাপয়েন্টমেন্ট সফলভাবে বুক হয়েছে।',
                'body': '<strong>{date}</strong> তারিখে <strong>{time}</strong> সময়ে আপনাকে স্বাগত জানানো হবে। সম্ভব হলে কয়েক মিনিট আগে পৌঁছাতে অনুরোধ করা হচ্ছে।',
                'cta': 'অ্যাপয়েন্টমেন্ট বাতিল করুন',
            },
            'reminder': {
                'subject': 'স্মারক: {superadmin}-এর সাথে আপনার অ্যাপয়েন্টমেন্ট ১২ ঘণ্টার মধ্যে',
                'headline': 'অ্যাপয়েন্টমেন্ট স্মারক',
                'intro': '{superadmin}-এর সাথে আপনার আসন্ন অ্যাপয়েন্টমেন্ট সম্পর্কে স্মরণ করিয়ে দিচ্ছি।',
                'body': '<strong>{date}</strong> তারিখে <strong>{time}</strong> সময়ে দেখা হবে। উপস্থিত থাকতে না পারলে দ্রুত বাতিল করুন যাতে অন্য কেউ ব্যবহার করতে পারে।',
                'cta': 'অ্যাপয়েন্টমেন্ট বাতিল করুন',
            },
            'cancelled': {
                'subject': 'আপনার অ্যাপয়েন্টমেন্ট বাতিল হয়েছে',
                'headline': 'অ্যাপয়েন্টমেন্ট বাতিল',
                'intro': '{superadmin}-এর সাথে আপনার অ্যাপয়েন্টমেন্ট বাতিল করা হয়েছে।',
                'body': 'ভুলবশত বাতিল হয়ে থাকলে নতুন করে বুক করতে পারেন।',
                'cta': None,
            },
        },
    },
}


_ADMIN_COPY = {
    'confirmation': {
        'subject': 'New appointment booked: {student}',
        'headline': 'New appointment scheduled',
        'intro': '{name} booked an appointment with you.',
        'footer': 'Manage this slot from the admin portal.',
    },
    'reminder': {
        'subject': 'Upcoming appointment with {student}',
        'headline': '12-hour reminder',
        'intro': "Here's a reminder of your upcoming appointment.",
        'footer': 'You are receiving this because a reminder was requested 12 hours before the meeting.',
    },
    'cancelled_user': {
        'subject': 'Appointment cancelled by attendee: {student}',
        'headline': 'Attendee cancelled',
        'intro': '{name} has cancelled the appointment.',
        'footer': 'The slot has been reopened automatically.',
    },
    'cancelled_admin': {
        'subject': 'You cancelled {student} appointment',
        'headline': 'Appointment cancelled',
        'intro': 'You cancelled this appointment.',
        'footer': 'The attendee has been notified automatically.',
    },
}


def _build_details_html(copy: dict, booking, slot) -> str:
    date_label, start_label, end_label = _format_slot_range(slot.start_at, slot.end_at)
    rows = [
        (copy['fields']['superadmin'], slot.superadmin.name),
        (copy['fields']['date'], date_label),
        (copy['fields']['time'], f"{start_label} – {end_label}"),
        (copy['fields']['student'], booking.student_ref),
        (copy['fields']['reason'], booking.reason),
        (copy['fields']['email'], booking.email),
        (copy['fields']['phone'], booking.phone),
    ]
    lines = ["<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>"]
    lines.append(f"<tr><td colspan='2' style='padding:0 0 12px;font-weight:600;font-size:15px;'>{copy['details_heading']}</td></tr>")
    for label, value in rows:
        lines.append(
            "<tr>"
            f"<td style='padding:6px 0;width:170px;color:#64748b;font-weight:600;'>{label}</td>"
            f"<td style='padding:6px 0;color:#0f172a;'>{value}</td>"
            "</tr>"
        )
    lines.append("</table>")
    return ''.join(lines)


def build_appointment_email(booking, slot, superadmin, *, language: str = 'en', mode: str = 'confirmation', cancel_url: str | None = None) -> Tuple[str, str]:
    lang = language if language in _CUSTOMER_COPY else 'en'
    copy = _CUSTOMER_COPY[lang]
    mode_copy = copy['modes'].get(mode, copy['modes']['confirmation'])
    date_label, start_label, _ = _format_slot_range(slot.start_at, slot.end_at)
    subject = mode_copy['subject'].format(superadmin=superadmin.name, date=date_label, time=start_label)
    intro = copy['greeting'].format(name=booking.name)
    intro += f"<br/><br/>{mode_copy['intro'].format(name=booking.name, superadmin=superadmin.name, date=date_label, time=start_label)}"
    body = f"<p style='margin:0 0 18px;font-size:14px;color:#334155;'>{mode_copy['body'].format(date=date_label, time=start_label)}</p>"
    body += _build_details_html(copy, booking, slot)
    if mode_copy.get('cta') and cancel_url:
        body += (
            "<div style='margin-top:24px;'>"
            f"<a href='{cancel_url}' style='display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:10px;font-size:14px;font-weight:600;'>{mode_copy['cta']}</a>"
            "</div>"
        )
    body += f"<p style='margin:28px 0 0;font-size:13px;color:#475569;'>{copy['signature']}</p>"
    html = _render_email_shell(subject, mode_copy['headline'], intro, body)
    return subject, html


def build_appointment_admin_email(booking, slot, *, mode: str = 'confirmation') -> Tuple[str, str]:
    template = _ADMIN_COPY.get(mode, _ADMIN_COPY['confirmation'])
    date_label, start_label, end_label = _format_slot_range(slot.start_at, slot.end_at)
    subject = template['subject'].format(student=booking.student_ref, name=booking.name)
    intro = template['intro'].format(name=booking.name)
    body = []
    body.append(
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>"
    )
    body.extend([
        f"<tr><td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>Attendee</td><td style='padding:6px 0;color:#0f172a;'>{booking.name}</td></tr>",
        f"<tr><td style='padding:6px 0;color:#64748b;font-weight:600;'>Student</td><td style='padding:6px 0;color:#0f172a;'>{booking.student_ref}</td></tr>",
        f"<tr><td style='padding:6px 0;color:#64748b;font-weight:600;'>Reason</td><td style='padding:6px 0;color:#0f172a;'>{booking.reason}</td></tr>",
        f"<tr><td style='padding:6px 0;color:#64748b;font-weight:600;'>Date</td><td style='padding:6px 0;color:#0f172a;'>{date_label}</td></tr>",
        f"<tr><td style='padding:6px 0;color:#64748b;font-weight:600;'>Time</td><td style='padding:6px 0;color:#0f172a;'>{start_label} – {end_label}</td></tr>",
        f"<tr><td style='padding:6px 0;color:#64748b;font-weight:600;'>Email</td><td style='padding:6px 0;color:#0f172a;'>{booking.email}</td></tr>",
        f"<tr><td style='padding:6px 0;color:#64748b;font-weight:600;'>Phone</td><td style='padding:6px 0;color:#0f172a;'>{booking.phone}</td></tr>",
    ])
    body.append("</table>")
    body.append(f"<p style='margin:24px 0 0;font-size:12px;color:#64748b;'>{template.get('footer','')}</p>")
    html = _render_email_shell(subject, template['headline'], intro, ''.join(body))
    return subject, html


# ---------------- Meeting Emails (internal scheduling) ---------------- #

def build_meeting_student_email(meeting, student, participant, *, mode: str = 'confirmation') -> Tuple[str, str]:
    """Build student-facing meeting email (confirmation or reminder)."""
    dt_label = ''
    try:
        dt_label = f"{meeting.date.strftime('%A, %d %B %Y')} at {meeting.time}"
    except Exception:
        dt_label = f"{meeting.date} at {meeting.time}"
    if mode == 'reminder':
        subject = f"Reminder: meeting with {participant.name} on {dt_label}"
        headline = 'Meeting reminder'
        intro = f"Hello {student.name if getattr(student,'name',None) else 'there'},<br/><br/>This is a reminder for your meeting with {participant.name}."
        body = f"<p style='margin:0 0 14px;font-size:14px;color:#334155;'>We will meet on <strong>{dt_label}</strong>.</p>"
    else:
        subject = f"Meeting confirmed with {participant.name} on {dt_label}"
        headline = 'Meeting confirmed'
        intro = f"Hello {student.name if getattr(student,'name',None) else 'there'},<br/><br/>Your meeting has been scheduled with {participant.name}."
        body = f"<p style='margin:0 0 14px;font-size:14px;color:#334155;'>Date & time: <strong>{dt_label}</strong></p>"
    # Include agenda if present
    try:
        if getattr(meeting, 'agenda', None):
            body += f"<p style='margin:0 0 14px;font-size:14px;color:#334155;'>Agenda: {meeting.agenda}</p>"
    except Exception:
        pass
    body += "<p style='margin:16px 0 0;font-size:13px;color:#475569;'>Best regards,<br/>Excel Tutors Team</p>"
    html = _render_email_shell(subject, headline, intro, body)
    return subject, html


def build_meeting_admin_email(meeting, participant, student, *, mode: str = 'confirmation') -> Tuple[str, str]:
    """Build internal admin/participant email for meeting events."""
    try:
        dt_label = f"{meeting.date.strftime('%A, %d %B %Y')} at {meeting.time}"
    except Exception:
        dt_label = f"{meeting.date} at {meeting.time}"
    if mode == 'reminder':
        subject = f"Reminder: upcoming meeting with {student.name if student else (meeting.student_name or 'student')}"
        headline = 'Meeting reminder'
        intro = "Here's a reminder of your upcoming meeting."
    else:
        subject = f"New meeting scheduled: {student.name if student else (meeting.student_name or 'student')}"
        headline = 'New meeting scheduled'
        intro = 'A meeting has been scheduled.'
    body = []
    body.append("<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>")
    def row(label, value):
        body.append(f"<tr><td style='padding:6px 0;width:180px;color:#64748b;font-weight:600;'>{label}</td><td style='padding:6px 0;color:#0f172a;'>{value}</td></tr>")
    row('With', participant.name if participant else '—')
    row('Date/Time', dt_label)
    row('Student', (student.name if student else (meeting.student_name or '—')))
    if getattr(meeting, 'agenda', None):
        row('Agenda', meeting.agenda)
    body.append("</table>")
    html = _render_email_shell(subject, headline, intro, ''.join(body))
    return subject, html


# ---------------- Task reminder emails ---------------- #

def build_task_due_soon_email(task, assigned_to) -> Tuple[str, str]:
    """Branded email HTML for tasks due soon (within a few days)."""
    due = task.due_date.strftime('%Y-%m-%d') if getattr(task, 'due_date', None) else 'N/A'
    subject = f"Task due soon: {task.description[:60]} (Due {due})"
    intro = f"Hello {assigned_to.name},<br/><br/>This is a friendly reminder that the following task is approaching its deadline."
    crit = getattr(task, 'criticality', 'N/A') or 'N/A'
    urg = getattr(task, 'urgency', 'N/A') or 'N/A'
    def row(label, value):
        safe = (str(value) or '').replace('\n','<br/>')
        return f"<tr><td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td><td style='padding:6px 0;color:#0f172a;'>{safe}</td></tr>"
    body = [
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>",
        row('Description', task.description),
        row('Due Date', due),
        row('Criticality', crit),
        row('Urgency', urg),
        row('Assigned By', task.created_by.name if getattr(task, 'created_by', None) else '—'),
        "</table>",
        "<p style='margin:16px 0 0;font-size:13px;color:#475569;'>Please review and complete before the deadline.</p>",
        f"<div style='margin-top:16px;'><a href='https://portal.exceltutors.org.uk/todos?assigned={assigned_to.id}' style='display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:10px 16px;border-radius:8px;font-size:14px;font-weight:600;'>View Tasks</a></div>",
    ]
    html = _render_email_shell('Task due soon', 'Task due soon', intro, ''.join(body))
    return subject, html

def build_task_overdue_email(task, assigned_to) -> Tuple[str, str]:
    """Branded email HTML for overdue tasks."""
    due = task.due_date.strftime('%Y-%m-%d') if getattr(task, 'due_date', None) else 'N/A'
    subject = f"Overdue task: {task.description[:60]} (Due {due})"
    intro = f"Hello {assigned_to.name},<br/><br/><strong>This task is now overdue.</strong> Please take action as soon as possible."
    crit = getattr(task, 'criticality', 'N/A') or 'N/A'
    urg = getattr(task, 'urgency', 'N/A') or 'N/A'
    def row(label, value):
        safe = (str(value) or '').replace('\n','<br/>')
        return f"<tr><td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td><td style='padding:6px 0;color:#0f172a;'>{safe}</td></tr>"
    body = [
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>",
        row('Description', task.description),
        row('Due Date', due),
        row('Criticality', crit),
        row('Urgency', urg),
        row('Assigned By', task.created_by.name if getattr(task, 'created_by', None) else '—'),
        "</table>",
        "<p style='margin:16px 0 0;font-size:13px;color:#475569;'>If this is already completed, please mark it as Done in the portal.</p>",
        f"<div style='margin-top:16px;'><a href='https://portal.exceltutors.org.uk/todos?assigned={assigned_to.id}' style='display:inline-block;background:#dc2626;color:#ffffff;text-decoration:none;padding:10px 16px;border-radius:8px;font-size:14px;font-weight:600;'>Review Task</a></div>",
    ]
    html = _render_email_shell('Task overdue', 'Task overdue', intro, ''.join(body))
    return subject, html


# ---------------- Enrollment Confirmation Email ---------------- #

def build_enrollment_confirmation_email(order) -> Tuple[str, str]:
    """Branded confirmation email for a completed enrollment order."""
    student_name = order.student_name or 'Student'
    subject = f"Enrollment Confirmation – Order #{order.id}"

    intro = (
        f"Thank you for enrolling <strong>{student_name}</strong> with Excel Tutors!<br/><br/>"
        "Your payment has been received and the enrollment is confirmed. "
        "Please find the details of your order below."
    )

    def row(label, value):
        safe = (str(value) or '').replace('\n', '<br/>')
        return (
            f"<tr>"
            f"<td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td>"
            f"<td style='padding:6px 0;color:#0f172a;'>{safe}</td>"
            f"</tr>"
        )

    # Order summary rows
    order_date = order.created_at.strftime('%d %B %Y') if order.created_at else '—'
    body_parts = [
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>",
        row('Order Number', f"#{order.id}"),
        row('Student Name', student_name),
        row('Student ID', order.student_id or '—'),
        row('Branch', order.branch or '—'),
        row('Year Group', (order.year_group or '—').replace('year', 'Year ').title()),
        row('Order Date', order_date),
        "</table>",
    ]

    # Course items table
    items = order.items if order.items else []
    if items:
        body_parts.append(
            "<h3 style='margin:20px 0 8px;font-size:15px;color:#363f99;font-weight:700;'>Enrolled Courses</h3>"
            "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:13px;'>"
            "<tr style='background:#f1f5f9;'>"
            "<th style='padding:8px 10px;text-align:left;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Course</th>"
            "<th style='padding:8px 10px;text-align:left;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Date</th>"
            "<th style='padding:8px 10px;text-align:left;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Time</th>"
            "<th style='padding:8px 10px;text-align:left;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Venue</th>"
            "<th style='padding:8px 10px;text-align:right;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Price</th>"
            "</tr>"
        )
        for item in items:
            date_str = item.product_date.strftime('%d %b %Y') if item.product_date else '—'
            price_str = f"£{float(item.product_price or 0):.2f}"
            body_parts.append(
                f"<tr>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#0f172a;'>{item.product_name}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#475569;'>{date_str}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#475569;'>{item.product_time or '—'}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#475569;'>{item.product_venue or '—'}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#0f172a;text-align:right;font-weight:600;'>{price_str}</td>"
                f"</tr>"
            )
        body_parts.append("</table>")

    # Totals
    subtotal_str = f"£{float(order.subtotal or 0):.2f}"
    discount_str = f"£{float(order.discount_amount or 0):.2f}"
    total_str = f"£{float(order.total or 0):.2f}"
    body_parts.append(
        "<table role='presentation' cellpadding='0' cellspacing='0' style='margin:12px 0 0 auto;border-collapse:collapse;font-size:14px;'>"
        f"<tr><td style='padding:4px 16px 4px 0;color:#64748b;'>Subtotal:</td><td style='padding:4px 0;text-align:right;color:#0f172a;font-weight:600;'>{subtotal_str}</td></tr>"
    )
    if order.discount_amount and float(order.discount_amount) > 0:
        body_parts.append(
            f"<tr><td style='padding:4px 16px 4px 0;color:#16a34a;'>Discount:</td><td style='padding:4px 0;text-align:right;color:#16a34a;font-weight:600;'>-{discount_str}</td></tr>"
        )
    body_parts.append(
        f"<tr style='border-top:2px solid #e2e8f0;'><td style='padding:8px 16px 4px 0;color:#0f172a;font-weight:700;font-size:16px;'>Total Paid:</td>"
        f"<td style='padding:8px 0 4px;text-align:right;color:#363f99;font-weight:700;font-size:16px;'>{total_str}</td></tr>"
        "</table>"
    )

    # Footer note
    body_parts.append(
        "<p style='margin:20px 0 0;font-size:13px;color:#475569;'>"
        "A PDF invoice is attached to this email for your records. "
        "If you have any questions about your enrollment, please contact us at "
        "<a href='mailto:management@exceltutors.org.uk' style='color:#363f99;'>management@exceltutors.org.uk</a>."
        "</p>"
    )

    html = _render_email_shell(
        'Enrollment Confirmation',
        'Enrollment Confirmed',
        intro,
        ''.join(body_parts),
    )
    return subject, html


def build_mock_test_confirmation_email(booking) -> Tuple[str, str]:
    """Branded confirmation email for a completed mock test booking."""
    student_name = booking.student_name or 'Student'
    subject = f"Mock Exam Booking Confirmation – Booking #{booking.id}"

    intro = (
        f"Thank you for booking mock exams for <strong>{student_name}</strong> with Excel Tutors!<br/><br/>"
        "<strong style='color:#b45309;'>Please note: This order is currently unpaid. "
        "Full payment must be made before the mock test date.</strong><br/><br/>"
        "Please find the details of your booking below."
    )

    def row(label, value):
        safe = (str(value) or '').replace('\n', '<br/>')
        return (
            f"<tr>"
            f"<td style='padding:6px 0;width:160px;color:#64748b;font-weight:600;'>{label}</td>"
            f"<td style='padding:6px 0;color:#0f172a;'>{safe}</td>"
            f"</tr>"
        )

    booking_date = booking.created_at.strftime('%d %B %Y') if booking.created_at else '—'
    body_parts = [
        "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:14px;color:#0f172a;'>",
        row('Booking Number', f"#{booking.id}"),
        row('Student Name', student_name),
        row('Branch', booking.branch or '—'),
        row('Year Group', (booking.year_group or '—').replace('year', 'Year ').title()),
        row('Booking Date', booking_date),
        "</table>",
    ]

    items = booking.items if booking.items else []
    if items:
        body_parts.append(
            "<h3 style='margin:20px 0 8px;font-size:15px;color:#363f99;font-weight:700;'>Booked Exams</h3>"
            "<table role='presentation' width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;font-size:13px;'>"
            "<tr style='background:#f1f5f9;'>"
            "<th style='padding:8px 10px;text-align:left;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Exam</th>"
            "<th style='padding:8px 10px;text-align:left;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Subject</th>"
            "<th style='padding:8px 10px;text-align:left;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Date</th>"
            "<th style='padding:8px 10px;text-align:left;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Time</th>"
            "<th style='padding:8px 10px;text-align:left;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Reporting Time</th>"
            "<th style='padding:8px 10px;text-align:left;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Venue</th>"
            "<th style='padding:8px 10px;text-align:right;color:#363f99;font-weight:700;border-bottom:1px solid #e2e8f0;'>Price</th>"
            "</tr>"
        )
        for item in items:
            date_str = item.test_date.strftime('%d %b %Y') if item.test_date else '—'
            price_str = f"£{float(item.test_price or 0):.2f}"
            reporting_time_str = getattr(item, 'test_reporting_time', None) or '—'
            body_parts.append(
                f"<tr>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#0f172a;'>{item.test_name}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#475569;'>{item.test_subject or '—'}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#475569;'>{date_str}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#475569;'>{item.test_time or '—'}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#475569;'>{reporting_time_str}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#475569;'>{item.test_venue or '—'}</td>"
                f"<td style='padding:8px 10px;border-bottom:1px solid #f1f5f9;color:#0f172a;text-align:right;font-weight:600;'>{price_str}</td>"
                f"</tr>"
            )
        body_parts.append("</table>")

    total_str = f"£{float(booking.total or 0):.2f}"
    body_parts.append(
        "<table role='presentation' cellpadding='0' cellspacing='0' style='margin:12px 0 0 auto;border-collapse:collapse;font-size:14px;'>"
        f"<tr style='border-top:2px solid #e2e8f0;'><td style='padding:8px 16px 4px 0;color:#0f172a;font-weight:700;font-size:16px;'>Total Due:</td>"
        f"<td style='padding:8px 0 4px;text-align:right;color:#b45309;font-weight:700;font-size:16px;'>{total_str}</td></tr>"
        "</table>"
    )

    body_parts.append(
        "<div style='margin:16px 0;padding:12px 16px;background:#fef3c7;border-left:4px solid #f59e0b;border-radius:4px;'>"
        "<p style='margin:0;font-size:14px;color:#92400e;font-weight:600;'>"
        "⚠️ This order is unpaid. Full payment must be made before the mock test date."
        "</p>"
        "</div>"
    )

    body_parts.append(
        "<p style='margin:20px 0 0;font-size:13px;color:#475569;'>"
        "A PDF invoice is attached to this email for your records. "
        "If you have any questions about your booking, please contact us at "
        "<a href='mailto:management@exceltutors.org.uk' style='color:#363f99;'>management@exceltutors.org.uk</a>."
        "</p>"
    )

    html = _render_email_shell(
        'Mock Exam Booking',
        'Booking Confirmed',
        intro,
        ''.join(body_parts),
    )
    return subject, html
