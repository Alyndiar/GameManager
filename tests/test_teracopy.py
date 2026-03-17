import os
from pathlib import Path

from gamemanager.services import teracopy


def test_resolve_teracopy_prefers_existing_preferred_path(
    tmp_path: Path, monkeypatch
) -> None:
    preferred = tmp_path / "TeraCopy.exe"
    preferred.write_text("x", encoding="utf-8")
    monkeypatch.setattr(teracopy, "FALLBACK_TERACOPY_PATHS", [])
    monkeypatch.setattr(teracopy.shutil, "which", lambda _: None)

    assert teracopy.resolve_teracopy_path(str(preferred)) == os.path.normpath(
        str(preferred)
    )


def test_resolve_teracopy_uses_fallback_when_preferred_missing(
    tmp_path: Path, monkeypatch
) -> None:
    fallback = tmp_path / "fallback.exe"
    fallback.write_text("x", encoding="utf-8")
    monkeypatch.setattr(teracopy, "FALLBACK_TERACOPY_PATHS", [str(fallback)])
    monkeypatch.setattr(teracopy.shutil, "which", lambda _: None)

    assert teracopy.resolve_teracopy_path("Z:\\missing\\TeraCopy.exe") == os.path.normpath(
        str(fallback)
    )


def test_resolve_teracopy_uses_path_lookup_when_no_file_candidates(monkeypatch) -> None:
    monkeypatch.setattr(teracopy, "FALLBACK_TERACOPY_PATHS", [])
    monkeypatch.setattr(
        teracopy.shutil,
        "which",
        lambda name: r"C:\Tools\TeraCopy.exe" if name.lower() == "teracopy.exe" else None,
    )

    assert teracopy.resolve_teracopy_path(None) == os.path.normpath(
        r"C:\Tools\TeraCopy.exe"
    )
