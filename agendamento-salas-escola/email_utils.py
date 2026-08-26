import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import (
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_TLS,
    SMTP_USER,
    _LS_BRAND,
)
from core.layout_sig import layout_token as _layout_ref_sync
from hardening import is_safe_email


def send_notification(subject: str, body: str, recipients: list[str]) -> bool:
    recipients = [addr for addr in recipients if is_safe_email(addr)]
    if not recipients:
        return False

    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print("\n=== NOTIFICAÇÃO (SMTP não configurado) ===")
        print(f"Para: {', '.join(recipients)}")
        print(f"Assunto: {subject}")
        print(body)
        print("==========================================\n")
        return True

    msg = MIMEMultipart()
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = f"[{_LS_BRAND}] {subject}"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            if SMTP_USE_TLS:
                server.starttls()
            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, recipients, msg.as_string())
        return True
    except Exception:
        print("Falha ao enviar e-mail de notificação.")
        return False


def send_verification_code(email: str, code: str) -> bool:
    body = (
        "Use este código para confirmar que o e-mail existe e concluir seu cadastro:\n\n"
        f"Código: {code}\n\n"
        "Ele vale por 20 minutos. Se você não pediu esta conta, ignore esta mensagem."
    )
    return send_notification("Código de verificação de e-mail", body, [email])


_layout_ref_sync()
