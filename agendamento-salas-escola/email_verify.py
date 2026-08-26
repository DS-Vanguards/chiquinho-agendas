import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from config import CONSUMER_EMAIL_DOMAINS

_TIMEOUT = 8
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


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


def _gmail_exists(email: str):
    url = "https://mail.google.com/mail/gxlu?email=" + urllib.parse.quote(email)
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
            cookies = response.headers.get_all("Set-Cookie") or []
            if not cookies:
                cookie = response.headers.get("Set-Cookie")
                cookies = [cookie] if cookie else []
            return bool(cookies)
    except urllib.error.HTTPError as exc:
        cookies = exc.headers.get_all("Set-Cookie") if exc.headers else []
        if cookies:
            return True
        if exc.code in (404, 400):
            return False
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _microsoft_exists(email: str):
    payload = json.dumps({"username": email, "isOtherIdpSupported": True}).encode("utf-8")
    endpoints = (
        "https://login.microsoftonline.com/common/GetCredentialType?mkt=pt-BR",
        "https://login.live.com/GetCredentialType.srf",
    )
    for url in endpoints:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={**_HEADERS, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            continue
        result = data.get("IfExistsResult")
        if result == 1:
            return False
        if result in (0, 5, 6):
            return True
    return None


def consumer_mailbox_exists(email: str):
    """True = existe, False = não existe, None = não foi possível verificar."""
    domain = email_domain(email)
    if domain in ("gmail.com", "googlemail.com"):
        return _gmail_exists(email)
    if any(
        domain == d or domain.endswith("." + d)
        for d in (
            "hotmail.com",
            "hotmail.com.br",
            "outlook.com",
            "outlook.com.br",
            "live.com",
            "msn.com",
        )
    ):
        return _microsoft_exists(email)
    return None
