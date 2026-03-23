from __future__ import annotations

import argparse
from pathlib import Path

from gamemanager.services.image_prep import SUPPORTED_IMAGE_EXTENSIONS
from gamemanager.services.template_transparency import (
    TemplateTransparencyOptions,
    process_template_file,
)


def _parse_color(value: str | None) -> tuple[int, int, int] | None:
    if not value:
        return None
    chunks = [token.strip() for token in value.split(",")]
    if len(chunks) != 3:
        raise ValueError("Background color must be 'R,G,B'.")
    rgb = tuple(max(0, min(255, int(token))) for token in chunks)
    return rgb  # type: ignore[return-value]


def _iter_inputs(path: Path, recursive: bool) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS else []
    if not path.is_dir():
        return []
    iterator = path.rglob("*") if recursive else path.glob("*")
    files: list[Path] = []
    for child in iterator:
        if child.is_file() and child.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
            files.append(child)
    return sorted(files, key=lambda p: p.name.casefold())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-convert opaque/near-opaque template backgrounds to transparent PNG, "
            "using edge-connected flood fill."
        )
    )
    parser.add_argument("--input", required=True, help="Input file or directory.")
    parser.add_argument("--output", required=True, help="Output directory.")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recurse into subfolders for directory input.",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=22,
        help="Color threshold (0-255). Default: 22.",
    )
    parser.add_argument(
        "--mode",
        choices=["max", "euclidean"],
        default="max",
        help="Color distance mode. Default: max.",
    )
    parser.add_argument(
        "--global",
        dest="use_global",
        action="store_true",
        help="Use global color removal (not edge-connected).",
    )
    parser.add_argument(
        "--background-color",
        default="",
        help="Optional fixed background color as R,G,B. Default uses border median.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    source = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        background_color = _parse_color(args.background_color)
    except ValueError as exc:
        print(f"Invalid --background-color: {exc}")
        return 2

    options = TemplateTransparencyOptions(
        threshold=max(0, min(255, int(args.threshold))),
        color_tolerance_mode=str(args.mode),
        use_edge_flood_fill=not bool(args.use_global),
        preserve_existing_alpha=True,
    )

    inputs = _iter_inputs(source, recursive=bool(args.recursive))
    if not inputs:
        print("No supported input images found.")
        return 0

    failed = 0
    for item in inputs:
        target = output_dir / f"{item.stem}.png"
        try:
            process_template_file(
                str(item),
                str(target),
                options=options,
                background_color=background_color,
            )
            print(f"OK: {item.name} -> {target.name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL: {item.name} ({exc})")

    print(f"Processed: {len(inputs)} | Failed: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

