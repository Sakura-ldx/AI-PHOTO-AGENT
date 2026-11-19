from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.adapters.deepseek_analysis import DeepSeekAnalysisProvider, _user_prompt
from app.adapters.vision_models import (
    PHOTO_PROMPT_VERSION,
    PHOTO_SYSTEM_PROMPT,
    photo_measurements_block,
)
from app.schemas import ImageMetadata


def make_metadata() -> ImageMetadata:
    return ImageMetadata(
        width=320,
        height=200,
        format="PNG",
        color_mode="RGB",
        file_size=100,
        content_type="image/png",
    )


def make_image(path: Path) -> None:
    Image.new("RGB", (320, 200), color=(35, 70, 110)).save(path, format="PNG")


def test_prompt_version_matches_the_calibrated_prompt() -> None:
    assert PHOTO_PROMPT_VERSION == "photo-analysis-v5"
    # The calibration table is what stops the model from returning 0.1 EV style
    # token adjustments that nobody can see.
    assert "exposure 以 EV 计" in PHOTO_SYSTEM_PROMPT
    assert "+1.0 是整体明显提亮一档" in PHOTO_SYSTEM_PROMPT
    assert "不要默认使用“中性/偏暖/偏冷”三分法" in PHOTO_SYSTEM_PROMPT
    assert "至少一套" in PHOTO_SYSTEM_PROMPT
    assert "两个非白平衡维度" in PHOTO_SYSTEM_PROMPT
    assert "量化测量" in PHOTO_SYSTEM_PROMPT
    # v5 keeps the advanced Lightroom values in the compact override list.
    assert "parameters.overrides" in PHOTO_SYSTEM_PROMPT
    assert "grain_amount" in PHOTO_SYSTEM_PROMPT
    assert "black_and_white" in PHOTO_SYSTEM_PROMPT


def test_measurement_block_quotes_program_computed_statistics(tmp_path: Path) -> None:
    path = tmp_path / "photo.png"
    make_image(path)

    block = photo_measurements_block(path)

    assert "量化测量" in block
    assert "中位" in block
    assert "18% 灰" in block


def test_measurement_block_is_empty_when_measurement_fails(tmp_path: Path) -> None:
    assert photo_measurements_block(tmp_path / "missing.png") == ""


def test_user_prompt_embeds_the_measurement_block() -> None:
    block = "图像量化测量（由程序计算，可信；0-255 尺度）：\n- 亮度中位：49"

    prompt = _user_prompt(make_metadata(), measurements_block=block)

    assert f"prompt_version={PHOTO_PROMPT_VERSION}" in prompt
    assert block in prompt
    assert prompt.index(block) < prompt.index("请分析附加图片")


def test_user_prompt_omits_the_block_when_measurement_is_unavailable() -> None:
    prompt = _user_prompt(make_metadata())

    assert "量化测量（由程序计算" not in prompt
    assert "请分析附加图片" in prompt


def test_deepseek_request_body_carries_the_measurement_block() -> None:
    provider = DeepSeekAnalysisProvider(api_key="test", client=object())

    payload = provider._build_payload(
        metadata=make_metadata(),
        image_data_url="data:image/png;base64,AAAA",
        measurements_block="SENTINEL-MEASUREMENTS",
        strict=False,
    )

    user_text = payload["messages"][1]["content"][0]["text"]
    assert "SENTINEL-MEASUREMENTS" in user_text
    assert payload["messages"][0]["content"] == PHOTO_SYSTEM_PROMPT
