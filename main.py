"""Entry point for the Referral Automation System."""
from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from src.core.orchestrator import SystemOrchestrator


def main() -> None:
    load_dotenv()
    config_path = os.getenv("CONFIG_PATH", os.path.join("config", "config.yaml"))
    orchestrator = SystemOrchestrator(config_path=config_path)
    asyncio.run(orchestrator.start())


if __name__ == "__main__":
    main()
