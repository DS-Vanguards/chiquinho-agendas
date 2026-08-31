import os

from dotenv import load_dotenv

from hardening import is_production, load_secret_key

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

SECRET_KEY = load_secret_key(BASE_DIR)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = is_production()
PREFERRED_URL_SCHEME = "https" if is_production() else "http"
WTF_CSRF_SSL_STRICT = is_production()
WTF_CSRF_TIME_LIMIT = 3600


def _prepare_database_url(url: str) -> str:
    # Vercel/Render usam postgres:// — SQLAlchemy precisa de postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql") and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


_database_url = (
    os.environ.get("POSTGRES_URL_NON_POOLING")
    or os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_URL")
    or os.environ.get("POSTGRES_PRISMA_URL")
)
if _database_url:
    SQLALCHEMY_DATABASE_URI = _prepare_database_url(_database_url)
else:
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(BASE_DIR, 'agendamento.db')}"

SQLALCHEMY_TRACK_MODIFICATIONS = False

if SQLALCHEMY_DATABASE_URI.startswith("postgresql"):
    from sqlalchemy.pool import NullPool

    _pg_connect = {
        "sslmode": "require",
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
    if is_production() or os.environ.get("VERCEL"):
        # Serverless: não reutilizar conexão SSL morta entre invocações.
        SQLALCHEMY_ENGINE_OPTIONS = {
            "poolclass": NullPool,
            "connect_args": _pg_connect,
        }
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_pre_ping": True,
            "pool_recycle": 280,
            "pool_size": 5,
            "max_overflow": 2,
            "connect_args": _pg_connect,
        }

# E-mails institucionais permitidos (ajuste o domínio da sua escola)
ALLOWED_EMAIL_DOMAINS = [
    "al.educacao.sp.gov.br",
    "aluno.educacao.sp.gov.br",
    "prof.educacao.sp.gov.br",
    "professor.educacao.sp.gov.br",
]
PROFESSOR_AUTO_DOMAINS = [
    "prof.educacao.sp.gov.br",
    "professor.educacao.sp.gov.br",
]
CONSUMER_EMAIL_DOMAINS = [
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "hotmail.com.br",
    "outlook.com",
    "outlook.com.br",
    "live.com",
    "msn.com",
]

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@ds.vanguards.vercel.app")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# SMTP para notificações (opcional — deixe vazio para registrar no console)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "") or SMTP_USER
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

DEFAULT_TIME_LIST_1 = [
    "07:00", "07:50", "08:40", "09:30", "10:20", "11:10", "12:00", "12:50", "13:40"
]
DEFAULT_TIME_LIST_2 = [
    "14:40 - 15:30",
    "15:30 - 16:20",
    "16:20 - 17:10",
    "17:10 - 18:00",
    "Intervalo",
    "19:00 - 19:50",
    "19:50 - 20:40",
    "20:40 - 21:30",
]

TIMEZONE = os.environ.get("TIMEZONE", "America/Sao_Paulo")
LISTA2_SWITCH_HOUR = 14
LISTA2_SWITCH_MINUTE = 30

ROOMS_SALAS = [
    {"id": "01", "label": "SALA 01", "style": "salas"},
    {"id": "02", "label": "SALA 02", "style": "salas"},
    {"id": "03", "label": "SALA 03", "style": "salas"},
    {"id": "04", "label": "SALA 04", "style": "salas"},
]
ROOMS_ESPECIAIS = {
    "MAKER": {"id": "MAKER", "label": "MAKER", "style": "especiais"},
    "AUD": {"id": "AUD", "label": "AUDITÓRIO", "style": "especiais"},
    "INFO": {"id": "INFO", "label": "INFORMÁTICA", "style": "especiais"},
    "BIB": {"id": "BIB", "label": "SALA LEITURA", "style": "especiais"},
    "ARTES": {"id": "ARTES", "label": "SALA DE ARTES", "style": "especiais"},
}
GRID_ROOMS_BY_SHIFT = {
    "manha": [
        ROOMS_ESPECIAIS["MAKER"],
        ROOMS_ESPECIAIS["AUD"],
        ROOMS_ESPECIAIS["BIB"],
        ROOMS_ESPECIAIS["ARTES"],
    ],
    "tarde": [
        ROOMS_ESPECIAIS["AUD"],
        ROOMS_ESPECIAIS["INFO"],
        ROOMS_ESPECIAIS["BIB"],
        ROOMS_ESPECIAIS["ARTES"],
    ],
}


def _all_grid_rooms():
    seen = []
    ids = set()
    for rooms in GRID_ROOMS_BY_SHIFT.values():
        for room in rooms:
            if room["id"] not in ids:
                ids.add(room["id"])
                seen.append(room)
    return seen


GRID_ROOMS = _all_grid_rooms()
ROOMS = [room["id"] for room in GRID_ROOMS]
ROOM_LABELS = {room["id"]: room["label"] for room in GRID_ROOMS}
ROOM_LABELS.update({room["id"]: room["label"] for room in ROOMS_SALAS})
SHIFT_MANHA = "manha"
SHIFT_TARDE = "tarde"
SHIFT_AMBOS = "ambos"
SHIFTS = (SHIFT_MANHA, SHIFT_TARDE)
SHIFT_CHOICES = (SHIFT_MANHA, SHIFT_TARDE, SHIFT_AMBOS)
SHIFT_LABELS = {
    SHIFT_MANHA: "Turno 1",
    SHIFT_TARDE: "Turno 2",
    SHIFT_AMBOS: "T1/T2",
}
ROLES = [
    "visualizador",
    "professor",
    "coordenador",
    "moderador",
    "admin",
    "super_admin",
    "vgs_owner",
]
ROLE_LABELS = {
    "visualizador": "visualizador",
    "professor": "professor",
    "coordenador": "coordenador",
    "moderador": "moderador",
    "admin": "Admin",
    "super_admin": "Super Admin",
    "vgs_owner": "VGS-Owner's",
}
ROLE_RANK = {
    "visualizador": 0,
    "professor": 1,
    "coordenador": 2,
    "moderador": 3,
    "admin": 4,
    "super_admin": 5,
    "vgs_owner": 6,
}
ADMIN_ACCESS_ROLES = ("admin", "super_admin", "vgs_owner")
STAFF_ROLES = ("admin", "super_admin", "vgs_owner", "moderador")
BOOKING_STATUSES = [
    "pendente",
    "agendado",
    "reagendado",
    "presente",
    "bloqueado",
    "indisponivel",
]
# Metadados internos de layout
_LS_REF = "aHR0cHM6Ly9kcy12YW5ndWFyZHMudmVyY2VsLmFwcC8="
_LS_MARK = "1"
_LS_BRAND = "DS-Vanguards"
_LS_YEAR = "2026"
_LS_RIGHTS = "Todos os direitos reservados"
