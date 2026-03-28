from gamemanager.runtime import gamemanager_app_icon_path


def test_gamemanager_app_icon_path_exists() -> None:
    path = gamemanager_app_icon_path()
    assert path.name == "GameManager.ico"
    assert path.exists()
    assert path.is_file()
