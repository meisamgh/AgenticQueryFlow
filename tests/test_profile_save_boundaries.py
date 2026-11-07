"""Profile writes must preserve old data and remain inside their sidecar root."""
from pathlib import Path
from typing import Any

import pytest

from semantic_text2sql.models import DatabaseProfile
from semantic_text2sql.profiling import ProfileStore


def profile(version: str, db_id: str = "shop") -> DatabaseProfile:
    return DatabaseProfile(db_id=db_id, dialect="sqlite", profiled_at=version, columns=[])


def test_partial_write_failure_preserves_previous_profile(tmp_path: Path, monkeypatch: Any) -> None:
    store = ProfileStore(tmp_path)
    path = store.save(profile("old"))
    original_bytes = path.read_bytes()
    original_open = Path.open

    class PartialWrite:
        def __init__(self, handle: Any) -> None:
            self.handle = handle

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            self.handle.close()

        def write(self, value: str) -> int:
            self.handle.write(value[:5])
            self.handle.flush()
            raise OSError("Synthetic disk-full failure")

    def open_with_failure(target: Path, *args: Any, **kwargs: Any) -> Any:
        handle = original_open(target, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        return PartialWrite(handle) if mode == "w" and target.parent == tmp_path else handle

    monkeypatch.setattr(Path, "open", open_with_failure)
    with pytest.raises(OSError, match="disk-full"):
        store.save(profile("new"))
    assert path.read_bytes() == original_bytes
    assert not list(tmp_path.glob("*.tmp"))
    restored = ProfileStore(tmp_path).load("sqlite", "shop")
    assert restored is not None and restored.profiled_at == "old"


def test_save_does_not_follow_a_symlink_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    root.mkdir()
    outside = tmp_path / "unrelated.json"
    outside.write_text("keep")
    (root / "sqlite__shop.json").symlink_to(outside)
    with pytest.raises(ValueError, match="[Ss]idecar|[Ss]ymlink"):
        ProfileStore(root).save(profile("new"))
    assert outside.read_text() == "keep"


def test_replace_failure_preserves_existing_profile(tmp_path: Path, monkeypatch: Any) -> None:
    store = ProfileStore(tmp_path)
    path = store.save(profile("old"))
    original_bytes = path.read_bytes()

    def reject_replace(*args: Any) -> None:
        raise OSError("Synthetic replacement failure")

    monkeypatch.setattr("semantic_text2sql.sidecar_store.os.replace", reject_replace)
    with pytest.raises(OSError, match="replacement failure"):
        store.save(profile("new"))
    assert path.read_bytes() == original_bytes
    assert not list(tmp_path.glob("*.tmp"))
    restored = store.load("sqlite", "shop")
    assert restored is not None and restored.profiled_at == "old"


def test_save_rejects_path_components_in_profile_id(tmp_path: Path) -> None:
    root = tmp_path / "profiles"
    (root / "sqlite__x").mkdir(parents=True)
    with pytest.raises(ValueError, match="[Ss]idecar"):
        ProfileStore(root).save(profile("new", "x/../../escape"))
    assert not (tmp_path / "escape.json").exists()


def test_save_cannot_tag_one_version_with_another_writers_timestamp(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    store = ProfileStore(tmp_path)
    path = store.save(profile("old"))
    first, second = profile("first"), profile("second")
    first_text = first.model_dump_json(indent=2) + "\n"
    original_stat = Path.stat
    swapped = False

    def swap_during_stat(target: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal swapped
        if target == path and not swapped and path.read_text() == first_text:
            swapped = True
            path.write_text(second.model_dump_json(indent=2) + "\n")
        return original_stat(target, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", swap_during_stat)
    store.save(first)
    loaded = store.load("sqlite", "shop")
    assert swapped
    assert loaded is not None and loaded.profiled_at == "second"
