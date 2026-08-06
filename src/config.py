from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .safety import SafetySettings, load_safety_settings


@dataclass(frozen=True)
class AppConfig:
    raw: dict[str, Any]
    base_dir: Path
    safety: SafetySettings

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name)
        if not isinstance(value, dict):
            raise ValueError(f"Config section '{name}' is required and must be a mapping")
        return value


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    text = path.read_text(encoding="utf-8")
    raw = _load_mapping(text)

    if not isinstance(raw, dict):
        raise ValueError("Config root must be a mapping")

    safety = load_safety_settings(raw)
    return AppConfig(raw=raw, base_dir=path.resolve().parent, safety=safety)


def _load_mapping(text: str) -> dict[str, Any]:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text) or {}
    except ImportError:
        return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    pending_key: list[tuple[int, dict[str, Any], str]] = []

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        while pending_key and indent <= pending_key[-1][0]:
            pending_key.pop()

        if line.startswith("- "):
            item_text = line[2:].strip()
            parent = stack[-1][1]
            if not isinstance(parent, list):
                if not pending_key:
                    raise ValueError(f"List item has no parent: {raw_line}")
                _, owner, key = pending_key[-1]
                parent = []
                owner[key] = parent
                stack.append((indent - 1, parent))

            if ":" in item_text:
                key, value = item_text.split(":", 1)
                item: dict[str, Any] = {key.strip(): _parse_scalar(value.strip())}
                parent.append(item)
                stack.append((indent, item))
            else:
                parent.append(_parse_scalar(item_text))
            continue

        if ":" not in line:
            raise ValueError(f"Unsupported config line: {raw_line}")

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        parent = stack[-1][1]
        if not isinstance(parent, dict):
            raise ValueError(f"Mapping item has non-mapping parent: {raw_line}")

        if value:
            parent[key] = _parse_scalar(value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            pending_key.append((indent, parent, key))
            stack.append((indent, child))

    return root


def _parse_scalar(value: str) -> Any:
    if value in {"true", "false"}:
        return value == "true"
    if value in {'""', "''"}:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
