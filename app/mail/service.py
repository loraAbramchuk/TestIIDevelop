"""Email ownership and password recovery, sharing the durable PostgreSQL queue."""

import base64
import hashlib
import hmac
import re
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text, update

from app.errors import DomainError
from app.mail.models import AuthMailToken
from app.models import AuthSession, User
from app.security import hash_password, token_hash
from app.uploads.queue import queue_app


class MailDeliveryError(Exception):
    """A safe error code: SMTP diagnostics may contain recipient addresses."""


def require_mail(settings):
    if not settings.mail_configured:
        raise DomainError(
            503, "mail_unavailable", "Отправка писем временно недоступна. Попробуйте позже."
        )


def token_value(settings, identifier, purpose):
    require_mail(settings)
    signature = hmac.new(
        settings.auth_mail_secret.get_secret_value().encode(),
        f"auth-mail:v1:{purpose}:{identifier.hex}".encode(),
        hashlib.sha256,
    ).digest()
    return identifier.hex + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def issue(db, settings, user, purpose):
    """Caller holds the user lock; the link and job commit in the same transaction."""
    require_mail(settings)
    now = datetime.now(UTC)
    db.execute(
        update(AuthMailToken)
        .where(
            AuthMailToken.user_id == user.id,
            AuthMailToken.purpose == purpose,
            AuthMailToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    identifier = uuid4()
    minutes = (
        settings.email_verification_minutes
        if purpose == "verify_email"
        else settings.password_reset_minutes
    )
    record = AuthMailToken(
        id=identifier,
        user_id=user.id,
        purpose=purpose,
        token_hash=token_hash(token_value(settings, identifier, purpose)),
        expires_at=now + timedelta(minutes=minutes),
    )
    db.add(record)
    db.flush()
    db.execute(text("SET LOCAL search_path TO video_queue, public"))
    connection = db.connection().connection.driver_connection
    queue_app(settings).configure_task("auth.send", queue="auth-mail", connection=connection).defer(
        token_id=str(identifier)
    )
    return record


def invalid_link():
    return DomainError(
        400,
        "action_token_invalid",
        "Ссылка недействительна, уже использована или срок её действия истёк. "
        "Запросите новое письмо.",
    )


def consume(db, settings, value, purpose, password=None):
    require_mail(settings)
    if not re.fullmatch(r"[0-9a-f]{32}\.[A-Za-z0-9_-]{43}", value):
        raise invalid_link()
    identifier = UUID(hex=value[:32])
    if not hmac.compare_digest(value, token_value(settings, identifier, purpose)):
        raise invalid_link()
    record = db.get(AuthMailToken, identifier)
    if record is None:
        raise invalid_link()
    # Serialize issuing and consuming links for an account, including parallel resets.
    user = db.scalar(select(User).where(User.id == record.user_id).with_for_update())
    record = db.scalar(
        select(AuthMailToken)
        .where(AuthMailToken.id == identifier)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    if (
        user is None
        or record is None
        or record.purpose != purpose
        or record.used_at
        or record.expires_at <= now
        or not hmac.compare_digest(record.token_hash, token_hash(value))
    ):
        raise invalid_link()
    record.used_at = now
    user.email_verified_at = user.email_verified_at or now
    if purpose == "reset_password":
        user.password_hash = hash_password(password)
        db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
        db.execute(
            update(AuthMailToken)
            .where(AuthMailToken.user_id == user.id, AuthMailToken.used_at.is_(None))
            .values(used_at=now)
        )
    db.commit()


def deliver(settings, sessions, token_id):
    """At-least-once email delivery; repeat emails contain the same one-use link."""
    require_mail(settings)
    with sessions() as db:
        record = db.get(AuthMailToken, UUID(token_id))
        if not record or record.used_at or record.sent_at or record.expires_at <= datetime.now(UTC):
            return
        user = db.get(User, record.user_id)
        if user is None:
            return
        purpose = record.purpose
        route = "verify-email" if purpose == "verify_email" else "reset-password"
        # The configured origin prevents Host poisoning; the fragment stays out of access logs.
        link = (
            settings.public_base_url.rstrip("/")
            + "/"
            + route
            + "#token="
            + token_value(settings, record.id, purpose)
        )
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = user.email
        message["Subject"] = (
            "Подтвердите email" if purpose == "verify_email" else "Восстановление пароля"
        )
        message.set_content(
            ("Подтвердите адрес email" if purpose == "verify_email" else "Задайте новый пароль")
            + f":\n\n{link}\n\nСсылка одноразовая. "
            + "Если вы не запрашивали письмо, проигнорируйте его.\n"
        )
    try:
        transport = smtplib.SMTP_SSL if settings.smtp_security == "tls" else smtplib.SMTP
        options = {"timeout": settings.smtp_timeout_seconds}
        if settings.smtp_security == "tls":
            options["context"] = ssl.create_default_context()
        with transport(settings.smtp_host, settings.smtp_port, **options) as smtp:
            if settings.smtp_security == "starttls":
                smtp.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException, ValueError):
        raise MailDeliveryError("mail_delivery_unavailable") from None
    with sessions() as db:
        db.execute(
            update(AuthMailToken)
            .where(AuthMailToken.id == UUID(token_id))
            .values(sent_at=datetime.now(UTC))
        )
        db.commit()


def recover_jobs(settings, sessions):
    """Recover only mail jobs whose library worker lost its heartbeat; no second queue."""
    with sessions() as db:
        jobs = db.execute(
            text("""
            SELECT j.id, j.attempts FROM video_queue.procrastinate_jobs j
            LEFT JOIN video_queue.procrastinate_workers w ON w.id=j.worker_id
            WHERE j.task_name='auth.send' AND j.status='doing'
              AND (w.last_heartbeat IS NULL OR w.last_heartbeat <
                   clock_timestamp() - make_interval(secs => :lease))
            ORDER BY j.id LIMIT 100 FOR UPDATE OF j SKIP LOCKED
        """),
            {"lease": settings.processing_lease_seconds},
        ).all()
        for job in jobs:
            if job.attempts < 3:
                db.execute(
                    text(
                        "SELECT video_queue.procrastinate_retry_job_v2"
                        "(:id, now(), NULL, NULL, NULL)"
                    ),
                    {"id": job.id},
                )
            else:
                db.execute(
                    text("SELECT video_queue.procrastinate_finish_job_v1(:id, 'failed', false)"),
                    {"id": job.id},
                )
        old = db.scalars(
            select(AuthMailToken.id)
            .where(AuthMailToken.expires_at < datetime.now(UTC) - timedelta(days=1))
            .limit(100)
        ).all()
        if old:
            db.execute(delete(AuthMailToken).where(AuthMailToken.id.in_(old)))
        db.commit()
        return len(jobs)
