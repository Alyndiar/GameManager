import io
import subprocess
import time

from PIL import Image
from gamemanager.services.background_removal import preload_background_models, remove_background_bytes
from gamemanager.services.icon_pipeline import preload_text_models, build_text_extraction_overlay


def mem_used_mb() -> int:
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        text=True,
    )
    vals = [int(x.strip()) for x in out.splitlines() if x.strip()]
    return vals[0] if vals else -1

base = mem_used_mb()
print(f"baseline_mb={base}")

print("preload_bg", preload_background_models())
print("preload_text", preload_text_models())
after_preload = mem_used_mb()
print(f"after_preload_mb={after_preload}")

img = Image.new("RGBA", (512, 512), (12, 22, 32, 255))
b = io.BytesIO()
img.save(b, format="PNG")
payload = b.getvalue()
_ = remove_background_bytes(payload, "rembg")
_ = remove_background_bytes(payload, "bria_rmbg")

src = Image.new("RGBA", (512, 512), (5, 5, 5, 255))
cut = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
_ = build_text_extraction_overlay(
    src,
    cut,
    {"enabled": True, "method": "paddleocr", "strength": 70, "feather": 1},
)

time.sleep(1.0)
after_warm = mem_used_mb()
print(f"after_warm_mb={after_warm}")
print(f"delta_preload_mb={after_preload - base}")
print(f"delta_warm_mb={after_warm - base}")
