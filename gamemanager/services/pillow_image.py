from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO
import warnings

from PIL import Image, ImageOps


@contextmanager
def suppress_ico_size_warning():
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Image was not the expected size",
            category=UserWarning,
            module=r"PIL\.IcoImagePlugin",
        )
        yield


def load_image_rgba_bytes(
    image_bytes: bytes,
    *,
    preferred_ico_size: int | None = None,
) -> Image.Image:
    with suppress_ico_size_warning():
        with Image.open(BytesIO(image_bytes)) as image:
            if str(getattr(image, "format", "") or "").upper() != "ICO":
                image.load()
                image = ImageOps.exif_transpose(image)
                return image.convert("RGBA")

            sizes = sorted(
                {
                    int(width)
                    for width, height in (image.info.get("sizes") or set())
                    if int(width) > 0 and int(height) > 0 and int(width) == int(height)
                },
                reverse=True,
            )
            trial_sizes: list[int] = []
            if preferred_ico_size is not None and int(preferred_ico_size) > 0:
                trial_sizes.append(int(preferred_ico_size))
            trial_sizes.extend(sizes)

            seen: set[int] = set()
            for size in trial_sizes:
                if size in seen:
                    continue
                seen.add(size)
                try:
                    if sizes and size in sizes:
                        image.size = (int(size), int(size))
                    image.load()
                    return image.convert("RGBA")
                except Exception:
                    continue

            image.load()
            return image.convert("RGBA")

