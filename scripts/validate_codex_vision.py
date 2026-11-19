from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import ExitStack
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, ImageDraw

from app.adapters.codex_analysis import CodexAnalysisProvider
from app.adapters.codex_learning import CodexLearningAnalysisProvider
from app.schemas import ExportRequest, PreviewRequest
from app.services.storage import FileStorage
from app.services.workflows import LearningWorkflow, PhotoWorkflow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run live Codex Phase 1/2 validation using synthetic images only."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--codex-executable", type=Path)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--phase",
        choices=("phase1", "phase2", "all"),
        default="all",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    samples_dir = output_dir / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    sample_paths = _create_synthetic_samples(samples_dir)
    storage = FileStorage(output_dir / "workspace")

    photo_provider = CodexAnalysisProvider(
        data_root=storage.root,
        executable=args.codex_executable,
        model=args.model,
        timeout_seconds=args.timeout,
    )
    learning_provider = CodexLearningAnalysisProvider(
        data_root=storage.root,
        executable=args.codex_executable,
        model=args.model,
        timeout_seconds=args.timeout,
    )
    report: dict[str, object] = {
        "fixture_policy": "synthetic-images-only",
        "model": args.model,
        "samples": [path.name for path in sample_paths],
    }

    if args.phase in {"phase1", "all"}:
        report["phase1"] = _run_phase1(storage, photo_provider, sample_paths[0])
        _write_report(output_dir, report)
    if args.phase in {"phase2", "all"}:
        report["phase2"] = _run_phase2(storage, learning_provider, sample_paths)
        _write_report(output_dir, report)

    print(json.dumps(report, ensure_ascii=False, indent=2))


def _run_phase1(
    storage: FileStorage,
    provider: CodexAnalysisProvider,
    sample_path: Path,
) -> dict[str, object]:
    workflow = PhotoWorkflow(storage=storage, analysis_provider=provider)
    with sample_path.open("rb") as source:
        session = asyncio.run(
            workflow.upload(UploadFile(file=source, filename=sample_path.name))
        )
    analysis = workflow.analyze(session.id)
    assets: list[dict[str, object]] = []
    for style in analysis.recommended_styles:
        preview = workflow.preview(
            session.id,
            PreviewRequest(style_id=style.id, intensity=0.7),
        )
        preset = workflow.export(
            session.id,
            ExportRequest(style_id=style.id, intensity=0.7),
        )
        assets.append(
            {
                "style_id": style.id,
                "preview_url": preview.preview_url,
                "preset_url": preset.download_url,
            }
        )
    return {
        "photo_id": session.id,
        "analysis": analysis.model_dump(mode="json"),
        "assets": assets,
    }


def _run_phase2(
    storage: FileStorage,
    provider: CodexLearningAnalysisProvider,
    sample_paths: list[Path],
) -> dict[str, object]:
    workflow = LearningWorkflow(storage=storage, analysis_provider=provider)
    session = workflow.create_session("冷暖色、视觉层级与主体引导")
    with ExitStack() as stack:
        for sample_path in sample_paths:
            source = stack.enter_context(sample_path.open("rb"))
            asyncio.run(
                workflow.add_reference(
                    session_id=session.id,
                    upload=UploadFile(file=source, filename=sample_path.name),
                    source_url=None,
                    author="AI Photo Agent synthetic fixture",
                    license_name="project-generated",
                )
            )
    analyzed = workflow.analyze(session.id)
    return {
        "session_id": analyzed.id,
        "topic": analyzed.topic,
        "references": [
            {
                "reference_id": reference.id,
                "sample": sample_paths[index].name,
                "analysis": reference.analysis.model_dump(mode="json"),
            }
            for index, reference in enumerate(analyzed.references)
            if reference.analysis is not None
        ],
        "summary": analyzed.summary.model_dump(mode="json")
        if analyzed.summary is not None
        else None,
    }


def _write_report(output_dir: Path, report: dict[str, object]) -> None:
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _create_synthetic_samples(samples_dir: Path) -> list[Path]:
    paths = [
        samples_dir / "daylight-landscape.png",
        samples_dir / "cool-low-key-still-life.png",
        samples_dir / "warm-minimal-interior.png",
    ]
    _draw_daylight_landscape(paths[0])
    _draw_cool_still_life(paths[1])
    _draw_warm_interior(paths[2])
    return paths


def _draw_daylight_landscape(path: Path) -> None:
    image = Image.new("RGB", (1200, 800), "#8dd4f5")
    draw = ImageDraw.Draw(image)
    draw.ellipse((930, 70, 1080, 220), fill="#ffd452")
    draw.polygon([(0, 530), (310, 230), (540, 530)], fill="#60758e")
    draw.polygon([(250, 530), (650, 160), (980, 530)], fill="#465d75")
    draw.polygon([(535, 270), (650, 160), (760, 300)], fill="#f7fbff")
    draw.rectangle((0, 520, 1200, 800), fill="#7daa58")
    draw.rectangle((430, 500, 680, 690), fill="#b94135")
    draw.polygon([(390, 500), (555, 380), (720, 500)], fill="#4d3028")
    draw.rectangle((525, 565, 590, 690), fill="#f0d29a")
    draw.line((80, 650, 1110, 610), fill="#e8d9a5", width=36)
    image.save(path)


def _draw_cool_still_life(path: Path) -> None:
    image = Image.new("RGB", (900, 900), "#091429")
    draw = ImageDraw.Draw(image)
    draw.polygon([(80, 760), (460, 110), (790, 760)], fill="#163f68")
    draw.ellipse((210, 270, 600, 660), fill="#c72f83")
    draw.rectangle((510, 210, 750, 690), fill="#23c6d5")
    draw.polygon([(0, 830), (900, 610), (900, 900), (0, 900)], fill="#020711")
    draw.line((70, 120, 830, 790), fill="#bceeff", width=18)
    image.save(path)


def _draw_warm_interior(path: Path) -> None:
    image = Image.new("RGB", (1000, 760), "#ead9bd")
    draw = ImageDraw.Draw(image)
    draw.rectangle((90, 70, 480, 470), fill="#f7bc72")
    draw.rectangle((115, 95, 455, 445), fill="#88bad1")
    draw.line((285, 95, 285, 445), fill="#f6e5c9", width=18)
    draw.line((115, 270, 455, 270), fill="#f6e5c9", width=18)
    draw.polygon([(520, 760), (660, 330), (820, 760)], fill="#7a4c32")
    draw.ellipse((565, 250, 690, 410), fill="#618552")
    draw.ellipse((680, 200, 835, 380), fill="#799b5f")
    draw.rectangle((590, 560, 790, 740), fill="#b86b45")
    draw.polygon([(480, 760), (850, 760), (970, 620), (610, 620)], fill="#c7a47b")
    image.save(path)


if __name__ == "__main__":
    main()
