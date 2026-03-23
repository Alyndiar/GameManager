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

## Batch Image Prep (To 512x512 PNG)

Crop images to the used area, keep transparent background, and normalize to 512x512 PNG.

```powershell
conda run -n GameManager python -m gamemanager.tools.image_prep_batch `
  --input "E:\Downloads\Icons" `
  --output-dir "W:\Dany\GameManager\PreparedIcons" `
  --recursive `
  --size 512 `
  --padding-ratio 0.00 `
  --min-padding-px 1
```
