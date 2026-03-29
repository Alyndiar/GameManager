from gamemanager.services.icon_sources import IconSearchSettings
from gamemanager.services import steamgriddb_upload as upload


def _settings() -> IconSearchSettings:
    return IconSearchSettings(
        steamgriddb_enabled=True,
        steamgriddb_api_key="k",
    )


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int,
        payload: dict | None = None,
        text: str = "",
    ) -> None:
        self.status_code = int(status_code)
        self._payload = payload
        self.text = text
        self.content = b"{}" if payload is not None else (text.encode("utf-8") if text else b"")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_upload_icon_sends_expected_multipart_payload(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeSession:
        def post(self, url, headers=None, data=None, files=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["data"] = data or {}
            captured["files"] = files or {}
            captured["timeout"] = timeout
            return _FakeResponse(status_code=200, payload={"success": True})

    monkeypatch.setattr(upload, "_session", lambda: _FakeSession())

    upload.upload_icon(_settings(), 435100, b"\x89PNG\r\n\x1a\n...")

    assert str(captured["url"]).endswith("/icons")
    data = dict(captured["data"])  # type: ignore[arg-type]
    assert data.get("game_id") == 435100
    assert data.get("style") == "custom"
    files = dict(captured["files"])  # type: ignore[arg-type]
    assert "asset" in files
    file_tuple = files["asset"]
    assert isinstance(file_tuple, tuple)
    assert file_tuple[0] == "icon.png"
    assert file_tuple[2] == "image/png"


def test_upload_icon_surfaces_api_errors_on_400(monkeypatch) -> None:
    class _FakeSession:
        def post(self, url, headers=None, data=None, files=None, timeout=None):
            return _FakeResponse(
                status_code=400,
                payload={"success": False, "errors": ["Image dimensions are invalid."]},
            )

    monkeypatch.setattr(upload, "_session", lambda: _FakeSession())

    try:
        upload.upload_icon(_settings(), 435100, b"\x89PNG\r\n\x1a\n...")
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected RuntimeError")

    assert "400" in message
    assert "Image dimensions are invalid." in message
