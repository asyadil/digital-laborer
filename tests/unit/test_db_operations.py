from pathlib import Path

from sqlalchemy import inspect

from src.database.models import Account, AccountStatus, AccountType, Base
from src.database.operations import create_engine_from_config, get_session_manager_from_config, init_db
from src.utils.config_loader import DatabaseConfig


def test_init_db_creates_tables(tmp_path):
    db_path = tmp_path / "test.db"
    config = DatabaseConfig(path=str(db_path))
    engine = create_engine_from_config(config)
    init_db(engine)
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    expected = {
        "accounts",
        "posts",
        "referral_links",
        "telegram_interactions",
        "system_metrics",
        "error_log",
    }
    assert expected.issubset(set(tables))


def test_session_manager_commit(tmp_path):
    db_path = tmp_path / "commit.db"
    config = DatabaseConfig(path=str(db_path))
    engine = create_engine_from_config(config)
    init_db(engine)
    session_manager = get_session_manager_from_config(config)
    with session_manager.session_scope() as session:
        account = Account(
            platform=AccountType.REDDIT,
            username="user1",
            password_encrypted="secret",
            status=AccountStatus.active,
        )
        session.add(account)
    with session_manager.session_scope() as session:
        stored = session.query(Account).filter_by(username="user1").one()
        assert stored.id is not None
        assert stored.platform == AccountType.REDDIT
