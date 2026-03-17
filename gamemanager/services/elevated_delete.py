from __future__ import annotations

import argparse
import os
import shutil
import stat
from pathlib import Path


def _remove_readonly_and_retry(func, path, exc_info):
    _exc_type, exc, _tb = exc_info
    if isinstance(exc, PermissionError):
        os.chmod(path, stat.S_IWRITE)
        func(path)
        return
    raise exc


def delete_path(path_value: str) -> None:
    path = Path(path_value)
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, onerror=_remove_readonly_and_retry)
        return
    try:
        path.unlink()
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Delete file/folder path.")
    parser.add_argument("--path", required=True, help="Path to delete")
    args = parser.parse_args()
    try:
        delete_path(args.path)
        return 0
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

