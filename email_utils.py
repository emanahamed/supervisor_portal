import smtplib
from datetime import datetime
from datetime import datetime as _dt
from email.message import EmailMessage
from typing import Optional, Tuple

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


def _populate_msg_headers(msg: EmailMessage, subject: str, to_email: str, setting: Optional[EmailSetting] = None):
    if setting and setting.sender_email:
        from_addr = setting.sender_email
        from_name = setting.sender_name or ''
        msg['From'] = f"{from_name} <{from_addr}>" if from_name else from_addr
    else:
        msg['From'] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg['To'] = to_email
    msg['Subject'] = subject
    if setting and setting.sender_email:
        msg['Reply-To'] = setting.sender_email
    else:
        if REPLY_TO:
            msg['Reply-To'] = REPLY_TO


def send_email(to_email: str, subject: str, html: str, *, setting_name: Optional[str] = None, attachments: Optional[list] = None) -> None:
    """Send an HTML email using DB-backed EmailSetting when available.

    attachments: list of tuples (bytes, maintype, subtype, filename)
    """
    setting = _get_active_email_setting(name=setting_name)
    msg = EmailMessage()
    _populate_msg_headers(msg, subject, to_email, setting)
    msg.set_content("This email contains HTML content. Please view in an HTML-capable client.")
    msg.add_alternative(html, subtype='html')

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


def send_recruitment_email(to_email: str, subject: str, html: str, *, attachments: Optional[list] = None) -> None:
    """Shorthand for sending via the 'recruitment' EmailSetting.

    Ensures job-related communications use the Recruitment mailbox identity
    (from name/address) configured in Email Settings.
    """
    send_email(to_email, subject, html, setting_name='recruitment', attachments=attachments)


def build_interview_invitation_email(applicant, slots: list[tuple[str, list[str]]]) -> Tuple[str, str]:
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
    body_inner = [
        f"<p style='margin:0 0 8px 0;font-size:14px;color:#334155;'>{intro}</p>",
        "<div style='font-family:monospace; font-size:13px; line-height:1.6; color:#0f172a;'>" + "<br/>".join(lines) + "</div>",
        f"<div style='font-size:13px;color:#334155;'>{details}</div>",
        f"<div style='font-size:13px;color:#334155;'>{footer}</div>",
    ]
    html = _render_email_shell('Interview invitation', 'Interview invitation', '', ''.join(body_inner))
    return (subject, html)


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
                    <td style='background:#0f172a;padding:20px 28px;'>
                        <h1 style='margin:0;font-size:20px;line-height:1.3;color:#ffffff;font-weight:600;'>Excel Tutors Task Notification</h1>
                        <p style='margin:4px 0 0;font-size:12px;color:#94a3b8;'>A new task has been created by {created_by.name}.</p>
                    </td>
                </tr>
                <tr>
                    <td style='padding:28px;'>
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
          <td style='background:#0f172a;padding:24px 32px;'>
            <h1 style='margin:0;font-size:22px;line-height:1.3;color:#ffffff;font-weight:600;'>Excel Tutors</h1>
            <p style='margin:6px 0 0;font-size:13px;color:#cbd5f5;'>{headline}</p>
          </td>
        </tr>
        <tr>
          <td style='padding:32px;'>
            <p style='margin:0 0 16px;font-size:15px;color:#0f172a;'>{intro}</p>
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
