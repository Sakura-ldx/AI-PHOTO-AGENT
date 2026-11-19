from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.schemas import EditingParametersV1, HSL_CHANNELS, MAPPING_VERSION, is_xml_1_0_text
from app.services.parameter_specs import (
    CURVE_SPECS,
    PARAMETER_SPECS,
    encode_for_lightroom,
    is_neutral,
)


XMP_META_NS = "adobe:ns:meta/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
CAMERA_RAW_NS = "http://ns.adobe.com/camera-raw-settings/1.0/"
AI_PHOTO_AGENT_NS = "urn:ai-photo-agent:metadata:1.0/"
PRESET_UUID_NAMESPACE = uuid5(NAMESPACE_URL, "urn:ai-photo-agent:lightroom-preset")
XML_NAME_LIMIT = 200

ET.register_namespace("x", XMP_META_NS)
ET.register_namespace("rdf", RDF_NS)
ET.register_namespace("crs", CAMERA_RAW_NS)
ET.register_namespace("aipa", AI_PHOTO_AGENT_NS)


def write_xmp_preset(
    output_path: Path,
    preset_name: str,
    parameters: EditingParametersV1,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    xmp = build_xmp_preset(preset_name, parameters)
    temp_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as output:
            output.write(xmp)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return output_path


def build_xmp_preset(preset_name: str, p: EditingParametersV1) -> str:
    safe_name = _validated_xml_name(preset_name)
    root = ET.Element(
        _qname(XMP_META_NS, "xmpmeta"),
        {_qname(XMP_META_NS, "xmptk"): f"AI Photo Agent {MAPPING_VERSION}"},
    )
    rdf = ET.SubElement(root, _qname(RDF_NS, "RDF"))
    description = ET.SubElement(
        rdf,
        _qname(RDF_NS, "Description"),
        _description_attributes(safe_name, p),
    )
    name = ET.SubElement(description, _qname(CAMERA_RAW_NS, "Name"))
    alternatives = ET.SubElement(name, _qname(RDF_NS, "Alt"))
    name_item = ET.SubElement(
        alternatives,
        _qname(RDF_NS, "li"),
        {"{http://www.w3.org/XML/1998/namespace}lang": "x-default"},
    )
    name_item.text = safe_name
    if p.has_adjustments():
        curve = ET.SubElement(description, _qname(CAMERA_RAW_NS, "ToneCurvePV2012"))
        sequence = ET.SubElement(curve, _qname(RDF_NS, "Seq"))
        for point in p.tone_curve:
            item = ET.SubElement(sequence, _qname(RDF_NS, "li"))
            item.text = f"{point.x}, {point.y}"
        for spec in CURVE_SPECS:
            points = getattr(p.advanced, spec.key)
            if not points:
                continue
            channel_curve = ET.SubElement(description, _qname(CAMERA_RAW_NS, spec.lr_key))
            channel_sequence = ET.SubElement(channel_curve, _qname(RDF_NS, "Seq"))
            for point in points:
                item = ET.SubElement(channel_sequence, _qname(RDF_NS, "li"))
                item.text = f"{point.x}, {point.y}"

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        f"{body}\n"
        '<?xpacket end="w"?>\n'
    )


def _description_attributes(preset_name: str, p: EditingParametersV1) -> dict[str, str]:
    has_settings = p.has_adjustments()
    attributes = {
        _qname(RDF_NS, "about"): "",
        _qname(CAMERA_RAW_NS, "PresetType"): "Normal",
        _qname(CAMERA_RAW_NS, "Cluster"): "AI Photo Agent",
        _qname(CAMERA_RAW_NS, "UUID"): _preset_uuid(preset_name, p),
        _qname(CAMERA_RAW_NS, "HasSettings"): "True" if has_settings else "False",
        _qname(CAMERA_RAW_NS, "ProcessVersion"): "15.4",
        _qname(CAMERA_RAW_NS, "Version"): "17.0",
        _qname(AI_PHOTO_AGENT_NS, "MappingVersion"): MAPPING_VERSION,
    }
    if not has_settings:
        return attributes

    attributes.update(
        {
            _qname(CAMERA_RAW_NS, "Exposure2012"): f"{p.exposure:.2f}",
            _qname(CAMERA_RAW_NS, "Contrast2012"): str(round(p.contrast)),
            _qname(CAMERA_RAW_NS, "Highlights2012"): str(round(p.highlights)),
            _qname(CAMERA_RAW_NS, "Shadows2012"): str(round(p.shadows)),
            _qname(CAMERA_RAW_NS, "Whites2012"): str(round(p.whites)),
            _qname(CAMERA_RAW_NS, "Blacks2012"): str(round(p.blacks)),
            _qname(CAMERA_RAW_NS, "IncrementalTemperature"): str(round(p.temperature)),
            _qname(CAMERA_RAW_NS, "IncrementalTint"): str(round(p.tint)),
            _qname(CAMERA_RAW_NS, "Vibrance"): str(round(p.vibrance)),
            _qname(CAMERA_RAW_NS, "Saturation"): str(round(p.saturation)),
            _qname(CAMERA_RAW_NS, "Texture"): str(round(p.texture)),
            _qname(CAMERA_RAW_NS, "Clarity2012"): str(round(p.clarity)),
            _qname(CAMERA_RAW_NS, "Dehaze"): str(round(p.dehaze)),
            _qname(CAMERA_RAW_NS, "ParametricShadowSplit"): "25",
            _qname(CAMERA_RAW_NS, "ParametricMidtoneSplit"): "50",
            _qname(CAMERA_RAW_NS, "ParametricHighlightSplit"): "75",
            _qname(CAMERA_RAW_NS, "ToneCurveName2012"): "Custom",
            _qname(CAMERA_RAW_NS, "SplitToningShadowHue"): str(
                p.color_grading.shadows.hue
            ),
            _qname(CAMERA_RAW_NS, "SplitToningShadowSaturation"): str(
                p.color_grading.shadows.saturation
            ),
            _qname(CAMERA_RAW_NS, "ColorGradeShadowLum"): str(
                p.color_grading.shadows.luminance
            ),
            _qname(CAMERA_RAW_NS, "ColorGradeMidtoneHue"): str(
                p.color_grading.midtones.hue
            ),
            _qname(CAMERA_RAW_NS, "ColorGradeMidtoneSat"): str(
                p.color_grading.midtones.saturation
            ),
            _qname(CAMERA_RAW_NS, "ColorGradeMidtoneLum"): str(
                p.color_grading.midtones.luminance
            ),
            _qname(CAMERA_RAW_NS, "SplitToningHighlightHue"): str(
                p.color_grading.highlights.hue
            ),
            _qname(CAMERA_RAW_NS, "SplitToningHighlightSaturation"): str(
                p.color_grading.highlights.saturation
            ),
            _qname(CAMERA_RAW_NS, "ColorGradeHighlightLum"): str(
                p.color_grading.highlights.luminance
            ),
            _qname(CAMERA_RAW_NS, "ColorGradeBlending"): str(p.color_grading.blending),
            _qname(CAMERA_RAW_NS, "SplitToningBalance"): str(p.color_grading.balance),
            _qname(CAMERA_RAW_NS, "Sharpness"): str(p.sharpening.amount),
            _qname(CAMERA_RAW_NS, "SharpenRadius"): f"{p.sharpening.radius:.2f}",
            _qname(CAMERA_RAW_NS, "SharpenDetail"): str(p.sharpening.detail),
            _qname(CAMERA_RAW_NS, "SharpenEdgeMasking"): str(p.sharpening.masking),
            _qname(CAMERA_RAW_NS, "LuminanceSmoothing"): str(
                p.noise_reduction.luminance
            ),
            _qname(CAMERA_RAW_NS, "ColorNoiseReduction"): str(
                p.noise_reduction.color
            ),
        }
    )
    attributes.update(_hsl_attributes(p))
    attributes.update(_advanced_attributes(p))
    return attributes


def _advanced_attributes(p: EditingParametersV1) -> dict[str, str]:
    """One ``crs:`` attribute per advanced parameter (the XMP twin of develop settings)."""

    attributes: dict[str, str] = {}
    for spec in PARAMETER_SPECS:
        if spec.kind == "curve":
            continue
        current = getattr(p.advanced, spec.key)
        if is_neutral(spec, current):
            # A preset should change only what the style asks for; writing defaults
            # would reset settings the user already had.
            continue
        attributes[_qname(CAMERA_RAW_NS, spec.xmp_attr)] = str(
            encode_for_lightroom(spec, current)
        )
    return attributes


def _hsl_attributes(p: EditingParametersV1) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for channel in HSL_CHANNELS:
        adjustment = p.hsl.get(channel)
        hue = adjustment.hue if adjustment is not None else 0
        saturation = adjustment.saturation if adjustment is not None else 0
        luminance = adjustment.luminance if adjustment is not None else 0
        title = channel.capitalize()
        attributes[_qname(CAMERA_RAW_NS, f"HueAdjustment{title}")] = str(hue)
        attributes[_qname(CAMERA_RAW_NS, f"SaturationAdjustment{title}")] = str(saturation)
        attributes[_qname(CAMERA_RAW_NS, f"LuminanceAdjustment{title}")] = str(luminance)
    return attributes


def _preset_uuid(preset_name: str, parameters: EditingParametersV1) -> str:
    canonical_payload = json.dumps(
        {
            "mapping_version": MAPPING_VERSION,
            "parameters": parameters.model_dump(mode="json"),
            "preset_name": preset_name,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(PRESET_UUID_NAMESPACE, canonical_payload).hex.upper()


def _validated_xml_name(value: str) -> str:
    if not value.strip():
        raise ValueError("preset name must not be blank")
    if len(value) > XML_NAME_LIMIT:
        raise ValueError(f"preset name must not exceed {XML_NAME_LIMIT} characters")
    if not is_xml_1_0_text(value):
        raise ValueError("preset name contains characters that XML 1.0 cannot represent")
    return value


def _qname(namespace: str, local_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", local_name):
        raise ValueError(f"invalid XML local name: {local_name}")
    return f"{{{namespace}}}{local_name}"
