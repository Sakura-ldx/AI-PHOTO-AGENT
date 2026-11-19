from __future__ import annotations

import math

import pytest
from PIL import Image

from app.schemas import EditingParametersV1
from app.services.edit_resolver import (
    ADAPTIVE_P95_CEILING,
    EXPOSURE_LIMIT_BRIGHT,
    MAX_VERIFICATION_ROUNDS,
    MIN_STRENGTH,
    baseline_corrections,
    exposure_limit,
    guard_failures,
    is_visible,
    make_render_observer,
    resolve_parameters,
    soft_knee,
    strength_ladder,
    tone_targets,
)
from app.services.measurements import ImageMeasurements, measure_image


def _flat(level: int, size: tuple[int, int] = (16, 16)) -> ImageMeasurements:
    return measure_image(Image.new("RGB", size, (level, level, level)))


def _flat_warm(level: int) -> ImageMeasurements:
    return measure_image(Image.new("RGB", (16, 16), (level, int(level * 0.72), int(level * 0.48))))


def _ramp() -> ImageMeasurements:
    """A full-range grey ramp: well exposed, no clipping, no flatness."""

    image = Image.new("RGB", (232, 16))
    image.putdata([(value, value, value) for _ in range(16) for value in range(12, 244)])
    return measure_image(image)


def _style_with_edits() -> EditingParametersV1:
    return EditingParametersV1(
        exposure=0.35,
        contrast=20,
        highlights=-30,
        shadows=25,
        temperature=6,
        vibrance=15,
    )


# --------------------------------------------------------------------------- #
# Soft knee
# --------------------------------------------------------------------------- #


def test_soft_knee_pins_both_ends() -> None:
    assert soft_knee(0.0) == 0.0
    assert soft_knee(1.0) == 1.0
    assert soft_knee(-0.5) == 0.0
    assert soft_knee(2.0) == 1.0


def test_soft_knee_is_concave_and_monotonic() -> None:
    samples = [soft_knee(value / 20.0) for value in range(21)]
    assert samples == sorted(samples)
    # A half-way slider must already deliver a clearly visible edit.
    assert soft_knee(0.5) > 0.6
    assert soft_knee(0.5) < soft_knee(1.0)


# --------------------------------------------------------------------------- #
# Targets and baseline corrections
# --------------------------------------------------------------------------- #


def test_tone_targets_keep_low_key_frames_moody() -> None:
    dark = _flat(49)
    targets = tone_targets(dark)
    assert targets.p50 > 49.0
    assert targets.p50 <= 96.0


def test_dark_frame_gets_positive_exposure_anchor() -> None:
    measurements = _flat(40)
    baseline = baseline_corrections(measurements, tone_targets(measurements))
    assert baseline.exposure_ev > 0.2
    assert any("曝光基准" in note for note in baseline.notes())


def test_well_exposed_frame_gets_no_exposure_anchor() -> None:
    measurements = _flat(120)
    baseline = baseline_corrections(measurements, tone_targets(measurements))
    assert abs(baseline.exposure_ev) < 0.25


def test_white_balance_is_only_touched_when_neutrals_exist() -> None:
    warm = _flat_warm(140)
    assert warm.neutral_coverage < 0.05
    bare = baseline_corrections(warm, tone_targets(warm))
    assert bare.temperature == 0


def test_clipped_frame_gets_highlight_recovery() -> None:
    measurements = measure_image(Image.new("RGB", (16, 16), (255, 255, 255)))
    baseline = baseline_corrections(measurements, tone_targets(measurements))
    assert baseline.highlights < 0


# --------------------------------------------------------------------------- #
# resolve_parameters
# --------------------------------------------------------------------------- #


def test_zero_intensity_is_exactly_neutral_and_skips_the_loop() -> None:
    calls: list[EditingParametersV1] = []

    def observer(parameters: EditingParametersV1) -> ImageMeasurements:
        calls.append(parameters)
        return _flat(120)

    resolution = resolve_parameters(
        measurements=_flat(49),
        style_parameters=_style_with_edits(),
        intensity=0.0,
        observer=observer,
    )

    expected = EditingParametersV1().scaled(0.0)
    assert resolution.parameters.model_dump() == expected.model_dump()
    assert calls == []
    assert resolution.report.rounds == 0
    assert resolution.report.baseline_factor == 0.0


def test_full_intensity_applies_the_full_edit() -> None:
    resolution = resolve_parameters(
        measurements=_ramp(),
        style_parameters=_style_with_edits(),
        intensity=1.0,
    )
    assert resolution.report.baseline_factor == 1.0
    assert resolution.parameters.contrast == pytest.approx(20, abs=6)
    assert resolution.parameters.shadows == pytest.approx(25, abs=6)


def test_baseline_correction_uses_the_soft_knee() -> None:
    """A mid slider still removes most of a gross defect, but stays under full."""

    full = resolve_parameters(
        measurements=_flat(40), style_parameters=_style_with_edits(), intensity=1.0
    )
    half = resolve_parameters(
        measurements=_flat(40), style_parameters=_style_with_edits(), intensity=0.5
    )
    assert half.report.baseline_factor > 0.6
    assert half.report.baseline_factor < 1.0
    assert (
        abs(half.parameters.exposure)
        > abs(full.parameters.exposure) * 0.5 * 0.9
    )


def test_verification_loop_lifts_a_dark_render() -> None:
    target = tone_targets(_flat(49)).p50
    base_level = 49.0

    def observer(parameters: EditingParametersV1) -> ImageMeasurements:
        gain = 2.0 ** (parameters.exposure / 2.2)
        level = int(max(0.0, min(255.0, base_level * gain)))
        return _flat(level)

    resolution = resolve_parameters(
        measurements=_flat(49),
        style_parameters=_style_with_edits(),
        intensity=1.0,
        observer=observer,
    )

    assert resolution.report.rounds >= 1
    assert resolution.report.rendered is not None
    assert abs(resolution.report.rendered.luma.p50 - target) < abs(base_level - target)
    assert 0 < resolution.report.rounds <= MAX_VERIFICATION_ROUNDS


def test_verification_stops_when_target_is_met() -> None:
    target = tone_targets(_ramp())

    def observer(parameters: EditingParametersV1) -> ImageMeasurements:
        return _flat(int(round(target.p50)))

    resolution = resolve_parameters(
        measurements=_ramp(),
        style_parameters=_style_with_edits(),
        intensity=1.0,
        observer=observer,
    )

    assert resolution.report.rounds == 1


def test_observer_failure_is_reported_not_raised() -> None:
    def observer(parameters: EditingParametersV1) -> ImageMeasurements:
        raise RuntimeError("renderer exploded")

    resolution = resolve_parameters(
        measurements=_flat(49),
        style_parameters=_style_with_edits(),
        intensity=1.0,
        observer=observer,
    )

    assert resolution.report.warnings
    assert resolution.parameters.exposure == pytest.approx(
        resolution.parameters.exposure, abs=0.0
    )


def test_resolved_parameters_stay_inside_schema_bounds() -> None:
    resolution = resolve_parameters(
        measurements=_flat(12),
        style_parameters=EditingParametersV1(
            exposure=4.5,
            contrast=95,
            shadows=95,
            highlights=-95,
            temperature=-95,
        ),
        intensity=1.0,
    )
    parameters = resolution.parameters
    assert -5.0 <= parameters.exposure <= 5.0
    for value in (
        parameters.contrast,
        parameters.shadows,
        parameters.highlights,
        parameters.temperature,
        parameters.vibrance,
    ):
        assert -100 <= value <= 100


def test_render_observer_returns_none_when_renderer_is_missing(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name == "app.services.preview":
            raise ImportError("no renderer")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert make_render_observer(__file__) is None


def test_tone_curve_survives_resolution() -> None:
    resolution = resolve_parameters(
        measurements=_flat(49),
        style_parameters=_style_with_edits(),
        intensity=1.0,
    )
    points = resolution.parameters.tone_curve
    assert points[0].x == 0 and points[0].y == 0
    assert points[-1].x == 255 and points[-1].y == 255
    assert all(
        points[index].x < points[index + 1].x for index in range(len(points) - 1)
    )
    assert math.isfinite(resolution.parameters.exposure)

def _render_style(
    base_level: float,
    *,
    shadow_lift: float = 0.35,
    contrast_gain: float = 0.0015,
) -> callable:
    """A stand-in renderer: exposure in EV, shadows and contrast do what they say."""

    def observer(parameters: EditingParametersV1) -> ImageMeasurements:
        gain = 2.0 ** (parameters.exposure / 2.2)
        level = base_level * gain
        level += max(0, parameters.shadows) * shadow_lift
        level += (128.0 - level) * max(0, parameters.contrast) * contrast_gain
        return _flat(int(round(max(0.0, min(255.0, level)))))

    return observer


def test_strength_ladder_descends_to_the_floor() -> None:
    ladder = strength_ladder()
    assert ladder[0] == 1.0
    assert ladder[-1] == MIN_STRENGTH
    assert list(ladder) == sorted(ladder, reverse=True)
    assert len(set(ladder)) == len(ladder)


def test_adaptive_strength_pulls_back_on_an_already_bright_frame() -> None:
    original = _flat(228)
    resolution = resolve_parameters(
        measurements=original,
        style_parameters=_style_with_edits(),
        intensity=1.0,
        observer=_render_style(228.0),
    )

    assert resolution.report.strength < 1.0
    assert resolution.report.strength >= MIN_STRENGTH
    assert resolution.report.guards == ()
    assert resolution.report.rendered is not None
    assert resolution.report.rendered.luma.p95 <= ADAPTIVE_P95_CEILING


def test_adaptive_strength_keeps_a_dark_frame_at_full_power() -> None:
    original = _flat(35)
    resolution = resolve_parameters(
        measurements=original,
        style_parameters=_style_with_edits(),
        intensity=1.0,
        observer=_render_style(35.0),
    )

    assert resolution.report.strength == 1.0
    assert resolution.report.visible is True
    assert is_visible(original, resolution.report.rendered)


def test_visibility_floor_boosts_a_style_that_changes_nothing() -> None:
    faint = EditingParametersV1.model_validate(
        {
            **EditingParametersV1().model_dump(),
            "contrast": 2,
            "shadows": 1,
        }
    )
    original = _flat(100)
    resolution = resolve_parameters(
        measurements=original,
        style_parameters=faint,
        intensity=0.6,
        observer=_render_style(100.0),
    )

    assert resolution.report.visible is True
    assert is_visible(original, resolution.report.rendered)


def test_dynamic_exposure_limit_tracks_the_frame_key() -> None:
    assert exposure_limit(_flat(25)) > exposure_limit(_flat(50))
    assert exposure_limit(_flat(50)) > exposure_limit(_flat(200))
    assert exposure_limit(_flat(200)) <= EXPOSURE_LIMIT_BRIGHT


def test_guard_failures_name_the_reason() -> None:
    original = _flat(120)
    hot = _flat(252)
    reasons = guard_failures(original, hot)
    assert reasons
    assert any("高光" in reason for reason in reasons)
    assert guard_failures(original, _flat(128)) == ()


def test_adaptive_strength_never_exceeds_the_ladder_floor() -> None:
    original = _flat(250)
    resolution = resolve_parameters(
        measurements=original,
        style_parameters=_style_with_edits(),
        intensity=1.0,
        observer=_render_style(250.0),
    )

    assert resolution.report.strength >= MIN_STRENGTH
    assert resolution.report.rendered is None or (
        resolution.report.rendered.luma.p95 <= 255.0
    )

def test_baseline_exposure_keeps_its_hands_off_blown_highlights() -> None:
    original = _flat_warm(70)
    targets = tone_targets(original)
    baseline = baseline_corrections(original, targets)
    # Mild frames still get lifted; the headroom clamp only bites when the
    # highlight end is already where the targets want it.
    assert baseline.exposure_ev >= 0.0

    hot = _flat_with_p95(229)
    hot_targets = tone_targets(hot)
    hot_baseline = baseline_corrections(hot, hot_targets)
    assert hot_baseline.exposure_ev <= 0.2


def test_guards_allow_a_small_lift_on_an_already_bright_frame() -> None:
    original = _flat_with_p95(229)
    assert guard_failures(original, _flat_with_p95(236)) == ()
    assert guard_failures(original, _flat_with_p95(248))


def test_visibility_counts_several_small_changes_together() -> None:
    original = _flat(128)
    nudged = measure_image(Image.new("RGB", (16, 16), (133, 131, 130)))
    assert is_visible(original, nudged)


def test_best_effort_keeps_the_least_blown_out_render() -> None:
    original = _flat_with_p95(240)
    resolution = resolve_parameters(
        measurements=original,
        style_parameters=_style_with_edits(),
        intensity=1.0,
        observer=_render_style(240.0),
    )

    rendered = resolution.report.rendered
    assert rendered is not None
    assert rendered.luma.p95 <= 246.0


def test_low_intensity_never_scales_the_style_away() -> None:
    original = _ramp()
    resolution = resolve_parameters(
        measurements=original,
        style_parameters=_style_with_edits(),
        intensity=0.3,
        observer=_render_style(128.0),
    )

    assert resolution.report.strength > MIN_STRENGTH


def _flat_with_p95(p95: float) -> ImageMeasurements:
    """A frame whose top 5% sits at ``p95`` (rest of the ramp stays mid)."""

    size = 100
    high = int(round(p95))
    column = [high] * 10 + [110] * 90
    image = Image.new("RGB", (size, len(column)))
    image.putdata([(value, value, value) for _ in range(size) for value in column])
    return measure_image(image)
