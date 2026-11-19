"""Anchor a model-authored edit to measured image statistics.

The vision model proposes a look; it has no way to read a histogram. This module
adds the part the model cannot do:

1. **Baseline corrections** derived from measurements (exposure anchor, shadow
   and highlight protection, flatness/contrast, white balance when the frame
   actually contains neutral surfaces).
2. **Soft-knee intensity** so a mid slider position still produces a visible
   edit without breaking the "intensity 0 == untouched" guarantee.
3. **A short measurement loop** that renders the candidate parameters at
   measuring resolution, measures the result, and nudges exposure until the
   rendered tone actually lands on the target instead of stopping at the model's
   guess.

Everything here is deterministic and pure Pillow; no optional dependencies.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from app.imaging import flatten_to_rgb
from app.schemas import EditingParametersV1
from app.services.measurements import (
    MEASUREMENT_MAX_SIZE,
    ImageMeasurements,
    measure_image,
)

EDIT_RESOLVER_VERSION = "edit_resolver_v1"

#: tanh curvature for the intensity slider. 1.6 keeps ``intensity=1`` exactly at
#: full strength while a half-way slider still delivers ~72% of the edit.
SOFT_KNEE_CURVE = 1.6

#: Verification loop budget and acceptance tolerances (0-255 luma units).
MAX_VERIFICATION_ROUNDS = 2
P50_TOLERANCE = 8.0
#: Highlights are guarded, not chased: the loop fails when the render blows out.
P95_CEILING = 250.0
MAX_BRIGHT_HIGHLIGHT_SHARE = 0.05

#: Safety rails so a runaway correction can never produce an absurd preset.
EXPOSURE_LIMIT = 1.5
EV_STEP_LIMIT = 0.6

#: Adaptive strength. A model style is authored for an "average" photo; on an
#: already bright frame the same sliders blow the highlights, on a dark frame
#: they are barely visible. We therefore render a descending ladder of strengths
#: and keep the strongest one that still passes the guards below.
STRENGTH_STEP = 0.1
MIN_STRENGTH = 0.3
#: Never scale the style below this fraction of what the user asked for: past it
#: the render stops looking like the chosen style at all. Highlights are protected
#: by dialling exposure back instead (see ``_enforce_guards``).
EFFECTIVE_STRENGTH_FLOOR = 0.25
#: Hard guard rails applied to the *rendered* result (stricter than the ceiling
#: used by the legacy verification loop, because Lightroom renders hotter than
#: this Pillow preview).
ADAPTIVE_P95_CEILING = 238.0
#: A frame that already carries bright highlights gets a little extra room, so a
#: photo with a lit sky is not frozen into "no edit at all" by the ceiling above.
ADAPTIVE_P95_RELATIVE_LIFT = 12.0
ADAPTIVE_P95_CEILING_MAX = 244.0
ADAPTIVE_CLIP_HIGHLIGHT_LIMIT = 0.012
SHADOW_CRUSH_MARGIN = 6.0
MAX_P50_SHIFT = 40.0
MAX_CHROMA_SHIFT = 20.0
#: A resolved edit must actually show somewhere, otherwise the user sees the
#: original photo and assumes the feature is broken.
MIN_VISIBLE_P50_DELTA = 4.0
MIN_VISIBLE_CHROMA_DELTA = 3.0
MIN_VISIBLE_RANGE_DELTA = 12.0
VISIBILITY_ROUNDS = 2
VISIBILITY_EXPOSURE_STEP = 0.15
VISIBILITY_CONTRAST_STEP = 6

#: Exposure headroom depends on the frame's key: a bright frame may not take
#: anything close to the "+1.5 EV" that a night shot tolerates.
EXPOSURE_LIMIT_BRIGHT = 0.9
EXPOSURE_LIMIT_DARK = 1.4

#: Last-resort dial-back when even the lowest strength still blows highlights:
#: pull exposure and highlights until the render fits inside the guards.
GUARD_ROUNDS = 4
GUARD_EXPOSURE_STEP = 0.2
GUARD_HIGHLIGHT_STEP = 10

#: Fraction of the measured tone gap applied as exposure correction. The model
#: still owns the mood; we only remove gross underexposure/overexposure.
BASELINE_EXPOSURE_DAMPING = 0.55

#: Neutral-patch coverage needed before white balance is "corrected" at all.
MIN_NEUTRAL_COVERAGE = 0.05
NEUTRAL_RATIO_LOW = 0.9
NEUTRAL_RATIO_HIGH = 1.12

_GAMMA = 2.2


@dataclass(frozen=True)
class ToneTargets:
    """Perceptual tone landmarks the resolved edit should land on."""

    p50: float
    p95: float
    p05: float


@dataclass(frozen=True)
class BaselineCorrections:
    """Corrections derived purely from measurements, in Lightroom slider units."""

    exposure_ev: float = 0.0
    shadows: int = 0
    highlights: int = 0
    contrast: int = 0
    dehaze: int = 0
    vibrance: int = 0
    temperature: int = 0

    def notes(self) -> tuple[str, ...]:
        produced: list[str] = []
        if abs(self.exposure_ev) >= 0.05:
            produced.append(f"曝光基准 {self.exposure_ev:+.2f} EV")
        if self.shadows:
            produced.append(f"暗部 +{self.shadows}" if self.shadows > 0 else f"暗部 {self.shadows}")
        if self.highlights:
            produced.append(
                f"高光 {self.highlights:+d}"
            )
        if self.contrast:
            produced.append(f"对比 {self.contrast:+d}")
        if self.dehaze:
            produced.append(f"去雾 {self.dehaze:+d}")
        if self.vibrance:
            produced.append(f"自然饱和 {self.vibrance:+d}")
        if self.temperature:
            produced.append(f"白平衡 {self.temperature:+d}")
        return tuple(produced)


@dataclass(frozen=True)
class ResolutionReport:
    """What the resolver did, for logging, tooling and evaluation runs."""

    version: str
    intensity: float
    baseline_factor: float
    targets: ToneTargets
    baseline: BaselineCorrections
    rounds: int
    verified: bool
    rendered: ImageMeasurements | None = None
    warnings: tuple[str, ...] = ()
    strength: float = 1.0
    visible: bool = True
    guards: tuple[str, ...] = ()

    @property
    def corrections(self) -> tuple[str, ...]:
        return self.baseline.notes()


@dataclass(frozen=True)
class Resolution:
    parameters: EditingParametersV1
    report: ResolutionReport | None = None


def soft_knee(intensity: float) -> float:
    """Map the 0..1 slider onto 0..1 strength with a concave response."""

    if intensity <= 0.0:
        return 0.0
    if intensity >= 1.0:
        return 1.0
    return math.tanh(SOFT_KNEE_CURVE * intensity) / math.tanh(SOFT_KNEE_CURVE)


def tone_targets(measurements: ImageMeasurements) -> ToneTargets:
    """Pick tone landmarks that respect the frame's key (low-key stays moody)."""

    p50 = measurements.luma.p50
    if p50 < 60.0:
        target_p50 = min(96.0, max(p50 * 1.6, p50 + 18.0))
    elif p50 > 165.0:
        target_p50 = max(150.0, p50 * 0.94)
    else:
        # A normally exposed frame needs no baseline exposure at all: the model's
        # own exposure already sets the mood, and lifting every mid-key photo
        # towards a fixed target read as "the preview over-brightens everything".
        target_p50 = p50

    # Keep a little highlight headroom unless the frame is plainly crushed.
    target_p95 = 232.0 if measurements.bright_highlight_share < 0.02 else 240.0
    target_p05 = min(18.0, max(6.0, measurements.luma.p05))
    return ToneTargets(p50=target_p50, p95=target_p95, p05=target_p05)


def baseline_corrections(
    measurements: ImageMeasurements,
    targets: ToneTargets,
) -> BaselineCorrections:
    """Derive deterministic corrections from measured statistics."""

    exposure_ev = _clamp(
        _ev_between(measurements.luma.p50, targets.p50) * BASELINE_EXPOSURE_DAMPING,
        -0.9,
        1.1,
    )
    # Never lift a frame whose highlights are already at the target: a shot with
    # a hot sky or a lit window has no headroom no matter how dark its midtones.
    headroom = _ev_between(measurements.luma.p95, targets.p95)
    if headroom > 0.0:
        exposure_ev = min(exposure_ev, headroom)

    shadows = 0
    clipped_shadows = measurements.clipped_shadows_share
    if clipped_shadows > 0.01:
        shadows += int(round(min(28.0, clipped_shadows * 400.0)))
    if measurements.deep_shadow_share > 0.25 and measurements.luma.p05 < 10.0:
        shadows += 12.0 if shadows == 0 else 6.0
    shadows = int(_clamp(round(shadows), 0.0, 40.0))

    highlights = 0
    if measurements.bright_highlight_share > 0.03:
        highlights -= int(round(min(30.0, measurements.bright_highlight_share * 300.0)))
    if measurements.clipped_highlights_share > 0.005:
        highlights -= 20
    highlights = int(_clamp(highlights, -45.0, 0.0))

    span = measurements.luma.range_90
    contrast = 0
    if span < 90.0:
        contrast += int(round((90.0 - span) / 90.0 * 22.0))
    elif span > 210.0 and measurements.clipped_shadows_share > 0.004:
        contrast -= 8
    contrast = int(_clamp(contrast, -12.0, 25.0))

    dehaze = 0
    if measurements.flat_share > 0.3:
        dehaze = int(round(min(18.0, (measurements.flat_share - 0.3) * 40.0)))

    vibrance = 0
    if measurements.saturation_mean > 160.0:
        vibrance = -12
    elif measurements.saturation_mean < 70.0:
        vibrance = 10

    temperature = 0
    if measurements.neutral_coverage >= MIN_NEUTRAL_COVERAGE:
        bias = measurements.warm_bias
        if bias > NEUTRAL_RATIO_HIGH:
            temperature = -int(round(min(20.0, (bias - NEUTRAL_RATIO_HIGH) * 120.0)))
        elif 0.0 < bias < NEUTRAL_RATIO_LOW:
            temperature = int(round(min(20.0, (NEUTRAL_RATIO_LOW - bias) * 120.0)))

    return BaselineCorrections(
        exposure_ev=round(exposure_ev, 2),
        shadows=shadows,
        highlights=highlights,
        contrast=contrast,
        dehaze=dehaze,
        vibrance=vibrance,
        temperature=temperature,
    )


def exposure_limit(measurements: ImageMeasurements) -> float:
    """How much positive exposure this frame can take before highlights blow.

    A night frame tolerates almost a stop and a half; a bright sunny frame loses
    its highlights long before that, so the cap follows the measured key.
    """

    headroom = _clamp((90.0 - measurements.luma.p50) / 90.0, 0.0, 1.0)
    limit = EXPOSURE_LIMIT_BRIGHT + (
        EXPOSURE_LIMIT_DARK - EXPOSURE_LIMIT_BRIGHT
    ) * headroom
    if measurements.bright_highlight_share > 0.03:
        limit = min(limit, EXPOSURE_LIMIT_BRIGHT)
    return round(limit, 2)


def strength_ladder() -> tuple[float, ...]:
    """Descending strengths tried by the adaptive fit (1.0, 0.9, ... 0.3)."""

    steps = int(round((1.0 - MIN_STRENGTH) / STRENGTH_STEP))
    return tuple(round(1.0 - STRENGTH_STEP * index, 2) for index in range(steps + 1))


def guard_failures(
    original: ImageMeasurements,
    rendered: ImageMeasurements,
) -> tuple[str, ...]:
    """Reasons the rendered result counts as over-edited (empty means fine)."""

    failures: list[str] = []
    # A frame with highlights already at the ceiling must not be judged against
    # the same absolute p95 as a night shot; only punish *new* blowout.
    p95_ceiling = min(
        ADAPTIVE_P95_CEILING_MAX,
        max(ADAPTIVE_P95_CEILING, original.luma.p95 + ADAPTIVE_P95_RELATIVE_LIFT),
    )
    if rendered.luma.p95 > p95_ceiling:
        failures.append(f"高光过亮（p95 {rendered.luma.p95:.0f}）")
    if rendered.luma.p99 >= 253.0 and rendered.luma.p99 > original.luma.p99 + 1.0:
        failures.append(f"高光接近纯白（p99 {rendered.luma.p99:.0f}）")
    clip_limit = max(
        ADAPTIVE_CLIP_HIGHLIGHT_LIMIT,
        original.clipped_highlights_share + 0.004,
    )
    if rendered.clipped_highlights_share > clip_limit:
        failures.append(f"高光剪切 {rendered.clipped_highlights_share * 100:.1f}%")
    if rendered.luma.p05 < max(0.0, original.luma.p05 - SHADOW_CRUSH_MARGIN):
        failures.append(f"暗部被压死（p05 {rendered.luma.p05:.0f}）")
    if rendered.clipped_shadows_share > original.clipped_shadows_share + 0.02:
        failures.append("纯黑面积扩大")
    if abs(rendered.luma.p50 - original.luma.p50) > MAX_P50_SHIFT:
        failures.append(f"整体亮度偏移 {rendered.luma.p50 - original.luma.p50:+.0f}")
    if abs(rendered.chroma_mean - original.chroma_mean) > MAX_CHROMA_SHIFT:
        failures.append(
            f"色彩偏移 {rendered.chroma_mean - original.chroma_mean:+.0f}"
        )
    return tuple(failures)


def visibility_score(
    original: ImageMeasurements,
    rendered: ImageMeasurements,
) -> float:
    """How far the render moved, as a fraction of "clearly different".

    Each axis is normalised by the least change a viewer notices, so several
    small shifts add up: a softly lifted, slightly warmer, slightly punchier
    frame can be plainly different without any single number jumping.
    """

    return (
        abs(rendered.luma.p50 - original.luma.p50) / MIN_VISIBLE_P50_DELTA
        + abs(rendered.chroma_mean - original.chroma_mean) / MIN_VISIBLE_CHROMA_DELTA
        + abs(rendered.luma.range_90 - original.luma.range_90) / MIN_VISIBLE_RANGE_DELTA
    )


def is_visible(
    original: ImageMeasurements,
    rendered: ImageMeasurements,
) -> bool:
    """True when the edit actually changes something a viewer can notice."""

    return visibility_score(original, rendered) >= 1.0


def resolve_parameters(
    *,
    measurements: ImageMeasurements,
    style_parameters: EditingParametersV1,
    intensity: float,
    observer: Callable[[EditingParametersV1], ImageMeasurements] | None = None,
    max_rounds: int = MAX_VERIFICATION_ROUNDS,
) -> Resolution:
    """Combine the model's look with measured corrections and verify it.

    ``observer`` renders candidate parameters and returns measurements of the
    rendered pixels. When it is omitted (or fails) the resolver still applies
    baseline corrections and the soft knee, but skips the verification loop and
    keeps the requested strength.
    """

    targets = tone_targets(measurements)

    if intensity <= 0.0:
        neutral = EditingParametersV1().scaled(0.0)
        return Resolution(
            parameters=neutral,
            report=ResolutionReport(
                version=EDIT_RESOLVER_VERSION,
                intensity=intensity,
                baseline_factor=0.0,
                targets=targets,
                baseline=BaselineCorrections(),
                rounds=0,
                verified=False,
                strength=0.0,
            ),
        )

    factor = soft_knee(intensity)
    baseline = baseline_corrections(measurements, targets)
    limit = exposure_limit(measurements)
    warnings: list[str] = []

    strength = 1.0
    boosted = False
    parameters = _combine(style_parameters, baseline, intensity, factor, limit)
    rendered: ImageMeasurements | None = None
    guards: tuple[str, ...] = ()

    if observer is not None:
        strength, parameters, rendered, guards = _fit_strength(
            style_parameters=style_parameters,
            baseline=baseline,
            intensity=intensity,
            factor=factor,
            limit=limit,
            measurements=measurements,
            observer=observer,
            warnings=warnings,
        )
        if rendered is not None:
            parameters, rendered = _enforce_guards(
                parameters=parameters,
                rendered=rendered,
                limit=limit,
                measurements=measurements,
                observer=observer,
                warnings=warnings,
            )
            guards = guard_failures(measurements, rendered)
            if not guards:
                parameters, rendered, boosted = _ensure_visible(
                    parameters=parameters,
                    rendered=rendered,
                    limit=limit,
                    factor=factor,
                    measurements=measurements,
                    observer=observer,
                    warnings=warnings,
                )
                if boosted:
                    guards = guard_failures(measurements, rendered)

    rounds = 0
    verified = False
    settled = boosted and is_visible(measurements, rendered)

    for round_index in range(1, max_rounds + 1):
        if settled:
            # The visibility pass already produced a render that differs from
            # the original without breaking a guard. Running the exposure
            # verification on top of it only walks the edit back to something
            # invisible, so stop here.
            verified = True
            break
        if observer is None or rendered is None:
            break

        rounds = round_index
        p50_gap = targets.p50 - rendered.luma.p50
        if (
            abs(p50_gap) <= P50_TOLERANCE
            and rendered.luma.p95 <= P95_CEILING
            and rendered.bright_highlight_share <= MAX_BRIGHT_HIGHLIGHT_SHARE
        ):
            verified = True
            break

        step = _clamp(
            _ev_between(max(rendered.luma.p50, 1.0), max(targets.p50, 1.0)) * 0.65,
            -EV_STEP_LIMIT,
            EV_STEP_LIMIT,
        ) * factor
        updates: dict[str, object] = {
            "exposure": round(
                _clamp(parameters.exposure + step, -EXPOSURE_LIMIT, limit),
                2,
            )
        }
        if rendered.bright_highlight_share > 0.05 or rendered.luma.p99 >= 253:
            updates["highlights"] = int(
                _clamp(parameters.highlights - 8, -100.0, 0.0)
            )
        candidate = parameters.model_copy(update=updates)
        try:
            candidate_rendered = observer(candidate)
        except Exception as exc:  # pragma: no cover - defensive, renderer owned
            warnings.append(f"回渲染校验失败：{type(exc).__name__}")
            break
        blocked = guard_failures(measurements, candidate_rendered)
        if blocked:
            warnings.append("为避免过曝已停止继续提亮：" + "、".join(blocked[:2]))
            break
        parameters, rendered = candidate, candidate_rendered

    visible = rendered is not None and is_visible(measurements, rendered)

    return Resolution(
        parameters=parameters,
        report=ResolutionReport(
            version=EDIT_RESOLVER_VERSION,
            intensity=intensity,
            baseline_factor=factor,
            targets=targets,
            baseline=baseline,
            rounds=rounds,
            verified=verified,
            rendered=rendered,
            warnings=tuple(warnings),
            strength=strength,
            visible=visible,
            guards=guards,
        ),
    )


def _fit_strength(
    *,
    style_parameters: EditingParametersV1,
    baseline: BaselineCorrections,
    intensity: float,
    factor: float,
    limit: float,
    measurements: ImageMeasurements,
    observer: Callable[[EditingParametersV1], ImageMeasurements],
    warnings: list[str],
) -> tuple[float, EditingParametersV1, ImageMeasurements | None, tuple[str, ...]]:
    """Pick the strongest style scale that still passes the over-edit guards."""

    last_parameters = _combine(style_parameters, baseline, intensity, factor, limit)
    last_rendered: ImageMeasurements | None = None
    last_strength = 1.0
    failures: tuple[str, ...] = ()
    for strength in strength_ladder():
        if intensity * strength < EFFECTIVE_STRENGTH_FLOOR:
            break
        last_strength = strength
        parameters = _combine(
            style_parameters, baseline, intensity * strength, factor, limit
        )
        try:
            rendered = observer(parameters)
        except Exception as exc:  # pragma: no cover - defensive, renderer owned
            warnings.append(f"回渲染校验失败：{type(exc).__name__}")
            return strength, parameters, None, ()

        last_parameters = parameters
        last_rendered = rendered
        failures = guard_failures(measurements, rendered)
        if not failures:
            if strength < 1.0:
                warnings.append(f"自适应强度 {strength * 100:.0f}%（原图偏亮或已够亮）")
            return strength, parameters, rendered, ()

    # Even the lowest rung is over-edited: keep it and say so, rather than
    # silently shipping a blown-out render.
    warnings.append(
        "已把强度降到最低仍偏强：" + "、".join(failures[:2])
    )
    return last_strength, last_parameters, last_rendered, failures


def _enforce_guards(
    *,
    parameters: EditingParametersV1,
    rendered: ImageMeasurements,
    limit: float,
    measurements: ImageMeasurements,
    observer: Callable[[EditingParametersV1], ImageMeasurements],
    warnings: list[str],
) -> tuple[EditingParametersV1, ImageMeasurements]:
    """Dial exposure and highlights back until the render stops blowing out."""

    current_parameters = parameters
    current_rendered = rendered
    for _ in range(GUARD_ROUNDS):
        failures = guard_failures(measurements, current_rendered)
        if not failures:
            return current_parameters, current_rendered
        updates: dict[str, object] = {
            "exposure": round(
                _clamp(
                    current_parameters.exposure - GUARD_EXPOSURE_STEP,
                    -EXPOSURE_LIMIT,
                    limit,
                ),
                2,
            ),
            "highlights": int(
                _clamp(
                    current_parameters.highlights - GUARD_HIGHLIGHT_STEP,
                    -100.0,
                    0.0,
                )
            ),
        }
        if current_parameters.whites > 0:
            updates["whites"] = int(_clamp(current_parameters.whites - 6, -100.0, 0.0))
        candidate = current_parameters.model_copy(update=updates)
        if candidate.model_dump() == current_parameters.model_dump():
            break
        try:
            candidate_rendered = observer(candidate)
        except Exception as exc:  # pragma: no cover - defensive, renderer owned
            warnings.append(f"回渲染校验失败：{type(exc).__name__}")
            break
        current_parameters, current_rendered = candidate, candidate_rendered

    if guard_failures(measurements, current_rendered):
        warnings.append(
            "照片本身高光已接近纯白，已下调曝光与高光以压住过曝"
        )
    return current_parameters, current_rendered


#: Nudges tried, in order, when an edit is too subtle. Brightness first, because
#: it reads as "the photo changed"; then tone shaping that does not need highlight
#: headroom (a frame with a blown sky can still gain depth from shadows/colour).
_VISIBILITY_NUDGES: tuple[tuple[str, float], ...] = (
    ("exposure", VISIBILITY_EXPOSURE_STEP),
    ("shadows", 12.0),
    ("blacks", -8.0),
    ("contrast", VISIBILITY_CONTRAST_STEP),
    ("vibrance", 10.0),
    ("saturation", 5.0),
    ("dehaze", 8.0),
    ("clarity", 15.0),
    ("texture", 15.0),
)

_BOUNDS: dict[str, tuple[float, float]] = {
    "shadows": (-20.0, 70.0),
    "highlights": (-80.0, 20.0),
    "contrast": (-40.0, 40.0),
    "dehaze": (-20.0, 40.0),
    "vibrance": (-40.0, 40.0),
    "saturation": (-40.0, 40.0),
    "blacks": (-40.0, 40.0),
    "whites": (-40.0, 40.0),
    "clarity": (-40.0, 40.0),
    "texture": (-40.0, 40.0),
    "temperature": (-60.0, 60.0),
}


def _nudge(
    parameters: EditingParametersV1,
    key: str,
    delta: float,
    limit: float,
) -> EditingParametersV1:
    if key == "exposure":
        value = _clamp(parameters.exposure + delta, -EXPOSURE_LIMIT, limit)
        return parameters.model_copy(update={"exposure": round(value, 2)})
    low, high = _BOUNDS[key]
    return parameters.model_copy(
        update={key: int(_clamp(getattr(parameters, key) + delta, low, high))}
    )


def _ensure_visible(
    *,
    parameters: EditingParametersV1,
    rendered: ImageMeasurements,
    limit: float,
    factor: float,
    measurements: ImageMeasurements,
    observer: Callable[[EditingParametersV1], ImageMeasurements],
    warnings: list[str],
) -> tuple[EditingParametersV1, ImageMeasurements, bool]:
    """Nudge a too-subtle edit until the difference is visible (or guards bind)."""

    if is_visible(measurements, rendered):
        return parameters, rendered, False

    boosted = False
    best_parameters = parameters
    best_rendered = rendered
    best_score = visibility_score(measurements, rendered)
    for key, delta in _VISIBILITY_NUDGES:
        # Direction follows the photo: a bright frame has no headroom to add
        # brightness, so try pulling exposure down instead of up.
        step = delta * factor
        signs = (1.0, -1.0) if key == "exposure" else (1.0,)
        accepted = False
        for sign in signs:
            candidate = _nudge(parameters, key, step * sign, limit)
            if candidate.model_dump() == parameters.model_dump():
                continue
            try:
                candidate_rendered = observer(candidate)
            except Exception as exc:  # pragma: no cover - defensive, renderer owned
                warnings.append(f"回渲染校验失败：{type(exc).__name__}")
                return best_parameters, best_rendered, boosted
            if guard_failures(measurements, candidate_rendered):
                # No headroom left for this nudge. Buy some back by pulling the
                # highlights down, then retry the nudge once on top of that: a
                # frame that is already bright should still get a visible edit,
                # just through tone compression instead of more brightness.
                relieved = _nudge(parameters, "highlights", -GUARD_HIGHLIGHT_STEP, limit)
                candidate = _nudge(relieved, key, step * sign, limit)
                if candidate.model_dump() in (
                    parameters.model_dump(),
                    relieved.model_dump(),
                ):
                    continue
                try:
                    candidate_rendered = observer(candidate)
                except Exception as exc:  # pragma: no cover - renderer owned
                    warnings.append(f"回渲染校验失败：{type(exc).__name__}")
                    return best_parameters, best_rendered, boosted
                if guard_failures(measurements, candidate_rendered):
                    continue
            score = visibility_score(measurements, candidate_rendered)
            if score > best_score:
                best_parameters, best_rendered, best_score = (
                    candidate,
                    candidate_rendered,
                    score,
                )
            parameters, rendered = candidate, candidate_rendered
            boosted = True
            accepted = True
            if is_visible(measurements, rendered):
                return parameters, rendered, boosted
            break
        if accepted:
            continue

    if not is_visible(measurements, best_rendered):
        warnings.append("这张照片本身与风格差异很小，可手动加大强度")
        return best_parameters, best_rendered, boosted
    return best_parameters, best_rendered, boosted


def make_render_observer(
    source_path: Path,
    *,
    max_size: tuple[int, int] = MEASUREMENT_MAX_SIZE,
) -> Callable[[EditingParametersV1], ImageMeasurements] | None:
    """Build a verification observer that renders ``source_path`` in memory.

    The source is decoded and downsampled once and reused across rounds. Returns
    ``None`` when the preview renderer is unavailable so callers can degrade
    gracefully instead of failing a photo edit.
    """

    try:
        from app.services.preview import render_image
    except Exception:  # pragma: no cover - renderer always present in app builds
        return None

    cache: list[Image.Image] = []

    def observer(parameters: EditingParametersV1) -> ImageMeasurements:
        if not cache:
            with Image.open(source_path) as source:
                base = flatten_to_rgb(ImageOps.exif_transpose(source))
                base.thumbnail(max_size)
                cache.append(base)
        return measure_image(render_image(cache[0].copy(), parameters))

    return observer


def _combine(
    style_parameters: EditingParametersV1,
    baseline: BaselineCorrections,
    intensity: float,
    factor: float,
    limit: float = EXPOSURE_LIMIT,
) -> EditingParametersV1:
    """Scale the model's look linearly and add the measured corrections.

    The style keeps the plain ``scaled()`` semantics so equal effective output
    always maps onto equal parameters; the measured corrections ride the
    soft-knee factor so a half-way slider still removes most of a gross defect.
    """

    base = style_parameters.scaled(intensity)

    exposure = _clamp(
        base.exposure + baseline.exposure_ev * factor, -EXPOSURE_LIMIT, limit
    )
    updates: dict[str, object] = {
        "exposure": round(exposure, 2),
        "shadows": _add(base.shadows, baseline.shadows, factor, -20, 70),
        "highlights": _add(base.highlights, baseline.highlights, factor, -80, 20),
        "contrast": _add(base.contrast, baseline.contrast, factor, -40, 40),
        "dehaze": _add(base.dehaze, baseline.dehaze, factor, -20, 40),
        "vibrance": _add(base.vibrance, baseline.vibrance, factor, -40, 40),
        "temperature": _add(base.temperature, baseline.temperature, factor, -60, 60),
    }
    return base.model_copy(update=updates)


def _add(
    scaled_value: int,
    correction: int,
    factor: float,
    low: float = -100.0,
    high: float = 100.0,
) -> int:
    if correction == 0:
        return int(_clamp(scaled_value, low, high))
    return int(_clamp(scaled_value + correction * factor, low, high))



def _ev_between(current: float, target: float) -> float:
    safe_current = max(current, 1.0) / 255.0
    safe_target = max(target, 1.0) / 255.0
    return math.log2((safe_target / safe_current) ** _GAMMA)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
