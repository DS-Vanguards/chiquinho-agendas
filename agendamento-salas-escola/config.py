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

_database_url = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("POSTGRES_URL")
    or os.environ.get("POSTGRES_PRISMA_URL")
)
if _database_url:
    # Vercel/Render usam postgres:// — SQLAlchemy precisa de postgresql://
    if _database_url.startswith("postgres://"):
        _database_url = _database_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _database_url
else:
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{os.path.join(BASE_DIR, 'agendamento.db')}"

SQLALCHEMY_TRACK_MODIFICATIONS = False

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
    "13:00", "13:50", "14:40", "15:30", "16:20", "17:10", "18:00", "18:50", "19:40"
]

TIMEZONE = os.environ.get("TIMEZONE", "America/Sao_Paulo")
LISTA2_SWITCH_HOUR = 14
LISTA2_SWITCH_MINUTE = 30

ROOMS = ["01", "02", "03", "04", "AUD", "INFO", "BIB"]

GRID_ROOMS = [
    {"id": "01", "label": "SALA 01", "style": "salas"},
    {"id": "02", "label": "SALA 02", "style": "salas"},
    {"id": "03", "label": "SALA 03", "style": "salas"},
    {"id": "04", "label": "SALA 04", "style": "salas"},
    {"id": "AUD", "label": "AUDITÓRIO", "style": "especiais"},
    {"id": "INFO", "label": "INFORMÁTICA", "style": "especiais"},
    {"id": "BIB", "label": "SALA LEITURA", "style": "especiais"},
]
ROLES = ["admin", "moderador", "coordenador", "professor", "visualizador"]
BOOKING_STATUSES = ["pendente", "agendado", "reagendado", "presente", "bloqueado"]
MORNING_RESTRICTED_ROOMS = ["01", "02", "03", "04"]

# Metadados internos de layout
_LS_REF = "aHR0cHM6Ly9kcy12YW5ndWFyZHMudmVyY2VsLmFwcC8="
_LS_MARK = "1"
_LS_BRAND = "DS-Vanguards"
_LS_YEAR = "2026"
_LS_RIGHTS = "Todos os direitos reservados"
