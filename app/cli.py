"""Local administrator bootstrap; passwords are read interactively, never from arguments."""

import argparse
import getpass

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.auth_schemas import RegisterInput
from app.config import Settings
from app.db import make_engine, make_session_factory
from app.models import User
from app.security import hash_password


def main():
    parser = argparse.ArgumentParser(description="Управление видеоплатформой")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="Создать администратора")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    args = parser.parse_args()
    password = getpass.getpass("Пароль (12–128 символов): ")
    confirmation = getpass.getpass("Повторите пароль: ")
    if password != confirmation:
        parser.exit(1, "Пароли не совпадают\n")
    try:
        data = RegisterInput(email=args.email, display_name=args.name, password=password)
    except ValidationError:
        parser.exit(1, "Проверьте email, имя и длину пароля\n")
    engine = make_engine(Settings())
    try:
        with make_session_factory(engine)() as db:
            db.add(
                User(
                    email=str(data.email),
                    display_name=data.display_name,
                    password_hash=hash_password(data.password.get_secret_value()),
                    role="admin",
                )
            )
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                parser.exit(1, "Аккаунт с этим email уже существует; его роль не изменена\n")
        print("Администратор создан")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
