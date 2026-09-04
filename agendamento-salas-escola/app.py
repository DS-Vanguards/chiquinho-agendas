import hashlib
import json
import os
import re
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    flash,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf.csrf import CSRFProtect, generate_csrf
from sqlalchemy.exc import IntegrityError
from sqlalchemy import event, func, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import joinedload

import config
from core.layout_sig import bind as _layout_bind
from email_utils import send_notification
from extensions import db, recover_db_session, retry_on_disconnect, _DISCONNECT_ERRORS
from hardening import (
    LOGIN_ERROR,
    add_security_headers,
    apply_proxy_fix,
    clear_password_reset,
    client_ip,
    is_safe_email,
    pending_reset_user_id,
    start_password_reset,
    too_many_requests,
)
from models import (
    Booking,
    MachineGuard,
    NotificationEmail,
    SystemConfig,
    User,
    ensure_schema,
    init_default_data,
)

app = Flask(__name__)
app.config.from_object(config)
apply_proxy_fix(app)
CSRFProtect(app)
_layout_bind(app)

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Faça login para continuar."
app.config.setdefault("SEND_FILE_MAX_AGE_DEFAULT", 120)


@event.listens_for(Engine, "connect")
def _sqlite_fast_pragmas(dbapi_connection, _connection_record):
    module = (type(dbapi_connection).__module__ or "").lower()
    name = type(dbapi_connection).__name__.lower()
    if "sqlite" not in module and "sqlite" not in name:
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _should_migrate_schema() -> bool:
    if os.environ.get("RUN_DB_MIGRATE", "").lower() in ("1", "true", "yes"):
        return True
    return not os.environ.get("VERCEL")


@app.before_request
def _revive_db_connection():
    if request.endpoint in ("static", "favicon"):
        return
    if not str(app.config.get("SQLALCHEMY_DATABASE_URI", "")).startswith("postgresql"):
        return
    engine_opts = app.config.get("SQLALCHEMY_ENGINE_OPTIONS") or {}
    if engine_opts.get("pool_pre_ping") or engine_opts.get("poolclass") is not None:
        return
    try:
        db.session.execute(text("SELECT 1"))
    except _DISCONNECT_ERRORS:
        recover_db_session()


@app.before_request
def _require_professor_shift():
    if not getattr(current_user, "is_authenticated", False):
        return
    if request.endpoint in (
        "escolher_turno",
        "logout",
        "static",
        "favicon",
        "set_password",
    ):
        return
    if current_user.needs_shift_choice():
        return redirect(url_for("escolher_turno"))


@login_manager.user_loader
def load_user(user_id):
    raw = str(user_id or "")
    try:
        if ":" in raw:
            uid, version = raw.split(":", 1)
        else:
            uid, version = raw, None
        uid = int(uid)
    except (TypeError, ValueError):
        return None

    def _load():
        user = db.session.get(User, uid)
        if user is None:
            return None
        if version is not None and str(int(user.session_version or 0)) != str(version):
            return None
        return user

    try:
        return _load()
    except _DISCONNECT_ERRORS:
        recover_db_session()
        try:
            return _load()
        except _DISCONNECT_ERRORS:
            return None


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                flash("Você não tem permissão para acessar esta página.", "error")
                return redirect(url_for(get_home_endpoint()))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def validate_institutional_email(email: str) -> bool:
    email = email.strip().lower()
    if "@" not in email:
        return False
    domain = email.split("@", 1)[1]
    return any(
        domain == d or domain.endswith("." + d) for d in config.ALLOWED_EMAIL_DOMAINS
    )


def allowed_email_domains_text() -> str:
    return ", ".join("@" + d for d in config.ALLOWED_EMAIL_DOMAINS)


def duplicate_account_message(username: str, email: str) -> str | None:
    name_taken = User.query.filter_by(username=username).first() is not None
    email_taken = User.query.filter_by(email=email).first() is not None
    if name_taken and email_taken:
        return "Já existe uma conta com este nome e este e-mail."
    if name_taken:
        return "Este nome de usuário já está em uso."
    if email_taken:
        return "Este e-mail já está cadastrado."
    return None


MACHINE_COOKIE = "dv_mid"
REGISTER_ATTEMPT_LIMIT = 4
DS_SUPPORT_URL = "https://ds-vanguards.vercel.app/"


def _client_ip() -> str:
    return client_ip() or "0.0.0.0"


def _new_machine_token() -> str:
    return uuid.uuid4().hex


@retry_on_disconnect
def get_or_create_machine_guard(persist: bool = True):
    ip = _client_ip()
    token = request.cookies.get(MACHINE_COOKIE, "").strip()
    guard = None
    if token:
        guard = MachineGuard.query.filter_by(token=token).first()
    if not guard:
        active_on_ip = (
            MachineGuard.query.filter(
                MachineGuard.ip_address == ip,
                MachineGuard.locked_until != None,  # noqa: E711
                MachineGuard.locked_until > datetime.utcnow(),
            )
            .order_by(MachineGuard.locked_until.desc())
            .first()
        )
        if active_on_ip:
            guard = active_on_ip
            token = guard.token
    if not guard:
        token = token or _new_machine_token()
        guard = MachineGuard(token=token, ip_address=ip)
        if persist:
            db.session.add(guard)
            db.session.commit()
    elif persist and guard.ip_address != ip:
        guard.ip_address = ip
        guard.updated_at = datetime.utcnow()
        db.session.commit()
    return guard


def attach_machine_cookie(response, token: str):
    response.set_cookie(
        MACHINE_COOKIE,
        token,
        max_age=60 * 60 * 24 * 400,
        httponly=True,
        samesite="Lax",
        secure=request.is_secure or request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https",
        path="/",
    )
    return response


def apply_register_lock(guard: MachineGuard) -> None:
    now = datetime.utcnow()
    guard.strike_count = int(guard.strike_count or 0) + 1
    if guard.strike_count >= 6:
        guard.lock_level = 2
        guard.locked_until = now + timedelta(days=1)
    else:
        guard.lock_level = 1
        guard.locked_until = now + timedelta(minutes=5)
    guard.attempt_count = 0
    guard.updated_at = now
    db.session.commit()


def local_timezone():
    try:
        return ZoneInfo(config.TIMEZONE)
    except Exception:
        return timezone(timedelta(hours=-3))


def format_lock_until(value) -> str:
    if not value:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(local_timezone())
    return local.strftime("%d/%m/%Y %H:%M")


def register_lock_response(guard: MachineGuard):
    severe = guard.is_severe_lock()
    response = make_response(
        render_template(
            "register.html",
            register_blocked=True,
            block_severe=severe,
            support_url=DS_SUPPORT_URL,
        )
    )
    return attach_machine_cookie(response, guard.token)


def parse_booking_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def times_overlap(start1, end1, start2, end2) -> bool:
    return start1 < end2 and start2 < end1


def get_home_endpoint(user=None) -> str:
    user = user or current_user
    if user.is_authenticated and user.needs_shift_choice():
        return "escolher_turno"
    if user.is_authenticated and user.is_professor:
        return "agendamentos"
    return "dashboard"


def rooms_for_shift(shift: str) -> list:
    return [room["id"] for room in config.GRID_ROOMS_BY_SHIFT.get(shift, ())]


def user_can_use_shift(user, shift: str) -> bool:
    return shift in (user.visible_shifts() if user else [])


def role_label(role: str) -> str:
    return config.ROLE_LABELS.get(role, role)


def is_last_of_role(role: str) -> bool:
    return User.query.filter_by(role=role).count() <= 1


def email_domain(email: str) -> str:
    if "@" not in email:
        return ""
    return email.split("@", 1)[1]


def should_auto_assign_professor(email: str) -> bool:
    if not SystemConfig.is_auto_professor_enabled():
        return False
    domain = email_domain(email)
    return any(
        domain == d or domain.endswith("." + d)
        for d in config.PROFESSOR_AUTO_DOMAINS
    )


def notify_booking_async(subject: str, body: str) -> None:
    recipients = [e.email for e in NotificationEmail.query.all()]
    if not recipients:
        return

    def _send():
        try:
            send_notification(subject, body, recipients)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


@app.context_processor
def inject_globals():
    home = "login"
    if current_user.is_authenticated:
        home = get_home_endpoint()
    return {
        "home_endpoint": home,
        "ROLES": config.ROLES,
        "ROOMS": config.ROOMS,
        "GRID_ROOMS": config.GRID_ROOMS,
        "ROOM_LABELS": config.ROOM_LABELS,
        "ROLE_LABELS": config.ROLE_LABELS,
        "SHIFT_LABELS": config.SHIFT_LABELS,
        "STATUSES": config.BOOKING_STATUSES,
        "csrf_token": generate_csrf,
    }


@app.after_request
def set_security_headers(response):
    response = add_security_headers(response)
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = (
            "public, max-age=120, stale-while-revalidate=86400"
        )
    return response


WEEKDAYS_PT = [
    "SEGUNDA-FEIRA",
    "TERÇA-FEIRA",
    "QUARTA-FEIRA",
    "QUINTA-FEIRA",
    "SEXTA-FEIRA",
    "SÁBADO",
    "DOMINGO",
]


def get_selected_date(filter_date_str=None) -> date:
    if filter_date_str:
        parsed = parse_booking_date(filter_date_str)
        if parsed:
            return parsed
    return date.today()


def format_date_label(selected: date) -> str:
    weekday = WEEKDAYS_PT[selected.weekday()]
    return f"{selected.strftime('%d/%m/%Y')} {weekday}"


def _normalize_hhmm(value: str) -> str:
    parts = value.strip().split(":")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


def parse_shift_rows(lines) -> list:
    items = [str(item).strip() for item in (lines or []) if str(item).strip()]
    if not items:
        return []

    if all(re.fullmatch(r"\d{1,2}:\d{2}", item) for item in items):
        rows = []
        aula = 0
        for index in range(len(items) - 1):
            start = _normalize_hhmm(items[index])
            end = _normalize_hhmm(items[index + 1])
            if start >= end:
                continue
            aula += 1
            rows.append(
                {
                    "kind": "aula",
                    "aula": aula,
                    "start_time": start,
                    "end_time": end,
                    "label": f"{start} - {end}",
                }
            )
        return rows

    rows = []
    aula = 0
    range_re = re.compile(r"^(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})$")
    for item in items:
        if item.lower() == "intervalo":
            rows.append(
                {
                    "kind": "intervalo",
                    "aula": "",
                    "start_time": "",
                    "end_time": "",
                    "label": "Intervalo",
                }
            )
            continue
        match = range_re.match(item)
        if not match:
            continue
        start = _normalize_hhmm(match.group(1))
        end = _normalize_hhmm(match.group(2))
        if start >= end:
            continue
        aula += 1
        rows.append(
            {
                "kind": "aula",
                "aula": aula,
                "start_time": start,
                "end_time": end,
                "label": f"{start} - {end}",
            }
        )
    return rows


def periods_for_shift(shift: str) -> list:
    bucket = getattr(g, "_periods_for_shift", None)
    if bucket is None:
        bucket = {}
        try:
            g._periods_for_shift = bucket
        except RuntimeError:
            pass
    cached = bucket.get(shift)
    if cached is not None:
        return cached
    rows = parse_shift_rows(SystemConfig.get_time_slots_for_shift(shift))
    result = [(row["start_time"], row["end_time"]) for row in rows if row["kind"] == "aula"]
    bucket[shift] = result
    return result


def classify_booking_shift(booking) -> str:
    key = (booking.start_time, booking.end_time)
    manha_periods = set(periods_for_shift("manha"))
    tarde_periods = set(periods_for_shift("tarde"))
    in_manha = key in manha_periods
    in_tarde = key in tarde_periods
    if in_manha and not in_tarde:
        return "manha"
    if in_tarde and not in_manha:
        return "tarde"
    rooms_manha = set(rooms_for_shift("manha"))
    rooms_tarde = set(rooms_for_shift("tarde"))
    if booking.room in rooms_tarde and booking.room not in rooms_manha:
        return "tarde"
    if booking.room in rooms_manha and booking.room not in rooms_tarde:
        return "manha"
    if in_tarde:
        return "tarde"
    return "manha"


def split_bookings_by_shift(bookings):
    manha = []
    tarde = []
    for booking in bookings:
        if classify_booking_shift(booking) == "tarde":
            tarde.append(booking)
        else:
            manha.append(booking)
    return manha, tarde


def _live_revision(payload) -> str:
    raw = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:20]


def serialize_live_booking(booking) -> dict:
    teacher = getattr(booking, "teacher", None)
    return {
        "id": booking.id,
        "room": booking.room,
        "room_label": config.ROOM_LABELS.get(booking.room, booking.room),
        "booking_date": booking.booking_date.isoformat() if booking.booking_date else "",
        "date_label": booking.booking_date.strftime("%d/%m/%Y") if booking.booking_date else "",
        "start_time": booking.start_time,
        "end_time": booking.end_time,
        "status": booking.status,
        "username": teacher.username if teacher else "",
        "teacher_id": booking.teacher_id,
        "shift": classify_booking_shift(booking),
    }


def build_shift_context(selected_date: date, shift: str, grid=None):
    rows = parse_shift_rows(SystemConfig.get_time_slots_for_shift(shift))
    if grid is None:
        bookings = (
            Booking.query.options(joinedload(Booking.teacher))
            .filter_by(booking_date=selected_date)
            .all()
        )
        grid = {
            (booking.room, booking.start_time, booking.end_time): booking
            for booking in bookings
        }

    return {
        "shift": shift,
        "shift_label": config.SHIFT_LABELS.get(shift, shift),
        "grid_rooms": config.GRID_ROOMS_BY_SHIFT.get(shift, []),
        "schedule_rows": rows,
        "schedule_grid": grid,
    }


def build_schedule_context(selected_date: date, user=None):
    user = user or current_user
    prev_date = (selected_date - timedelta(days=1)).isoformat()
    next_date = (selected_date + timedelta(days=1)).isoformat()
    shifts = user.visible_shifts()
    bookings = (
        Booking.query.options(joinedload(Booking.teacher))
        .filter_by(booking_date=selected_date)
        .all()
    )
    grid = {
        (booking.room, booking.start_time, booking.end_time): booking
        for booking in bookings
    }
    views = [build_shift_context(selected_date, shift, grid) for shift in shifts]
    return {
        "selected_date": selected_date,
        "date_label": format_date_label(selected_date),
        "filter_date": selected_date.isoformat(),
        "prev_date": prev_date,
        "next_date": next_date,
        "schedule_views": views,
    }


def create_booking(room, booking_date, start_time, end_time, teacher_id, status="agendado", shift=None):
    if shift not in config.SHIFTS:
        shift = None
        for candidate in config.SHIFTS:
            if (
                (start_time, end_time) in periods_for_shift(candidate)
                and room in rooms_for_shift(candidate)
            ):
                shift = candidate
                break
    if shift not in config.SHIFTS:
        return False, "Turno inválido.", None
    if not user_can_use_shift(current_user, shift):
        return False, "Você não pode agendar neste turno.", None

    periods = periods_for_shift(shift)
    allowed_rooms = rooms_for_shift(shift)

    if room not in allowed_rooms:
        return False, "Sala inválida para este turno.", None
    if not booking_date:
        return False, "Data inválida.", None
    if booking_date < date.today():
        return False, "Não é possível agendar em datas passadas.", None
    if (start_time, end_time) not in periods:
        return False, "Horário inválido.", None

    query = Booking.query.filter_by(room=room, booking_date=booking_date)
    if db.engine.dialect.name != "sqlite":
        query = query.with_for_update()
    conflict = query.all()
    if any(
        times_overlap(start_time, end_time, b.start_time, b.end_time) for b in conflict
    ):
        return False, "Já existe um agendamento neste horário para esta sala.", None

    booking = Booking(
        room=room,
        booking_date=booking_date,
        start_time=start_time,
        end_time=end_time,
        status=status,
        teacher_id=teacher_id,
    )
    db.session.add(booking)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return False, "Já existe um agendamento neste horário para esta sala.", None

    if status not in ("bloqueado", "indisponivel"):
        teacher = db.session.get(User, teacher_id)
        body = (
            f"Novo agendamento registrado:\n\n"
            f"Professor: {teacher.username if teacher else teacher_id}\n"
            f"Sala: {room}\n"
            f"Data: {booking_date.strftime('%d/%m/%Y')}\n"
            f"Horário: {start_time} às {end_time}\n"
            f"Status: {status}"
        )
        notify_booking_async("Novo agendamento de sala", body)

    if status == "bloqueado":
        message = "Horário bloqueado."
    elif status == "indisponivel":
        message = "Horário marcado como indisponível."
    else:
        message = "Agendamento realizado com sucesso!"
    return True, message, booking


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for(get_home_endpoint()))
    return redirect(url_for("login"))


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static", "images"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for(get_home_endpoint()))

    if request.method == "POST":
        if too_many_requests("login", 8, 15 * 60):
            flash("Muitas tentativas. Aguarde alguns minutos e tente de novo.", "error")
            return render_template("login.html")

        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()

        if not user:
            flash(LOGIN_ERROR, "error")
            return render_template("login.html")

        if user.must_reset_password or not user.password_hash:
            start_password_reset(user.id)
            return render_template("set_password.html", user=user, forced=True)

        if not user.check_password(password):
            flash(LOGIN_ERROR, "error")
            return render_template("login.html")

        login_user(user)
        flash(f"Bem-vindo(a), {user.username}!", "success")
        return redirect(url_for(get_home_endpoint(user)))

    return render_template("login.html")


@app.route("/turno", methods=["GET", "POST"])
@login_required
def escolher_turno():
    if not current_user.needs_shift_choice():
        return redirect(url_for("agendamentos" if current_user.is_professor else "dashboard"))

    if request.method == "POST":
        shift = request.form.get("shift", "")
        if shift not in config.SHIFTS:
            flash("Escolha o turno.", "error")
            return render_template("escolher_turno.html")
        current_user.shift = shift
        db.session.commit()
        flash(f"{config.SHIFT_LABELS[shift]} definido.", "success")
        return redirect(url_for(get_home_endpoint(current_user)))

    return render_template("escolher_turno.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for(get_home_endpoint()))

    try:
        guard = get_or_create_machine_guard(persist=False)
    except _DISCONNECT_ERRORS:
        recover_db_session()
        try:
            guard = get_or_create_machine_guard(persist=False)
        except _DISCONNECT_ERRORS:
            recover_db_session()
            token = request.cookies.get(MACHINE_COOKIE, "").strip() or _new_machine_token()
            guard = MachineGuard(token=token, ip_address=_client_ip())
    if guard.is_locked():
        return register_lock_response(guard)

    if request.method == "POST":
        if too_many_requests("register", 8, 15 * 60):
            flash("Muitas tentativas. Aguarde alguns minutos e tente de novo.", "error")
            response = make_response(render_template("register.html", register_blocked=False))
            return attach_machine_cookie(response, guard.token)

        guard = get_or_create_machine_guard(persist=True)
        if guard.id is None:
            db.session.add(guard)
            db.session.commit()
        guard.attempt_count = (guard.attempt_count or 0) + 1
        guard.updated_at = datetime.utcnow()
        db.session.commit()
        if guard.attempt_count >= REGISTER_ATTEMPT_LIMIT:
            apply_register_lock(guard)
            return register_lock_response(guard)

        username = request.form.get("username", "").strip()[:80]
        email = request.form.get("email", "").strip().lower()[:120]
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if len(username) < 3:
            flash("Nome de usuário deve ter pelo menos 3 caracteres.", "error")
        elif not validate_institutional_email(email):
            flash(
                "Use um e-mail dos domínios permitidos: "
                + allowed_email_domains_text()
                + ".",
                "error",
            )
        elif len(password) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.", "error")
        elif password != confirm:
            flash("As senhas não coincidem.", "error")
        else:
            conflict = duplicate_account_message(username, email)
            if conflict:
                flash(conflict, "error")
            else:
                role = "professor" if should_auto_assign_professor(email) else "visualizador"
                user = User(username=username, email=email, role=role, email_verified=True)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                if role == "professor":
                    flash("Cadastro realizado como professor! Faça login para continuar.", "success")
                else:
                    flash("Cadastro realizado! Faça login para continuar.", "success")
                response = make_response(redirect(url_for("login")))
                return attach_machine_cookie(response, guard.token)

    response = make_response(render_template("register.html", register_blocked=False))
    return attach_machine_cookie(response, guard.token)


@app.route("/set-password", methods=["POST"])
def set_password():
    if too_many_requests("set-password", 8, 15 * 60):
        flash("Muitas tentativas. Aguarde alguns minutos e tente de novo.", "error")
        return redirect(url_for("login"))

    uid = pending_reset_user_id()
    user = db.session.get(User, uid) if uid else None
    if not user or not (user.must_reset_password or not user.password_hash):
        flash("Esta conta não está aguardando redefinição de senha.", "error")
        return redirect(url_for("login"))

    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    if len(password) < 6:
        flash("A senha deve ter pelo menos 6 caracteres.", "error")
        return render_template("set_password.html", user=user, forced=True)

    if password != confirm:
        flash("As senhas não coincidem.", "error")
        return render_template("set_password.html", user=user, forced=True)

    user.set_password(password)
    db.session.commit()
    clear_password_reset()
    login_user(user)
    flash("Senha definida com sucesso!", "success")
    return redirect(url_for(get_home_endpoint(user)))

@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_delete_user(user_id):
    user = db.session.get(User, user_id)
    if user:
        if user.id == current_user.id:
            flash("Você não pode excluir sua própria conta.", "error")
            return redirect(url_for("admin_panel"))
        if not current_user.can_edit_user(user):
            flash("Você não pode excluir este usuário.", "error")
            return redirect(url_for("admin_panel"))
        if user.role == "vgs_owner" and is_last_of_role("vgs_owner"):
            flash("Não é possível excluir o último VGS-Owner's.", "error")
            return redirect(url_for("admin_panel"))
            
        try:
            # Remove agendamentos vinculados para evitar erros no banco
            Booking.query.filter_by(teacher_id=user.id).delete()
            
            # Remove o usuário
            db.session.delete(user)
            db.session.commit()
            flash(f"Usuário {user.username} e seus agendamentos foram removidos com sucesso.", "success")
        except Exception as e:
            db.session.rollback()
            flash("Erro ao excluir o usuário no banco de dados.", "error")
    else:
        flash("Usuário não encontrado.", "error")
        
    return redirect(url_for("admin_panel"))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Você saiu do sistema.", "info")
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.is_professor:
        return redirect(url_for("agendamentos"))

    if current_user.is_visualizador:
        return render_template("dashboard.html", acesso_negado=True)

    context = {}
    if current_user.role in (
        "admin",
        "super_admin",
        "vgs_owner",
        "moderador",
        "coordenador",
        "inspetor",
    ):
        selected_date = get_selected_date(request.args.get("date"))
        context = build_schedule_context(selected_date)
    return render_template("dashboard.html", **context)


@app.route("/agendar")
@role_required("professor")
def agendar():
    return redirect(url_for("agendamentos"))


@app.route("/agendamentos")
@login_required
def agendamentos():
    if not current_user.can_view_bookings():
        flash("Você não tem permissão para ver agendamentos.", "error")
        return redirect(url_for(get_home_endpoint()))

    selected_date = get_selected_date(request.args.get("date"))

    if current_user.is_visualizador:
        context = build_schedule_context(selected_date)
        return render_template(
            "agendamentos.html",
            view_mode="restricted",
            **context,
        )

    if current_user.role in (
        "admin",
        "super_admin",
        "vgs_owner",
        "moderador",
        "coordenador",
        "inspetor",
    ):
        bookings = (
            Booking.query.options(joinedload(Booking.teacher))
            .filter_by(booking_date=selected_date)
            .order_by(Booking.start_time, Booking.room)
            .all()
        )
        bookings_manha, bookings_tarde = split_bookings_by_shift(bookings)
        detailed_shifts = ["manha", "tarde"]
        if current_user.is_inspetor:
            detailed_shifts = current_user.visible_shifts()
            if "manha" not in detailed_shifts:
                bookings_manha = []
            if "tarde" not in detailed_shifts:
                bookings_tarde = []
        prev_date = (selected_date - timedelta(days=1)).isoformat()
        next_date = (selected_date + timedelta(days=1)).isoformat()

        return render_template(
            "agendamentos.html",
            view_mode="detailed",
            bookings_manha=bookings_manha,
            bookings_tarde=bookings_tarde,
            detailed_shifts=detailed_shifts,
            filter_date=selected_date.isoformat(),
            date_label=format_date_label(selected_date),
            prev_date=prev_date,
            next_date=next_date,
        )

    context = build_schedule_context(selected_date)
    return render_template("agendamentos.html", view_mode="grid", **context)


@app.route("/agendamentos/agendar-rapido", methods=["POST"])
@role_required("professor", "admin", "super_admin", "vgs_owner", "coordenador")
def agendar_rapido():
    if too_many_requests("book", 80, 10 * 60):
        return jsonify({"success": False, "message": "Muitos agendamentos em pouco tempo. Tente de novo em instantes."}), 429

    room = request.form.get("room", "")
    booking_date = parse_booking_date(request.form.get("booking_date", ""))
    start_time = request.form.get("start_time", "")
    end_time = request.form.get("end_time", "")
    shift = request.form.get("shift", "")

    success, message, booking = create_booking(
        room, booking_date, start_time, end_time, current_user.id, shift=shift
    )
    status_code = 200 if success else 400
    payload = {"success": success, "message": message}
    if success and booking:
        payload.update(
            {
                "booking_id": booking.id,
                "username": current_user.username,
                "status": booking.status,
            }
        )
    return jsonify(payload), status_code


@app.route("/agendamentos/<int:booking_id>/cancelar", methods=["POST"])
@role_required("professor", "admin", "super_admin", "vgs_owner", "coordenador")
def cancelar_agendamento(booking_id):
    booking = db.session.get(Booking, booking_id)
    if not booking:
        return jsonify({"success": False, "message": "Agendamento não encontrado."}), 404

    if booking.teacher_id != current_user.id:
        return jsonify(
            {"success": False, "message": "Somente o autor pode cancelar o agendamento."}
        ), 403

    if booking.status in ("presente", "bloqueado", "indisponivel"):
        return jsonify(
            {
                "success": False,
                "message": "Não é possível cancelar este horário.",
            }
        ), 400

    db.session.delete(booking)
    db.session.commit()
    return jsonify({"success": True, "message": "Agendamento cancelado com sucesso!"})


@app.route("/agendamentos/bloquear", methods=["POST"])
@role_required("coordenador", "vgs_owner")
def bloquear_horario():
    room = request.form.get("room", "")
    booking_date = parse_booking_date(request.form.get("booking_date", ""))
    start_time = request.form.get("start_time", "")
    end_time = request.form.get("end_time", "")
    shift = request.form.get("shift", "")

    success, message, booking = create_booking(
        room,
        booking_date,
        start_time,
        end_time,
        current_user.id,
        status="bloqueado",
        shift=shift,
    )
    status_code = 200 if success else 400
    payload = {"success": success, "message": message}
    if success and booking:
        payload.update({"booking_id": booking.id, "status": "bloqueado"})
    return jsonify(payload), status_code


@app.route("/agendamentos/indisponivel", methods=["POST"])
@role_required("vgs_owner")
def marcar_indisponivel():
    room = request.form.get("room", "")
    booking_date = parse_booking_date(request.form.get("booking_date", ""))
    start_time = request.form.get("start_time", "")
    end_time = request.form.get("end_time", "")
    shift = request.form.get("shift", "")

    success, message, booking = create_booking(
        room,
        booking_date,
        start_time,
        end_time,
        current_user.id,
        status="indisponivel",
        shift=shift,
    )
    status_code = 200 if success else 400
    payload = {"success": success, "message": message}
    if success and booking:
        payload.update({"booking_id": booking.id, "status": "indisponivel"})
    return jsonify(payload), status_code


@app.route("/agendamentos/<int:booking_id>/desbloquear", methods=["POST"])
@role_required("coordenador", "vgs_owner")
def desbloquear_horario(booking_id):
    booking = db.session.get(Booking, booking_id)
    if not booking:
        return jsonify({"success": False, "message": "Bloqueio não encontrado."}), 404

    if booking.status == "bloqueado":
        if not current_user.can_block_slots():
            return jsonify({"success": False, "message": "Sem permissão para desbloquear."}), 403
        message = "Horário desbloqueado."
    elif booking.status == "indisponivel":
        if not current_user.can_mark_unavailable():
            return jsonify({"success": False, "message": "Sem permissão para liberar este horário."}), 403
        message = "Horário liberado."
    else:
        return jsonify({"success": False, "message": "Bloqueio não encontrado."}), 404

    db.session.delete(booking)
    db.session.commit()
    return jsonify({"success": True, "message": message})


@app.route("/live/agenda")
@login_required
def live_agenda():
    if not current_user.can_view_bookings():
        return jsonify({"success": False, "message": "Sem permissão."}), 403
    if too_many_requests("live-agenda", 90, 60):
        return jsonify({"unchanged": True}), 200

    selected_date = get_selected_date(request.args.get("date"))
    bookings = (
        Booking.query.options(joinedload(Booking.teacher))
        .filter_by(booking_date=selected_date)
        .order_by(Booking.start_time, Booking.room)
        .all()
    )
    revision = _live_revision(
        [
            (
                b.id,
                b.room,
                b.start_time,
                b.end_time,
                b.status,
                b.teacher_id,
            )
            for b in bookings
        ]
    )
    if request.args.get("rev") == revision:
        return jsonify({"unchanged": True, "revision": revision})
    items = [serialize_live_booking(b) for b in bookings]
    manha = [b for b in items if b["shift"] != "tarde"]
    tarde = [b for b in items if b["shift"] == "tarde"]
    return jsonify(
        {
            "unchanged": False,
            "revision": revision,
            "date": selected_date.isoformat(),
            "bookings": items,
            "bookings_manha": manha,
            "bookings_tarde": tarde,
        }
    )


@app.route("/live/admin")
@login_required
@role_required(*config.STAFF_ROLES)
def live_admin():
    if too_many_requests("live-admin", 90, 60):
        return jsonify({"unchanged": True}), 200

    booking_sig = db.session.query(
        Booking.id,
        Booking.status,
        Booking.room,
        Booking.start_time,
        Booking.end_time,
        Booking.booking_date,
        Booking.teacher_id,
    ).order_by(Booking.id).all()
    user_sig = (
        db.session.query(User.id, User.role, User.shift, User.session_version)
        .order_by(User.id)
        .all()
    )
    machine_sig = db.session.query(
        func.count(MachineGuard.id), func.max(MachineGuard.locked_until)
    ).filter(
        MachineGuard.locked_until != None,  # noqa: E711
        MachineGuard.locked_until > datetime.utcnow(),
    ).one()
    revision = _live_revision(
        {
            "b": [list(row) for row in booking_sig],
            "u": [list(row) for row in user_sig],
            "m": [machine_sig[0], str(machine_sig[1] or "")],
            "admin": current_user.has_admin_access(),
        }
    )
    if request.args.get("rev") == revision:
        return jsonify({"unchanged": True, "revision": revision})

    bookings = (
        Booking.query.options(joinedload(Booking.teacher))
        .order_by(Booking.booking_date.desc(), Booking.start_time)
        .all()
    )
    users = User.query.order_by(User.username).all()
    machines = (
        MachineGuard.query.filter(
            MachineGuard.locked_until != None,  # noqa: E711
            MachineGuard.locked_until > datetime.utcnow(),
        )
        .order_by(MachineGuard.locked_until.desc())
        .all()
    )
    is_admin = current_user.has_admin_access()
    payload = {
        "is_admin": is_admin,
        "statuses": list(config.BOOKING_STATUSES),
        "bookings": [serialize_live_booking(b) for b in bookings],
        "users": [
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "role": user.role,
                "role_label": config.ROLE_LABELS.get(user.role, user.role),
                "shift": user.shift or "",
                "shift_label": user.shift_label(),
                "can_edit": current_user.can_edit_user(user),
                "can_reset": current_user.can_reset_user(user),
            }
            for user in users
        ],
        "machines": [
            {
                "id": machine.id,
                "token": machine.token,
                "token_short": machine.token_short(),
                "ip_address": machine.ip_address,
                "lock_level": machine.lock_level or 0,
                "locked_until": format_lock_until(machine.locked_until),
            }
            for machine in machines
        ],
    }
    payload["unchanged"] = False
    payload["revision"] = revision
    return jsonify(payload)


@app.route("/agendamentos/<int:booking_id>/presente", methods=["POST"])
@role_required("professor")
def marcar_presente(booking_id):
    booking = db.session.get(Booking, booking_id)
    if not booking:
        flash("Agendamento não encontrado.", "error")
        return redirect(url_for("agendamentos"))

    if booking.teacher_id != current_user.id:
        flash("Somente o autor do agendamento pode marcar presença.", "error")
        return redirect(url_for("agendamentos"))

    booking.status = "presente"
    db.session.commit()
    flash("Presença registrada!", "success")
    return redirect(url_for("agendamentos"))


@app.route("/admin")
@role_required(*config.STAFF_ROLES)
def admin_panel():
    bookings = (
        Booking.query.options(joinedload(Booking.teacher))
        .order_by(Booking.booking_date.desc(), Booking.start_time)
        .all()
    )
    users = User.query.order_by(User.username).all()
    time_config = SystemConfig.get_time_config()
    notification_emails = NotificationEmail.query.order_by(NotificationEmail.email).all()

    return render_template(
        "admin.html",
        bookings=bookings,
        users=users,
        time_config=time_config,
        notification_emails=notification_emails,
        is_admin=current_user.has_admin_access(),
        assignable_roles=current_user.assignable_roles(),
        auto_professor_enabled=SystemConfig.is_auto_professor_enabled(),
        locked_machines=MachineGuard.query.filter(
            MachineGuard.locked_until != None,  # noqa: E711
            MachineGuard.locked_until > datetime.utcnow(),
        )
        .order_by(MachineGuard.locked_until.desc())
        .all(),
        format_lock_until=format_lock_until,
    )


@app.route("/admin/booking/<int:booking_id>/status", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_update_status(booking_id):
    status = request.form.get("status", "")
    if status not in config.BOOKING_STATUSES:
        flash("Status inválido.", "error")
        return redirect(url_for("admin_panel"))

    booking = db.session.get(Booking, booking_id)
    if booking:
        booking.status = status
        db.session.commit()
        flash("Status atualizado.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/booking/<int:booking_id>/delete", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_delete_booking(booking_id):
    booking = db.session.get(Booking, booking_id)
    if booking:
        db.session.delete(booking)
        db.session.commit()
        flash("Agendamento excluído.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/bookings/delete-all", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_delete_all_bookings():
    Booking.query.delete()
    db.session.commit()
    flash("Todos os agendamentos foram excluídos.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/bookings/delete-by-date", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_delete_bookings_by_date():
    mode = (request.form.get("mode") or "single").strip()
    start = parse_booking_date(request.form.get("date_start", ""))
    end = parse_booking_date(request.form.get("date_end", ""))

    if mode != "range":
        if not start:
            flash("Escolha a data dos agendamentos que deseja excluir.", "error")
            return redirect(url_for("admin_panel"))
        end = start
    else:
        if not start or not end:
            flash("Escolha a data inicial e a data final.", "error")
            return redirect(url_for("admin_panel"))
        if start > end:
            start, end = end, start

    deleted = Booking.query.filter(
        Booking.booking_date >= start,
        Booking.booking_date <= end,
    ).delete(synchronize_session=False)
    db.session.commit()

    if start == end:
        flash(
            f"{deleted} agendamento(s) do dia {start.strftime('%d/%m/%Y')} foram excluídos.",
            "success",
        )
    else:
        flash(
            f"{deleted} agendamento(s) de {start.strftime('%d/%m/%Y')} até {end.strftime('%d/%m/%Y')} foram excluídos.",
            "success",
        )
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_update_role(user_id):
    role = request.form.get("role", "")
    if role not in config.ROLES:
        flash("Função inválida.", "error")
        return redirect(url_for("admin_panel"))

    user = db.session.get(User, user_id)
    if not user or user.id == current_user.id:
        flash("Não é possível alterar este cargo.", "error")
        return redirect(url_for("admin_panel"))
    if not current_user.can_edit_user(user):
        flash("Você não pode alterar o cargo deste usuário.", "error")
        return redirect(url_for("admin_panel"))
    if role not in current_user.assignable_roles():
        flash("Você não pode atribuir esta função.", "error")
        return redirect(url_for("admin_panel"))
    if user.role == "vgs_owner" and role != "vgs_owner" and is_last_of_role("vgs_owner"):
        flash("Não é possível remover o último VGS-Owner's.", "error")
        return redirect(url_for("admin_panel"))
    user.role = role
    db.session.commit()
    flash(
        f"Função de {user.username} atualizada para {role_label(role)}.",
        "success",
    )
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/shift", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_update_shift(user_id):
    shift = request.form.get("shift", "")
    if shift not in config.SHIFT_CHOICES:
        flash("Turno inválido.", "error")
        return redirect(url_for("admin_panel"))
    if shift == config.SHIFT_AMBOS and not current_user.has_admin_access():
        flash("Apenas um administrador pode liberar os dois turnos.", "error")
        return redirect(url_for("admin_panel"))

    user = db.session.get(User, user_id)
    if not user:
        flash("Usuário não encontrado.", "error")
        return redirect(url_for("admin_panel"))
    user.shift = shift
    db.session.commit()
    flash(f"Turno de {user.username} atualizado para {config.SHIFT_LABELS[shift]}.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/add", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_add_user():
    username = request.form.get("username", "").strip()[:80]
    email = request.form.get("email", "").strip().lower()[:120]
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")
    role = request.form.get("role", "visualizador")
    if role not in current_user.assignable_roles():
        flash("Você não pode atribuir esta função.", "error")
        return redirect(url_for("admin_panel"))

    if len(username) < 3:
        flash("Nome de usuário deve ter pelo menos 3 caracteres.", "error")
    elif not is_safe_email(email):
        flash("E-mail inválido.", "error")
    elif len(password) < 6:
        flash("A senha deve ter pelo menos 6 caracteres.", "error")
    elif password != confirm:
        flash("As senhas não coincidem.", "error")
    else:
        conflict = duplicate_account_message(username, email)
        if conflict:
            flash(conflict, "error")
        else:
            user = User(username=username, email=email, role=role, email_verified=True)
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.commit()
                flash(f"Usuário {username} cadastrado com a função {role_label(role)}.", "success")
            except IntegrityError:
                db.session.rollback()
                flash(
                    duplicate_account_message(username, email)
                    or "Este nome de usuário ou e-mail já está cadastrado.",
                    "error",
                )
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@role_required(*config.STAFF_ROLES)
def admin_reset_password(user_id):
    user = db.session.get(User, user_id)
    if user:
        if not current_user.can_reset_user(user):
            flash("Você não pode redefinir a senha deste usuário.", "error")
            return redirect(url_for("admin_panel"))
        user.password_hash = None
        user.must_reset_password = True
        user.bump_session()
        db.session.commit()
        flash(
            f"Senha de {user.username} removida. O usuário deverá definir uma nova senha no próximo login.",
            "success",
        )
    return redirect(url_for("admin_panel"))


@app.route("/admin/settings/auto-professor", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_toggle_auto_professor():
    enabled = not SystemConfig.is_auto_professor_enabled()
    SystemConfig.set_auto_professor_enabled(enabled)
    db.session.commit()
    flash(
        "Atribuição automática de professor "
        + ("ligada." if enabled else "desligada."),
        "success",
    )
    return redirect(url_for("admin_panel"))


@app.route("/admin/machine-locks/<int:lock_id>/duration", methods=["POST"])
@role_required(*config.STAFF_ROLES)
def admin_update_machine_lock(lock_id):
    guard = db.session.get(MachineGuard, lock_id)
    if not guard:
        flash("Registro de bloqueio não encontrado.", "error")
        return redirect(url_for("admin_panel"))

    raw_amount = (request.form.get("amount") or "").strip()
    try:
        amount = int(raw_amount)
    except ValueError:
        flash("Informe um número válido para o tempo de bloqueio.", "error")
        return redirect(url_for("admin_panel"))

    amount = max(1, min(amount, 999))
    unit = request.form.get("unit", "days")
    if unit == "hours":
        delta = timedelta(hours=amount)
    elif unit == "weeks":
        delta = timedelta(weeks=amount)
    elif unit == "months":
        delta = timedelta(days=30 * amount)
    elif unit == "years":
        delta = timedelta(days=365 * amount)
    else:
        delta = timedelta(days=amount)

    guard.locked_until = datetime.utcnow() + delta
    if delta >= timedelta(days=28):
        guard.lock_level = max(guard.lock_level, 2)
    guard.updated_at = datetime.utcnow()
    db.session.commit()
    flash("Tempo de bloqueio atualizado.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/machine-locks/<int:lock_id>/remove", methods=["POST"])
@role_required(*config.STAFF_ROLES)
def admin_remove_machine_lock(lock_id):
    guard = db.session.get(MachineGuard, lock_id)
    if not guard:
        flash("Registro de bloqueio não encontrado.", "error")
        return redirect(url_for("admin_panel"))

    guard.locked_until = None
    guard.lock_level = 0
    guard.attempt_count = 0
    guard.strike_count = 0
    guard.updated_at = datetime.utcnow()
    db.session.commit()
    flash("Bloqueio removido.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/time-slots", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_update_time_slots():
    lista1_raw = request.form.get("lista1", "")
    lista2_raw = request.form.get("lista2", "")

    def parse_times(raw):
        return [t.strip() for t in raw.replace("\r", "").split("\n") if t.strip()]

    lista1 = parse_times(lista1_raw)
    lista2 = parse_times(lista2_raw)

    if not parse_shift_rows(lista1) or not parse_shift_rows(lista2):
        flash("Cada turno precisa ter pelo menos um horário de aula válido.", "error")
        return redirect(url_for("admin_panel"))

    config_data = SystemConfig.get_time_config()
    config_data["lista1"] = lista1
    config_data["lista2"] = lista2
    SystemConfig.set("time_slots", config_data)
    db.session.commit()
    flash("Horários atualizados.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/notifications/add", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_add_notification_email():
    email = request.form.get("email", "").strip().lower()
    if not is_safe_email(email):
        flash("E-mail inválido.", "error")
    elif NotificationEmail.query.filter_by(email=email).first():
        flash("Este e-mail já está cadastrado.", "error")
    else:
        db.session.add(NotificationEmail(email=email))
        db.session.commit()
        flash("E-mail adicionado para notificações.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/notifications/<int:email_id>/delete", methods=["POST"])
@role_required(*config.ADMIN_ACCESS_ROLES)
def admin_delete_notification_email(email_id):
    row = db.session.get(NotificationEmail, email_id)
    if row:
        db.session.delete(row)
        db.session.commit()
        flash("E-mail removido.", "success")
    return redirect(url_for("admin_panel"))


with app.app_context():
    if _should_migrate_schema():
        db.create_all()
        ensure_schema()
    init_default_data()


if __name__ == "__main__":
    app.run(
        debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes"),
        host="127.0.0.1",
        port=5000,
        threaded=True,
    )
