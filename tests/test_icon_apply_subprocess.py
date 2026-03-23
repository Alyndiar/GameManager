from pathlib import Path
from types import SimpleNamespace

from gamemanager.services.icon_apply_subprocess import apply_folder_icon_in_subprocess


def test_apply_folder_icon_in_subprocess_parses_worker_json(
    tmp_path: Path, monkeypatch
) -> None:
    def _fake_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout='{"folder_path":"C:/Games/A","status":"applied","message":"ok","ico_path":"C:/Games/A/A.ico","desktop_ini_path":"C:/Games/A/desktop.ini"}\n',
            stderr="",
        )

    monkeypatch.setattr(
        "gamemanager.services.icon_apply_subprocess.subprocess.run", _fake_run
    )
    result = apply_folder_icon_in_subprocess(
        folder_path=Path("C:/Games/A"),
        source_image=b"abc",
        icon_name_hint="A",
        info_tip=None,
        icon_style="none",
        bg_removal_engine="none",
        bg_removal_params=None,
        text_preserve_config=None,
        border_shader=None,
        temp_dir=tmp_path,
    )
    assert result.status == "applied"
    assert result.ico_path == "C:/Games/A/A.ico"


def test_apply_folder_icon_in_subprocess_handles_launch_error(
    tmp_path: Path, monkeypatch
) -> None:
    def _fake_run(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(
        "gamemanager.services.icon_apply_subprocess.subprocess.run", _fake_run
    )
    result = apply_folder_icon_in_subprocess(
        folder_path=Path("C:/Games/A"),
        source_image=b"abc",
        icon_name_hint="A",
        info_tip=None,
        icon_style="none",
        bg_removal_engine="none",
        bg_removal_params=None,
        text_preserve_config=None,
        border_shader=None,
        temp_dir=tmp_path,
    )
    assert result.status == "failed"
    assert "Could not launch icon apply worker" in result.message
