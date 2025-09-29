import smtplib
from datetime import datetime
from email.message import EmailMessage

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
