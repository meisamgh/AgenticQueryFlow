"""Shared containment and identity checks for JSON sidecar stores."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel


def sidecar_write_path(root: Path, filename: str) -> Path:
    """Only direct child names may be written; never follow a file symlink."""
    if (not filename or Path(filename).name != filename or "\\" in filename
            or filename in {".", ".."}):
        raise ValueError("Sidecar writes require a direct child filename.")
    root = root.resolve()
    path = root / filename
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("Sidecar writes cannot follow symlinks outside their target.")
    return path


def atomic_write_sidecar(root: Path, filename: str, payload: str) -> Path:
    """Publish complete JSON or retain the prior file on failure.

    The temporary file is in the same directory/filesystem as the destination.
    This prevents partial readers; it is not a lock against trusted concurrent
    writers (the last successful replacement wins).
    """
    path = sidecar_write_path(root, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


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


def load_sidecar[SidecarModel: BaseModel](
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
