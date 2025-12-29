"""Database engine and session management utilities."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Generator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError, DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from src.database.models import Base
from src.utils.config_loader import DatabaseConfig


_DEFAULT_POOL_SIZE = 5
_DEFAULT_MAX_OVERFLOW = 10


def _ensure_parent_directory(path: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def create_engine_from_config(config: DatabaseConfig, echo: bool = False) -> Engine:
    """Create a SQLAlchemy engine based on application configuration."""
    db_type = config.type.lower()
    if db_type == "sqlite":
        _ensure_parent_directory(config.path)
        url = f"sqlite+pysqlite:///{config.path}"
        engine = create_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        return engine
    if db_type in {"postgres", "postgresql"}:
        url = os.getenv("DATABASE_URL") or f"postgresql+psycopg2://{config.path}"
        engine = create_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=_DEFAULT_POOL_SIZE,
            max_overflow=_DEFAULT_MAX_OVERFLOW,
        )
        return engine
    if db_type in {"mysql", "mysql+pymysql"}:
        url = os.getenv("DATABASE_URL") or f"mysql+pymysql://{config.path}"
        engine = create_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=_DEFAULT_POOL_SIZE,
            max_overflow=_DEFAULT_MAX_OVERFLOW,
        )
        return engine

    raise ValueError(f"Unsupported database type: {config.type}")


class DatabaseSessionManager:
    """Context-managed database sessions with retry for transient failures."""

    def __init__(
        self,
        engine: Engine,
        expire_on_commit: bool = False,
        pool_size: int = _DEFAULT_POOL_SIZE,
        max_overflow: int = _DEFAULT_MAX_OVERFLOW,
    ) -> None:
        self.engine = engine
        # Configure sessionmaker; pooling handled at engine creation time.
        self._session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=expire_on_commit,
        )

    @contextmanager
    def session_scope(
        self,
        retries: int = 3,
        backoff_seconds: float = 0.5,
        logger: Optional[object] = None,
    ) -> Generator[Session, None, None]:
        """Provide a transactional scope with retry for lock/timeouts/deadlocks."""
        attempt = 0
        while True:
            attempt += 1
            session: Session = self._session_factory()
            try:
                yield session
                session.commit()
                return
            except (OperationalError, DBAPIError) as exc:
                session.rollback()
                message = str(exc).lower()
                is_lock = "database is locked" in message or "deadlock" in message or "locked" in message
                is_timeout = "timeout" in message or "timed out" in message
                if attempt < retries and is_lock:
                    sleep_for = backoff_seconds * attempt
                    if logger:
                        logger.warning(
                            "Database lock detected; retrying",
                            extra={"component": "db", "attempt": attempt, "error": str(exc)},
                        )
                    time.sleep(sleep_for)
                    continue
                if attempt < retries and is_timeout:
                    sleep_for = backoff_seconds * attempt
                    if logger:
                        logger.warning(
                            "Database timeout detected; retrying",
                            extra={"component": "db", "attempt": attempt, "error": str(exc)},
                        )
                    time.sleep(sleep_for)
                    continue
                if logger:
                    logger.error(
                        "Operational/DBAPI error during DB session",
                        extra={"component": "db", "attempt": attempt, "error": str(exc)},
                    )
                raise
            except SQLAlchemyError as exc:
                session.rollback()
                if logger:
                    logger.error(
                        "SQLAlchemy error during DB session",
                        extra={"component": "db", "attempt": attempt, "error": str(exc)},
                    )
                raise
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()


def init_db(engine: Engine, create_all: bool = True) -> None:
    """Initialize the database schema."""
    if create_all:
        Base.metadata.create_all(engine)


def get_session_manager_from_config(config: DatabaseConfig, echo: bool = False) -> DatabaseSessionManager:
    engine = create_engine_from_config(config, echo=echo)
    return DatabaseSessionManager(engine=engine)
