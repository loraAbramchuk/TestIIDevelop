from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import Settings


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def make_engine(settings: Settings, *, query_timeout=False):
    arguments = {"connect_timeout": 3}
    if query_timeout:
        arguments["options"] = "-cstatement_timeout=5000 -clock_timeout=3000"
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args=arguments,
    )


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
