from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.config import load_config
from src.scalp_stream import AngelOneScalpRuntime


def main() -> None:
    parser = argparse.ArgumentParser(description="Release 6 paper-only NIFTY futures scalp shadow")
    parser.add_argument("--config", default="config.example.yaml", help="Path to config YAML")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Authenticate, resolve instruments and validate configuration without opening the stream",
    )
    args = parser.parse_args()

    config = load_config(Path(args.config))
    runtime = AngelOneScalpRuntime(config)
    manifest = runtime.prepare()
    print(json.dumps(manifest, indent=2, default=str))
    if not args.prepare_only:
        runtime.run_forever()


if __name__ == "__main__":
    main()
