import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    ADMIN_ACCESS_ROLES,
    ADMIN_EMAIL,
    ADMIN_PASSWORD,
    ADMIN_USERNAME,
    DEFAULT_TIME_LIST_1,
    DEFAULT_TIME_LIST_2,
    ROLE_RANK,
    ROLES,
    SHIFT_LABELS,
)
from core.layout_sig import layout_token as _layout_ref_sync
from extensions import db
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=True)
    role = db.Column(db.String(32), default="visualizador", nullable=False)
    must_reset_password = db.Column(db.Boolean, default=False, nullable=False)
    session_version = db.Column(db.Integer, default=0, nullable=False)
    email_verified = db.Column(db.Boolean, default=True, nullable=False)
    verification_code_hash = db.Column(db.String(256), nullable=True)
    verification_expires = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    shift = db.Column(db.String(16), nullable=True)

    bookings = db.relationship("Booking", backref="teacher", lazy=True)

    def get_id(self):
        return f"{self.id}:{int(self.session_version or 0)}"

    def bump_session(self) -> None:
        self.session_version = int(self.session_version or 0) + 1

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)
        self.must_reset_password = False
        self.bump_session()

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def issue_verification_code(self) -> str:
        import secrets

        code = f"{secrets.randbelow(1_000_000):06d}"
        self.email_verified = False
        self.verification_code_hash = generate_password_hash(code)
        self.verification_expires = datetime.utcnow() + timedelta(minutes=20)
        return code

    def check_verification_code(self, code: str) -> bool:
        if not code or not self.verification_code_hash or not self.verification_expires:
            return False
        if datetime.utcnow() > self.verification_expires:
            return False
        return check_password_hash(self.verification_code_hash, code.strip())

    def mark_email_verified(self) -> None:
        self.email_verified = True
        self.verification_code_hash = None
        self.verification_expires = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_super_admin(self) -> bool:
        return self.role == "super_admin"

    @property
    def is_vgs_owner(self) -> bool:
        return self.role == "vgs_owner"

    @property
    def is_moderador(self) -> bool:
        return self.role == "moderador"

    @property
    def is_professor(self) -> bool:
        return self.role == "professor"

    @property
    def is_coordenador(self) -> bool:
        return self.role == "coordenador"

    @property
    def is_visualizador(self) -> bool:
        return self.role == "visualizador"

    def role_rank(self) -> int:
        return ROLE_RANK.get(self.role, -1)

    def has_admin_access(self) -> bool:
        return self.role in ADMIN_ACCESS_ROLES

    def can_manage_users(self) -> bool:
        return self.role in ("admin", "super_admin", "vgs_owner", "moderador")

    def can_book(self) -> bool:
        return self.role in ("professor", "admin", "super_admin", "vgs_owner", "coordenador")

    def can_block_slots(self) -> bool:
        return self.role in ("coordenador", "vgs_owner")

    def can_choose_marking(self) -> bool:
        return self.role in ("coordenador", "vgs_owner")

    def can_mark_unavailable(self) -> bool:
        return self.role == "vgs_owner"

    def can_view_bookings(self) -> bool:
        return self.role in (
            "professor",
            "visualizador",
            "admin",
            "super_admin",
            "vgs_owner",
            "moderador",
            "coordenador",
        )

    def assignable_roles(self) -> list:
        if self.role == "admin":
            return [r for r in ROLES if ROLE_RANK.get(r, 99) < ROLE_RANK["admin"]]
        if self.role == "super_admin":
            return [r for r in ROLES if ROLE_RANK.get(r, 99) <= ROLE_RANK["super_admin"]]
        if self.role == "vgs_owner":
            return list(ROLES)
        return []

    def can_edit_user(self, other) -> bool:
        if other is None or other.id == self.id:
            return False
        if self.role == "vgs_owner":
            return True
        return self.role_rank() > ROLE_RANK.get(other.role, 99)

    def can_reset_user(self, other) -> bool:
        if other is None:
            return False
        if other.id == self.id:
            return True
        if self.role == "vgs_owner":
            return True
        if self.role == "moderador":
            return ROLE_RANK.get(other.role, 99) < ROLE_RANK["admin"]
        return self.role_rank() > ROLE_RANK.get(other.role, 99)

    def needs_shift_choice(self) -> bool:
        return self.role == "professor" and self.shift not in ("manha", "tarde", "ambos")

    def visible_shifts(self) -> list:
        if self.shift == "ambos":
            return ["manha", "tarde"]
        if self.shift in ("manha", "tarde"):
            return [self.shift]
        if self.role != "professor":
            return ["manha", "tarde"]
        return []

    def shift_label(self) -> str:
        if self.shift in SHIFT_LABELS:
            return SHIFT_LABELS[self.shift]
        if self.role != "professor":
            return SHIFT_LABELS["ambos"]
        return "Não definido"


class Booking(db.Model):
    __tablename__ = "bookings"

    id = db.Column(db.Integer, primary_key=True)
    room = db.Column(db.String(10), nullable=False)
    booking_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5), nullable=False)
    status = db.Column(db.String(20), default="agendado", nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint(
            "room", "booking_date", "start_time", name="unique_room_slot"
        ),
    )


class SystemConfig(db.Model):
    __tablename__ = "system_config"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text, nullable=False)

    @staticmethod
    def get(key: str, default=None):
        row = SystemConfig.query.filter_by(key=key).first()
        if not row:
            return default
        try:
            return json.loads(row.value)
        except json.JSONDecodeError:
            return row.value

    @staticmethod
    def set(key: str, value) -> None:
        serialized = json.dumps(value) if not isinstance(value, str) else value
        row = SystemConfig.query.filter_by(key=key).first()
        if row:
            row.value = serialized
        else:
            db.session.add(SystemConfig(key=key, value=serialized))

    @staticmethod
    def get_time_config():
        config = SystemConfig.get("time_slots")
        if not config:
            config = {
                "lista1": DEFAULT_TIME_LIST_1,
                "lista2": DEFAULT_TIME_LIST_2,
            }
            SystemConfig.set("time_slots", config)
            db.session.commit()
        if not isinstance(config, dict):
            config = {}
        config.setdefault("lista1", list(DEFAULT_TIME_LIST_1))
        config.setdefault("lista2", list(DEFAULT_TIME_LIST_2))
        lista2 = config.get("lista2") or []
        if not any(
            " - " in str(item) or str(item).strip().lower() == "intervalo"
            for item in lista2
        ):
            config["lista2"] = list(DEFAULT_TIME_LIST_2)
            SystemConfig.set("time_slots", config)
            db.session.commit()
        return config

    @staticmethod
    def get_time_slots_for_shift(shift: str):
        config = SystemConfig.get_time_config()
        if shift == "tarde":
            return config.get("lista2", DEFAULT_TIME_LIST_2)
        return config.get("lista1", DEFAULT_TIME_LIST_1)

    @staticmethod
    def is_auto_professor_enabled() -> bool:
        value = SystemConfig.get("auto_professor_role", True)
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("1", "true", "yes")

    @staticmethod
    def set_auto_professor_enabled(enabled: bool) -> None:
        SystemConfig.set("auto_professor_role", bool(enabled))


class NotificationEmail(db.Model):
    __tablename__ = "notification_emails"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)


class MachineGuard(db.Model):
    __tablename__ = "machine_guards"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    ip_address = db.Column(db.String(64), nullable=False, index=True)
    attempt_count = db.Column(db.Integer, default=0, nullable=False)
    strike_count = db.Column(db.Integer, default=0, nullable=False)
    lock_level = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > datetime.utcnow())

    def is_severe_lock(self) -> bool:
        return self.is_locked() and self.lock_level >= 2

    def token_short(self) -> str:
        return (self.token or "")[:10]


def ensure_schema() -> None:
    """Ajusta colunas antigas. create_all não altera tabelas existentes."""
    dialect = db.engine.dialect.name
    if dialect == "postgresql":
        statements = [
            "ALTER TABLE bookings ALTER COLUMN room TYPE VARCHAR(20)",
            "ALTER TABLE bookings ALTER COLUMN status TYPE VARCHAR(20)",
            "ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(32)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT TRUE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_code_hash VARCHAR(256)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_expires TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS shift VARCHAR(16)",
        ]
    else:
        statements = [
            "ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT 1",
            "ALTER TABLE users ADD COLUMN verification_code_hash VARCHAR(256)",
            "ALTER TABLE users ADD COLUMN verification_expires DATETIME",
            "ALTER TABLE users ADD COLUMN session_version INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN shift VARCHAR(16)",
        ]
    for sql in statements:
        try:
            db.session.execute(text(sql))
            db.session.commit()
        except Exception:
            db.session.rollback()


def init_default_data():
    try:
        admin_exists = User.query.filter(
            (User.username == ADMIN_USERNAME) | (User.email == ADMIN_EMAIL)
        ).first()
        if not admin_exists and ADMIN_PASSWORD:
            admin = User(
                username=ADMIN_USERNAME,
                email=ADMIN_EMAIL,
                role="admin",
            )
            admin.set_password(ADMIN_PASSWORD)
            db.session.add(admin)

        if not SystemConfig.query.filter_by(key="time_slots").first():
            SystemConfig.set(
                "time_slots",
                {
                    "lista1": DEFAULT_TIME_LIST_1,
                    "lista2": DEFAULT_TIME_LIST_2,
                },
            )

        if SystemConfig.query.filter_by(key="auto_professor_role").first() is None:
            SystemConfig.set("auto_professor_role", True)

        if User.query.filter_by(role="vgs_owner").first() is None:
            founder = User.query.filter(
                (User.username == ADMIN_USERNAME) | (User.email == ADMIN_EMAIL)
            ).first()
            if founder:
                founder.role = "vgs_owner"

        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    except Exception:
        db.session.rollback()
    _layout_ref_sync()
