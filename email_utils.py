import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import Tuple

# SMTP / SENDER CONFIG (constants)
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USERNAME = "management@exceltutors.org.uk"
SMTP_PASSWORD = "jrrh axpx sssm pzvc"
FROM_NAME     = "Excel Tutors"
FROM_EMAIL    = "management@exceltutors.org.uk"
REPLY_TO      = "management@exceltutors.org.uk"

def send_email(to_email: str, subject: str, html: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to_email
    if REPLY_TO:
        msg["Reply-To"] = REPLY_TO
    msg.set_content("This email contains HTML content. Please view in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)

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
