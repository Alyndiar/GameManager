# Game Backup Manager

Desktop app for managing game backup roots, tagging, cleanup, and archive moves.

## Run

```powershell
conda run -n GameManager python -m pip install -r requirements-dev.txt
conda run -n GameManager python -m gamemanager
```

Or, after activating the environment:

```powershell
conda activate GameManager
python -m pip install -r requirements-dev.txt
python -m gamemanager
```

## Test

```powershell
conda run -n GameManager pytest
```
