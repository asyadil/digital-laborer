"""Entry point for the Referral Automation System."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from src.core.orchestrator import SystemOrchestrator


def main() -> None:
    parser = argparse.ArgumentParser(description="Referral Automation System")
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Skip startup pre-flight validation (emergency only).",
    )
    parser.add_argument(
        "--config",
        default=os.getenv("CONFIG_PATH", os.path.join("config", "config.yaml")),
        help="Path to YAML configuration file.",
    )
    args = parser.parse_args()

    load_dotenv()
    config_path = args.config
    orchestrator = SystemOrchestrator(config_path=config_path, skip_validation=args.skip_validation)
    asyncio.run(orchestrator.start())


if __name__ == "__main__":
    main()
