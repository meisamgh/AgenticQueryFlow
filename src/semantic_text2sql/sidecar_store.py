"""Shared containment and identity checks for JSON sidecar stores."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

SidecarModel = TypeVar("SidecarModel", bound=BaseModel)


def contained_sidecar_path(root: Path | None, filename: str) -> Path | None:
    """Return a direct child file under ``root``, or ``None`` if it escapes."""
    if root is None:
        return None
    try:
        path = (root / filename).resolve()
        if path.parent != root or not path.is_file():
            return None
        return path
    except (OSError, RuntimeError):
        return None


def load_sidecar(
    path: Path,
    model: type[SidecarModel],
    identity_field: str,
    identity: str,
) -> SidecarModel | None:
    """Load a sidecar only when its embedded identity is absent or matching."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    embedded = payload.get(identity_field)
    if embedded is not None and embedded != identity:
        return None
    payload.setdefault(identity_field, identity)
    try:
        return model.model_validate(payload)
    except ValueError:
        return None
