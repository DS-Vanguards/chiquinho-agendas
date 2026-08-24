import os
import threading
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)

import config
from core.layout_sig import bind as _layout_bind
from email_utils import send_notification
from extensions import db
from models import Booking, NotificationEmail, SystemConfig, User, ensure_schema, init_default_data

app = Flask(__name__)
app.config.from_object(config)
_layout_bind(app)

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Faça login para continuar."


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


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
    return any(domain == d or domain.endswith("." + d) for d in config.ALLOWED_EMAIL_DOMAINS)


def parse_booking_date(value: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def times_overlap(start1, end1, start2, end2) -> bool:
    return start1 < end2 and start2 < end1


def get_home_endpoint(user=None) -> str:
    user = user or current_user
    if user.is_authenticated and user.is_professor:
        return "agendamentos"
    return "dashboard"


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


def is_morning_room_restricted(room: str) -> bool:
    return (
        SystemConfig.get_effective_active_list() == "lista1"
        and room in config.MORNING_RESTRICTED_ROOMS
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
        "ROOM_LABELS": {room["id"]: room["label"] for room in config.GRID_ROOMS},
        "STATUSES": config.BOOKING_STATUSES,
        "MORNING_RESTRICTED_ROOMS": config.MORNING_RESTRICTED_ROOMS,
    }


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


def build_schedule_context(selected_date: date):
    time_slots = SystemConfig.get_active_time_slots()
    rows = []
    for index in range(len(time_slots) - 1):
        rows.append(
            {
                "aula": index + 1,
                "start_time": time_slots[index],
                "end_time": time_slots[index + 1],
                "label": f"{time_slots[index]} - {time_slots[index + 1]}",
            }
        )

    bookings = Booking.query.filter_by(booking_date=selected_date).all()
    grid = {}
    for booking in bookings:
        grid[(booking.room, booking.start_time, booking.end_time)] = booking

    prev_date = (selected_date - timedelta(days=1)).isoformat()
    next_date = (selected_date + timedelta(days=1)).isoformat()

    return {
        "selected_date": selected_date,
        "date_label": format_date_label(selected_date),
        "filter_date": selected_date.isoformat(),
        "prev_date": prev_date,
        "next_date": next_date,
        "schedule_rows": rows,
        "schedule_grid": grid,
        "active_list_name": SystemConfig.get_effective_active_list(),
        "auto_list_switch": SystemConfig.is_auto_list_switch_enabled(),
    }


def create_booking(room, booking_date, start_time, end_time, teacher_id, status="agendado"):
    time_slots = SystemConfig.get_active_time_slots()

    if room not in config.ROOMS:
        return False, "Sala inválida.", None
    if not booking_date:
        return False, "Data inválida.", None
    if booking_date < date.today():
        return False, "Não é possível agendar em datas passadas.", None
    if start_time not in time_slots or end_time not in time_slots:
        return False, "Horário inválido.", None
    if start_time >= end_time:
        return False, "O horário final deve ser posterior ao inicial.", None
    if status != "bloqueado" and is_morning_room_restricted(room):
        return False, "No turno da manhã, as salas 01 a 04 não podem ser agendadas.", None

    conflict = Booking.query.filter_by(room=room, booking_date=booking_date).all()
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
    db.session.commit()

    if status != "bloqueado":
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

    message = (
        "Horário bloqueado."
        if status == "bloqueado"
        else "Agendamento realizado com sucesso!"
    )
    return True, message, booking


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for(get_home_endpoint()))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for(get_home_endpoint()))

    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier)
        ).first()

        if not user:
            flash("Usuário ou e-mail não encontrado.", "error")
            return render_template("login.html")

        if user.must_reset_password or not user.password_hash:
            return render_template("set_password.html", user=user, forced=True)

        if not user.check_password(password):
            flash("Senha incorreta.", "error")
            return render_template("login.html")

        login_user(user)
        flash(f"Bem-vindo(a), {user.username}!", "success")
        return redirect(url_for(get_home_endpoint(user)))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for(get_home_endpoint()))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if len(username) < 3:
            flash("Nome de usuário deve ter pelo menos 3 caracteres.", "error")
        elif not validate_institutional_email(email):
            flash(
                "Use um e-mail institucional válido (domínios permitidos: "
                + ", ".join(config.ALLOWED_EMAIL_DOMAINS)
                + ").",
                "error",
            )
        elif len(password) < 6:
            flash("A senha deve ter pelo menos 6 caracteres.", "error")
        elif password != confirm:
            flash("As senhas não coincidem.", "error")
        elif User.query.filter_by(username=username).first():
            flash("Este nome de usuário já está em uso.", "error")
        elif User.query.filter_by(email=email).first():
            flash("Este e-mail já está cadastrado.", "error")
        else:
            role = "professor" if should_auto_assign_professor(email) else "visualizador"
            user = User(username=username, email=email, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            if role == "professor":
                flash("Cadastro realizado como professor! Faça login para continuar.", "success")
            else:
                flash("Cadastro realizado! Faça login para continuar.", "success")
            return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/set-password", methods=["POST"])
def set_password():
    user_id = request.form.get("user_id", type=int)
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    user = db.session.get(User, user_id)
    if not user:
        flash("Usuário não encontrado.", "error")
        return redirect(url_for("login"))

    if len(password) < 6:
        flash("A senha deve ter pelo menos 6 caracteres.", "error")
        return render_template("set_password.html", user=user, forced=True)

    if password != confirm:
        flash("As senhas não coincidem.", "error")
        return render_template("set_password.html", user=user, forced=True)

    user.set_password(password)
    db.session.commit()
    login_user(user)
    flash("Senha definida com sucesso!", "success")
    return redirect(url_for(get_home_endpoint(user)))

@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@role_required("admin", "moderador")
def admin_delete_user(user_id):
    user = db.session.get(User, user_id)
    if user:
        if user.id == current_user.id:
            flash("Você não pode excluir sua própria conta.", "error")
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


@app.route("/logout")
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
    if current_user.role in ("admin", "moderador", "coordenador"):
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

    if current_user.role in ("admin", "moderador", "coordenador"):
        query = Booking.query.filter_by(booking_date=selected_date)
        bookings = query.order_by(Booking.start_time, Booking.room).all()
        prev_date = (selected_date - timedelta(days=1)).isoformat()
        next_date = (selected_date + timedelta(days=1)).isoformat()

        return render_template(
            "agendamentos.html",
            view_mode="detailed",
            bookings=bookings,
            filter_date=selected_date.isoformat(),
            date_label=format_date_label(selected_date),
            prev_date=prev_date,
            next_date=next_date,
        )

    context = build_schedule_context(selected_date)
    return render_template("agendamentos.html", view_mode="grid", **context)


@app.route("/agendamentos/agendar-rapido", methods=["POST"])
@role_required("professor")
def agendar_rapido():
    room = request.form.get("room", "")
    booking_date = parse_booking_date(request.form.get("booking_date", ""))
    start_time = request.form.get("start_time", "")
    end_time = request.form.get("end_time", "")

    success, message, booking = create_booking(
        room, booking_date, start_time, end_time, current_user.id
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
@role_required("professor")
def cancelar_agendamento(booking_id):
    booking = db.session.get(Booking, booking_id)
    if not booking:
        return jsonify({"success": False, "message": "Agendamento não encontrado."}), 404

    if booking.teacher_id != current_user.id:
        return jsonify(
            {"success": False, "message": "Somente o autor pode cancelar o agendamento."}
        ), 403

    if booking.status in ("presente", "bloqueado"):
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
@role_required("coordenador")
def bloquear_horario():
    room = request.form.get("room", "")
    booking_date = parse_booking_date(request.form.get("booking_date", ""))
    start_time = request.form.get("start_time", "")
    end_time = request.form.get("end_time", "")

    success, message, booking = create_booking(
        room,
        booking_date,
        start_time,
        end_time,
        current_user.id,
        status="bloqueado",
    )
    status_code = 200 if success else 400
    payload = {"success": success, "message": message}
    if success and booking:
        payload.update({"booking_id": booking.id, "status": "bloqueado"})
    return jsonify(payload), status_code


@app.route("/agendamentos/<int:booking_id>/desbloquear", methods=["POST"])
@role_required("coordenador")
def desbloquear_horario(booking_id):
    booking = db.session.get(Booking, booking_id)
    if not booking or booking.status != "bloqueado":
        return jsonify({"success": False, "message": "Bloqueio não encontrado."}), 404

    db.session.delete(booking)
    db.session.commit()
    return jsonify({"success": True, "message": "Horário desbloqueado."})


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
@role_required("admin", "moderador")
def admin_panel():
    bookings = Booking.query.order_by(
        Booking.booking_date.desc(), Booking.start_time
    ).all()
    users = User.query.order_by(User.username).all()
    time_config = SystemConfig.get_time_config()
    notification_emails = NotificationEmail.query.order_by(NotificationEmail.email).all()

    return render_template(
        "admin.html",
        bookings=bookings,
        users=users,
        time_config=time_config,
        effective_active_list=SystemConfig.get_effective_active_list(),
        auto_list_switch=SystemConfig.is_auto_list_switch_enabled(),
        notification_emails=notification_emails,
        is_admin=current_user.is_admin,
        auto_professor_enabled=SystemConfig.is_auto_professor_enabled(),
    )


@app.route("/admin/booking/<int:booking_id>/status", methods=["POST"])
@role_required("admin")
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
@role_required("admin")
def admin_delete_booking(booking_id):
    booking = db.session.get(Booking, booking_id)
    if booking:
        db.session.delete(booking)
        db.session.commit()
        flash("Agendamento excluído.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/bookings/delete-all", methods=["POST"])
@role_required("admin")
def admin_delete_all_bookings():
    Booking.query.delete()
    db.session.commit()
    flash("Todos os agendamentos foram excluídos.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@role_required("admin")
def admin_update_role(user_id):
    role = request.form.get("role", "")
    if role not in config.ROLES:
        flash("Função inválida.", "error")
        return redirect(url_for("admin_panel"))

    user = db.session.get(User, user_id)
    if user and user.id != current_user.id:
        user.role = role
        db.session.commit()
        flash(f"Função de {user.username} atualizada para {role}.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@role_required("admin", "moderador")
def admin_reset_password(user_id):
    user = db.session.get(User, user_id)
    if user:
        user.password_hash = None
        user.must_reset_password = True
        db.session.commit()
        flash(
            f"Senha de {user.username} removida. O usuário deverá definir uma nova senha no próximo login.",
            "success",
        )
    return redirect(url_for("admin_panel"))


@app.route("/admin/settings/auto-professor", methods=["POST"])
@role_required("admin")
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


@app.route("/admin/time-slots", methods=["POST"])
@role_required("admin")
def admin_update_time_slots():
    lista1_raw = request.form.get("lista1", "")
    lista2_raw = request.form.get("lista2", "")

    def parse_times(raw):
        return [t.strip() for t in raw.replace("\r", "").split("\n") if t.strip()]

    lista1 = parse_times(lista1_raw)
    lista2 = parse_times(lista2_raw)

    if not lista1 or not lista2:
        flash("Ambas as listas devem ter pelo menos um horário.", "error")
        return redirect(url_for("admin_panel"))

    config_data = SystemConfig.get_time_config()
    config_data["lista1"] = lista1
    config_data["lista2"] = lista2
    SystemConfig.set("time_slots", config_data)
    db.session.commit()
    flash("Horários atualizados.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/time-slots/auto-switch", methods=["POST"])
@role_required("admin", "moderador")
def admin_toggle_auto_list_switch():
    config_data = SystemConfig.get_time_config()
    currently_auto = bool(config_data.get("auto_switch", True))
    if currently_auto:
        config_data["active_list"] = SystemConfig.get_effective_active_list()
        config_data["auto_switch"] = False
        flash("Troca automática desativada. Use o botão ao lado para alternar as listas.", "success")
    else:
        config_data["auto_switch"] = True
        flash("Troca automática ativada: Lista 1 até 14:29 e Lista 2 a partir de 14:30.", "success")
    SystemConfig.set("time_slots", config_data)
    db.session.commit()
    return redirect(url_for("admin_panel"))


@app.route("/admin/time-slots/switch", methods=["POST"])
@role_required("admin", "moderador")
def admin_switch_time_list():
    config_data = SystemConfig.get_time_config()
    if config_data.get("auto_switch", True):
        flash("Desative a troca automática para alternar a lista manualmente.", "error")
        return redirect(url_for("admin_panel"))

    config_data["active_list"] = (
        "lista2" if config_data.get("active_list") == "lista1" else "lista1"
    )
    SystemConfig.set("time_slots", config_data)
    db.session.commit()
    flash(f"Lista ativa alterada para {config_data['active_list']}.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/notifications/add", methods=["POST"])
@role_required("admin")
def admin_add_notification_email():
    email = request.form.get("email", "").strip().lower()
    if not email or "@" not in email:
        flash("E-mail inválido.", "error")
    elif NotificationEmail.query.filter_by(email=email).first():
        flash("Este e-mail já está cadastrado.", "error")
    else:
        db.session.add(NotificationEmail(email=email))
        db.session.commit()
        flash("E-mail adicionado para notificações.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/notifications/<int:email_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_notification_email(email_id):
    row = db.session.get(NotificationEmail, email_id)
    if row:
        db.session.delete(row)
        db.session.commit()
        flash("E-mail removido.", "success")
    return redirect(url_for("admin_panel"))


with app.app_context():
    db.create_all()
    ensure_schema()
    init_default_data()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
