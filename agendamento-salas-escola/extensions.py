from functools import wraps

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError

db = SQLAlchemy()

_DISCONNECT_ERRORS = (OperationalError, DisconnectionError, InterfaceError)


def recover_db_session() -> None:
    try:
        db.session.rollback()
    except Exception:
        pass
    db.session.remove()
    try:
        db.engine.dispose()
    except Exception:
        pass


def retry_on_disconnect(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _DISCONNECT_ERRORS:
            recover_db_session()
            return fn(*args, **kwargs)

    return wrapped
