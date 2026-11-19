from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from app.auth_dependencies import CurrentUser, Db
from app.auth_protection import check_csrf, rate_limit
from app.auth_schemas import ActionReceipt, ActionTokenInput, EmailInput, ResetPasswordInput
from app.mail import service
from app.models import User
from app.video_contracts import ContractError

router = APIRouter(
    prefix="/api/auth",
    tags=["auth"],
    dependencies=[Depends(check_csrf)],
    responses={
        400: {"model": ContractError},
        401: {"description": "Требуется вход"},
        403: {"model": ContractError},
        429: {"description": "Лимит запросов; Retry-After"},
        503: {"model": ContractError},
    },
)
REQUEST_RECEIPT = (
    "Если для этого адреса доступно действие, письмо будет отправлено. "
    "Проверьте почту и папку «Спам»."
)


def request_limit(db, request, address):
    peer = request.client.host if request.client else "unknown"
    rate_limit(db, f"mail:ip:{peer}", 15, 3600)
    rate_limit(db, f"mail:address:{address}", 5, 3600)


@router.post("/email/request", response_model=ActionReceipt, status_code=202)
def request_verification(request: Request, db: Db, current: CurrentUser):
    service.require_mail(request.app.state.settings)
    request_limit(db, request, current.email)
    user = db.scalar(select(User).where(User.id == current.id).with_for_update())
    if user.email_verified_at is None:
        service.issue(db, request.app.state.settings, user, "verify_email")
    db.commit()
    return ActionReceipt(detail=REQUEST_RECEIPT)


@router.post("/email/verify", response_model=ActionReceipt)
def verify_email(payload: ActionTokenInput, request: Request, db: Db):
    peer = request.client.host if request.client else "unknown"
    rate_limit(db, f"mail:consume:{peer}", 30, 3600)
    service.consume(
        db, request.app.state.settings, payload.token.get_secret_value(), "verify_email"
    )
    return ActionReceipt(detail="Email подтверждён. Можно продолжить работу или войти в аккаунт.")


@router.post("/password/request", response_model=ActionReceipt, status_code=202)
def request_password(payload: EmailInput, request: Request, db: Db):
    service.require_mail(request.app.state.settings)
    request_limit(db, request, str(payload.email))
    user = db.scalar(select(User).where(User.email == str(payload.email)).with_for_update())
    if user is not None:
        service.issue(db, request.app.state.settings, user, "reset_password")
    db.commit()
    return ActionReceipt(detail=REQUEST_RECEIPT)


@router.post("/password/reset", response_model=ActionReceipt)
def reset_password(payload: ResetPasswordInput, request: Request, response: Response, db: Db):
    peer = request.client.host if request.client else "unknown"
    rate_limit(db, f"mail:consume:{peer}", 30, 3600)
    service.consume(
        db,
        request.app.state.settings,
        payload.token.get_secret_value(),
        "reset_password",
        payload.password.get_secret_value(),
    )
    response.delete_cookie(
        request.app.state.settings.session_cookie_name,
        path="/",
        secure=request.app.state.settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return ActionReceipt(
        detail="Пароль изменён. Все прежние сеансы завершены. Войдите с новым паролем."
    )
