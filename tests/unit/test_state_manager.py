from src.database.operations import create_engine_from_config, init_db, get_session_manager_from_config
from src.utils.config_loader import DatabaseConfig
from src.core.state_manager import StateManager


def test_state_manager_roundtrip(tmp_path):
    db_path = tmp_path / "state.db"
    cfg = DatabaseConfig(path=str(db_path))
    engine = create_engine_from_config(cfg)
    init_db(engine)

    mgr = get_session_manager_from_config(cfg)
    sm = StateManager(db=mgr)

    assert sm.get_state("k1") is None
    assert sm.set_state("k1", {"a": 1}) is True
    snap = sm.get_state("k1")
    assert snap is not None
    assert snap.value["a"] == 1
    assert sm.delete_state("k1") is True
    assert sm.get_state("k1") is None
