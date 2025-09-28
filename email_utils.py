import smtplib
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
