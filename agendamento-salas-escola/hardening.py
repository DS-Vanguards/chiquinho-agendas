import os
import secrets
import time
from collections import deque
from threading import Lock

from flask import request, session
from werkzeug.middleware.proxy_fix import ProxyFix

RESET_SESSION_KEY = "pending_password_reset"
RESET_TTL_SEC = 15 * 60
LOGIN_ERROR = "Usuário ou senha incorretos."
GENERIC_REGISTER_ERROR = "Não foi possível concluir o cadastro. Verifique os dados."

_rate_lock = Lock()
_rate_hits: dict[str, deque] = {}


def is_production() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("RENDER"))


def load_secret_key(base_dir: str) -> str:
    key = (os.environ.get("SECRET_KEY") or "").strip()
    if key:
        return key
    if is_production():
        raise RuntimeError("Defina a variável de ambiente SECRET_KEY em produção.")
    path = os.path.join(base_dir, ".flask_secret")
    if os.path.isfile(path):
        stored = open(path, encoding="utf-8").read().strip()
        if stored:
            return stored
    generated = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(generated)
    return generated


def apply_proxy_fix(app) -> None:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def client_ip() -> str:
    return (request.remote_addr or "")[:64]


def too_many_requests(scope: str, limit: int, window_sec: int) -> bool:
    key = f"{scope}:{client_ip()}"
    now = time.time()
    with _rate_lock:
        queue = _rate_hits.setdefault(key, deque())
        while queue and queue[0] <= now - window_sec:
            queue.popleft()
        if len(queue) >= limit:
            return True
        queue.append(now)
        if len(_rate_hits) > 4000:
            stale = [item for item, hits in _rate_hits.items() if not hits or hits[-1] < now - 3600]
            for item in stale[:2000]:
                _rate_hits.pop(item, None)
        return False


def start_password_reset(user_id: int) -> None:
    session[RESET_SESSION_KEY] = {
        "uid": int(user_id),
        "exp": int(time.time()) + RESET_TTL_SEC,
    }
    session.modified = True


def pending_reset_user_id() -> int | None:
    data = session.get(RESET_SESSION_KEY) or {}
    uid = data.get("uid")
    exp = int(data.get("exp") or 0)
    if not uid or time.time() > exp:
        return None
    try:
        return int(uid)
    except (TypeError, ValueError):
        return None


def clear_password_reset() -> None:
    session.pop(RESET_SESSION_KEY, None)


def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    if request.is_secure or proto == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


def is_safe_email(value: str) -> bool:
    text = (value or "").strip().lower()
    if not text or "@" not in text:
        return False
    if any(char in text for char in ("\r", "\n", "\0", ",", ";")):
        return False
    local, _, domain = text.partition("@")
    return bool(local) and "." in domain and " " not in text
