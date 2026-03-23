from __future__ import annotations

import argparse
from pathlib import Path

from gamemanager.services.image_prep import (
    ImagePrepOptions,
    MIN_BLACK_LEVEL_MAX,
    prepare_images_to_512_png,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-crop images to content, keep transparent background, "
            "and export normalized PNG files."
        )
    )
    parser.add_argument(
        "-i",
        "--input",
        action="append",
        required=True,
        help="Input file or directory. Use multiple times for many paths.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        required=True,
        help="Destination folder for generated PNG files.",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=512,
        help="Output size in pixels (square). Default: 512.",
    )
    parser.add_argument(
        "--padding-ratio",
        type=float,
        default=0.0,
        help="Extra padding ratio around content before resize. Default: 0.0.",
    )
    parser.add_argument(
        "--min-padding-px",
        type=int,
        default=1,
        help="Minimum pixel padding around content when possible. Default: 1.",
    )
    parser.add_argument(
        "--alpha-threshold",
        type=int,
        default=8,
        help="Alpha threshold used for transparent-content detection. Default: 8.",
    )
    parser.add_argument(
        "--border-threshold",
        type=int,
        default=16,
        help="Difference threshold for opaque border trimming. Default: 16.",
    )
    parser.add_argument(
        "--min-black-level",
        type=int,
        default=0,
        help=(
            "If > 0, convert near-black border/background to transparency first "
            f"(clamped 0-{MIN_BLACK_LEVEL_MAX}). Default: 0."
        ),
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Search directories recursively for images.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output files.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).expanduser().resolve()
    options = ImagePrepOptions(
        output_size=max(32, int(args.size)),
        padding_ratio=max(0.0, min(0.5, float(args.padding_ratio))),
        min_padding_pixels=max(0, int(args.min_padding_px)),
        alpha_threshold=max(0, min(255, int(args.alpha_threshold))),
        border_threshold=max(0, min(255, int(args.border_threshold))),
        min_black_level=max(0, min(MIN_BLACK_LEVEL_MAX, int(args.min_black_level))),
        overwrite=bool(args.overwrite),
        recursive=bool(args.recursive),
    )
    report = prepare_images_to_512_png(
        input_paths=[str(value) for value in args.input],
        output_dir=str(output_dir),
        options=options,
    )

    print(f"Attempted: {report.attempted}")
    print(f"Succeeded: {report.succeeded}")
    print(f"Failed: {report.failed}")
    print(f"Skipped: {report.skipped}")
    if report.details:
        print("")
        print("Details:")
        for line in report.details[:50]:
            print(f"- {line}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
