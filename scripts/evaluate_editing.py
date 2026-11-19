"""Measure a photo, resolve the corrections, render, and re-measure.

This is the regression harness for editing quality: the preview used to lift
black to grey, so every run prints the before/after shadows percentile and
fails when the render destroys the black point or clips the highlights.

Examples:
    python scripts/evaluate_editing.py --photo uploads/<id>/display.jpg
    python scripts/evaluate_editing.py --photo photo.jpg --exposure 0.4 --shadows 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.schemas import EditingParametersV1
from app.services.edit_resolver import make_render_observer, resolve_parameters
from app.services.measurements import ImageMeasurements, measure_file, measure_image
from app.services.preview import render_image


#: How much the darkest 1% is allowed to rise. The v3 renderer lifted it by
#: +33 levels on the reference night portrait, which is what made every preview
#: look like it had a grey filter over it.
MAX_SHADOW_LIFT = 8.0
#: Highlights must stay under this percentile after rendering.
MAX_RENDERED_P95 = 250.0
#: Share of the frame allowed to sit at the highlight clip level.
MAX_HIGHLIGHT_CLIP_SHARE = 0.05

SCALAR_FIELDS = (
    "exposure",
    "contrast",
    "highlights",
    "shadows",
    "whites",
    "blacks",
    "temperature",
    "tint",
    "vibrance",
    "saturation",
    "clarity",
    "dehaze",
    "texture",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--photo", required=True, type=Path, help="Path to the photo to evaluate.")
    parser.add_argument("--intensity", type=float, default=1.0, help="Slider intensity, 0..1.")
    for field in SCALAR_FIELDS:
        parser.add_argument(f"--{field}", type=float, default=0.0)
    parser.add_argument("--verbose", action="store_true", help="Print the automatic baseline corrections.")
    return parser


def format_measurements(label: str, measurements: ImageMeasurements) -> str:
    luma = measurements.luma
    return (
        f"{label}: p01={luma.p01} p05={luma.p05} p50={luma.p50} "
        f"p95={luma.p95} p99={luma.p99} "
        f"clip_black={measurements.clipped_shadows_share * 100:.3f}% "
        f"clip_white={measurements.clipped_highlights_share * 100:.3f}%"
    )


def format_parameters(parameters: EditingParametersV1) -> str:
    parts = []
    for field in SCALAR_FIELDS:
        value = getattr(parameters, field)
        if value:
            parts.append(f"{field}={value}")
    if parameters.sharpening.amount:
        parts.append(f"sharpening={parameters.sharpening.amount}")
    if parameters.noise_reduction.luminance or parameters.noise_reduction.color:
        parts.append(
            f"noise_reduction={parameters.noise_reduction.luminance}/"
            f"{parameters.noise_reduction.color}"
        )
    return " ".join(parts) if parts else "(neutral)"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    photo = args.photo.expanduser()
    if not photo.is_file():
        print(f"photo not found: {photo}", file=sys.stderr)
        return 2

    style = EditingParametersV1(
        **{field: getattr(args, field) for field in SCALAR_FIELDS}
    )
    source = measure_file(photo)
    print(f"photo: {photo}")
    print(format_measurements("source", source))

    resolution = resolve_parameters(
        measurements=source,
        style_parameters=style,
        intensity=args.intensity,
        observer=make_render_observer(photo),
    )
    report = resolution.report
    if report is not None:
        print(f"intensity={report.intensity} baseline_factor={report.baseline_factor:.3f}")
        print(
            f"adaptive strength={report.strength:.2f} visible={report.visible}"
            + (f" guards={'; '.join(report.guards)}" if report.guards else "")
        )
        if args.verbose:
            print(f"baseline: {report.baseline.notes() or '(none)'}")
            print(f"targets: p05={report.targets.p05:.0f} p50={report.targets.p50:.0f} p95={report.targets.p95:.0f}")
        print(f"rounds={report.rounds} verified={report.verified}")
        for warning in report.warnings:
            print(f"warning: {warning}")
    print(f"resolved: {format_parameters(resolution.parameters)}")

    rendered = render_image(_load(photo), resolution.parameters)
    rendered_measurements = measure_image(rendered)
    print(format_measurements("rendered", rendered_measurements))

    shadow_lift = rendered_measurements.luma.p01 - source.luma.p01
    checks = (
        ("黑位未被抬灰", shadow_lift <= MAX_SHADOW_LIFT, f"p01 {shadow_lift:+.0f} (<= {MAX_SHADOW_LIFT:.0f})"),
        ("高光未溢出", rendered_measurements.luma.p95 <= MAX_RENDERED_P95, f"p95 {rendered_measurements.luma.p95:.0f} (<= {MAX_RENDERED_P95:.0f})"),
        (
            "高光剪切受控",
            rendered_measurements.clipped_highlights_share <= MAX_HIGHLIGHT_CLIP_SHARE,
            f"{rendered_measurements.clipped_highlights_share * 100:.2f}% (<= {MAX_HIGHLIGHT_CLIP_SHARE * 100:.0f}%)",
        ),
    )
    for label, passed, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    return 0 if all(passed for _, passed, _ in checks) else 1


def _load(path: Path):
    from PIL import Image, ImageOps

    from app.imaging import flatten_to_rgb

    with Image.open(path) as source:
        return flatten_to_rgb(ImageOps.exif_transpose(source)).convert("RGB")


if __name__ == "__main__":
    raise SystemExit(main())
