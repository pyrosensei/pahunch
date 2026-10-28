from collections.abc import Generator

from sqlmodel import Session, SQLModel, create_engine

from app.config import settings
from app.seed import seed_database

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        seed_database(session)


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
