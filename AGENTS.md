# Project Agent Instructions

## Environment Invariant (Critical)

Always run project commands in the `GameManager` conda environment.

- Required default for Python/pip/pytest/scripts:
  - `conda run -n GameManager <command>`
- Do not rely on the currently active shell environment (`base` or others).
- If `conda` is unavailable on a machine, use a project-local `.venv` and run all commands from that venv.

Examples:

```powershell
conda run -n GameManager python -m gamemanager
conda run -n GameManager python -m iconmaker_gui
conda run -n GameManager python -m pytest -q
```

Preferred test entrypoint:

```powershell
.\tools\run_tests.ps1
```

This script already enforces `conda run -n GameManager`.
