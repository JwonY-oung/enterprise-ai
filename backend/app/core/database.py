import psycopg
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


if settings.database_url.startswith("postgresql"):
    @event.listens_for(engine, "connect")
    def register_vector(dbapi_connection, _):
        from pgvector.psycopg import register_vector

        try:
            register_vector(dbapi_connection)
        except psycopg.ProgrammingError:
            # Fresh databases create the extension during initialization.
            pass


def initialize_database() -> None:
    if settings.database_url.startswith("postgresql"):
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("chunk_embeddings")}
    if "vector" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE chunk_embeddings ADD COLUMN vector VECTOR(512)"))
    chunk_columns = {column["name"] for column in inspect(engine).get_columns("document_chunks")}
    additions = {
        "section_title": "VARCHAR(300) NOT NULL DEFAULT ''",
        "char_start": "INTEGER NOT NULL DEFAULT 0",
        "char_end": "INTEGER NOT NULL DEFAULT 0",
        "content_type": "VARCHAR(30) NOT NULL DEFAULT 'body'",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in chunk_columns:
                connection.execute(text(f"ALTER TABLE document_chunks ADD COLUMN {name} {definition}"))
    # 老表补任务队列字段（任务恢复）
    doc_columns = {column["name"] for column in inspect(engine).get_columns("documents")}
    doc_additions = {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "max_attempts": "INTEGER NOT NULL DEFAULT 3",
        "claimed_at": "TIMESTAMP",
    }
    with engine.begin() as connection:
        for name, definition in doc_additions.items():
            if name not in doc_columns:
                connection.execute(text(f"ALTER TABLE documents ADD COLUMN {name} {definition}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
