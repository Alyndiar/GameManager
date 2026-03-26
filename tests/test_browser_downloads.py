from __future__ import annotations

from pathlib import Path

from gamemanager.services import browser_downloads as bd


def test_browser_id_from_string_supports_edge_chromium_firefox() -> None:
    assert bd._browser_id_from_string("MSEdgeHTM") == "edge"
    assert bd._browser_id_from_string("ChromeHTML") == "chrome"
    assert bd._browser_id_from_string("ChromiumHTM") == "chromium"
    assert bd._browser_id_from_string("FirefoxURL") == "firefox"
    assert bd._browser_id_from_string("BraveHTML") == "brave"


def test_detect_browser_download_dir_prefers_default_firefox(
    monkeypatch,
    tmp_path: Path,
) -> None:
    hit = bd.BrowserDownloadDetection(
        browser_id="firefox",
        browser_label="Mozilla Firefox",
        download_dir=tmp_path,
        source="profiles.ini",
    )
    monkeypatch.setattr(bd, "_detect_default_browser_id", lambda: "firefox")
    monkeypatch.setattr(bd, "_detect_firefox_download_dir", lambda: hit)
    monkeypatch.setattr(bd, "_detect_chromium_download_dir", lambda _browser: None)
    detected = bd.detect_browser_download_dir()
    assert detected == hit


def test_detect_browser_download_dir_prefers_default_chromium(
    monkeypatch,
    tmp_path: Path,
) -> None:
    hit = bd.BrowserDownloadDetection(
        browser_id="edge",
        browser_label="Microsoft Edge",
        download_dir=tmp_path,
        source="Local State",
    )
    monkeypatch.setattr(bd, "_detect_default_browser_id", lambda: "edge")
    monkeypatch.setattr(
        bd,
        "_detect_chromium_download_dir",
        lambda browser: hit if browser == "edge" else None,
    )
    monkeypatch.setattr(bd, "_detect_firefox_download_dir", lambda: None)
    detected = bd.detect_browser_download_dir()
    assert detected == hit


def test_detect_browser_download_dir_falls_back_when_detection_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(bd, "_detect_default_browser_id", lambda: "")
    monkeypatch.setattr(bd, "_detect_chromium_download_dir", lambda _browser: None)
    monkeypatch.setattr(bd, "_detect_firefox_download_dir", lambda: None)
    monkeypatch.setattr(bd, "default_downloads_dir", lambda: tmp_path)
    detected = bd.detect_browser_download_dir()
    assert detected.browser_id == "fallback"
    assert detected.download_dir == tmp_path
    assert detected.source == "fallback"


def test_extract_firefox_prefs() -> None:
    content = (
        'user_pref("browser.download.folderList", 2);\n'
        'user_pref("browser.download.dir", "C:\\\\Games\\\\Downloads");\n'
    )
    assert bd._extract_firefox_pref_int(content, "browser.download.folderList") == 2
    assert (
        bd._extract_firefox_pref_string(content, "browser.download.dir")
        == r"C:\Games\Downloads"
    )

