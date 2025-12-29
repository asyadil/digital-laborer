import contextlib
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.monitoring.health_checker import HealthChecker
from src.database.models import Account, AccountStatus


class FakeDB:
    def __init__(self, sessions):
        self._sessions = sessions

    @contextlib.contextmanager
    def session_scope(self, *args, **kwargs):
        yield self._sessions.pop(0)


def make_session(active_accounts, flagged_accounts):
    session = MagicMock()

    active_query = MagicMock()
    active_query.filter.return_value = active_query
    active_query.all.return_value = active_accounts

    flagged_query = MagicMock()
    flagged_query.filter.return_value = flagged_query
    flagged_query.all.return_value = flagged_accounts

    def query_side_effect(model):
        if model == Account:
            # Return active query first, then flagged query.
            return active_query if not session.query.call_count else flagged_query
        raise ValueError("Unexpected model")

    session.query.side_effect = query_side_effect
    return session


def new_checker(active_accounts, flagged_accounts):
    session = make_session(active_accounts, flagged_accounts)
    fake_db = FakeDB([session])
    logger = MagicMock()
    telegram = MagicMock()
    return HealthChecker(db=fake_db, telegram=telegram, logger=logger)


def test_check_platforms_no_active_accounts():
    checker = new_checker(active_accounts=[], flagged_accounts=[])
    result = asyncio.run(checker.check_platform_adapters())
    # For no active accounts, a single HealthCheckResult is returned (platforms component)
    from src.monitoring.health_checker import HealthCheckResult
    assert isinstance(result, HealthCheckResult)
    assert result.status in {"degraded", "unhealthy"}
