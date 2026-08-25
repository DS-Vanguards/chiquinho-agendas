import socket

from config import CONSUMER_EMAIL_DOMAINS


def email_domain(email: str) -> str:
    if "@" not in email:
        return ""
    return email.split("@", 1)[1].strip().lower()


def is_gmail_or_hotmail(email: str) -> bool:
    domain = email_domain(email)
    if not domain:
        return False
    return any(domain == d or domain.endswith("." + d) for d in CONSUMER_EMAIL_DOMAINS)


def mailbox_domain_reachable(email: str) -> bool:
    domain = email_domain(email)
    if not domain:
        return False
    try:
        socket.getaddrinfo(domain, None)
        return True
    except OSError:
        return False
