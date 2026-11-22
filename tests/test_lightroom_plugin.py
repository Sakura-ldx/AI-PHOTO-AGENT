from __future__ import annotations

import json
import os
import re
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.services.lightroom_bridge as bridge_module
from app.adapters.analysis import MockAnalysisProvider
from app.config import AppSettings
from app.main import create_app
from app.services.lightroom_bridge import (
    BRIDGE_DIR_NAME,
    JOB_SCHEMA_VERSION,
    PLUGIN_FOLDER_NAME,
    PLUGIN_SOURCE_DIR,
    LightroomBridge,
    _safe_preset_filename,
)
from app.services.storage import FileStorage


def jpeg_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (128, 96), color=(120, 140, 160)).save(buffer, format="JPEG")
    return buffer.getvalue()


def make_lightroom_client(tmp_path) -> tuple[TestClient, AppSettings]:
    settings = AppSettings(
        data_root=tmp_path / "appdata",
        lightroom_root=tmp_path / "lightroom",
    )
    application = create_app(
        settings=settings,
        analysis_provider=MockAnalysisProvider(),
    )
    return TestClient(application), settings


def upload_analyze(client: TestClient) -> tuple[str, str]:
    upload = client.post(
        "/api/photos",
        files={"file": ("sample.jpg", jpeg_bytes(), "image/jpeg")},
    )
    assert upload.status_code == 200
    photo_id = upload.json()["photo_id"]
    analysis = client.post(f"/api/photos/{photo_id}/analyze")
    assert analysis.status_code == 200
    style_id = analysis.json()["recommended_styles"][0]["id"]
    return photo_id, style_id


def test_lightroom_status_and_plugin_install(tmp_path):
    client, settings = make_lightroom_client(tmp_path)

    status = client.get("/api/lightroom/status")
    assert status.status_code == 200
    body = status.json()
    assert body["detected"] is True
    assert body["lightroom_root"] == str(settings.lightroom_root)
    assert body["plugin_installed"] is False
    assert body["plugin_source"] == str(PLUGIN_SOURCE_DIR)

    installed = client.post("/api/lightroom/install-plugin")
    assert installed.status_code == 200
    installed_body = installed.json()
    assert installed_body["installed"] is True
    plugin_dir = Path(installed_body["plugin_dir"])
    assert (plugin_dir / "Info.lua").is_file()
    assert (plugin_dir / "Actions.lua").is_file()
    # 1.7.1: the Windows topmost helper must travel with the rest of the plugin.
    assert (plugin_dir / "ReviewWindowTopmost.ps1").is_file()

    refreshed = client.get("/api/lightroom/status").json()
    assert refreshed["plugin_installed"] is True


def test_develop_endpoint_returns_lightroom_settings_and_xmp(tmp_path):
    client, _settings = make_lightroom_client(tmp_path)
    photo_id, style_id = upload_analyze(client)

    response = client.post(
        f"/api/photos/{photo_id}/develop",
        json={"style_id": style_id, "intensity": 0.7},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["style_id"] == style_id
    assert body["mapping_version"] == "lightroom_xmp_v2"
    assert body["develop_settings_version"] == "lightroom_develop_v1"
    assert body["xmp_url"].endswith(".xmp")
    assert "preset_path" not in body
    settings = body["develop_settings"]
    assert settings["ProcessVersion"] == "15.4"
    assert settings["Exposure2012"] == round(body["parameters"]["exposure"], 2)
    assert settings["ToneCurvePV2012"] == [0, 0, 64, 60, 128, 128, 192, 196, 255, 255]
    for key in (
        "Contrast2012",
        "Highlights2012",
        "Shadows2012",
        "Whites2012",
        "Blacks2012",
        "Texture",
        "Clarity2012",
        "Dehaze",
        "HueAdjustmentRed",
        "SplitToningShadowHue",
        "Sharpness",
        "LuminanceSmoothing",
    ):
        assert key in settings
    assert client.get(body["xmp_url"]).status_code == 200


def test_send_to_lightroom_installs_preset_plugin_and_job(tmp_path):
    client, settings = make_lightroom_client(tmp_path)
    photo_id, style_id = upload_analyze(client)

    response = client.post(
        f"/api/photos/{photo_id}/lightroom",
        json={"style_id": style_id, "intensity": 0.7, "install_plugin": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["plugin_installed"] is True
    assert body["job_id"].startswith("job_")
    assert Path(body["preset_path"]).is_file()
    assert Path(body["preset_path"]).is_relative_to(
        settings.lightroom_root / "Develop Presets"
    )
    assert Path(body["preset_path"]).suffix == ".xmp"

    inbox = settings.lightroom_root / BRIDGE_DIR_NAME / "inbox"
    jobs = list(inbox.glob("*.json"))
    assert len(jobs) == 1
    payload = json.loads(jobs[0].read_text(encoding="utf-8"))
    assert payload["schema_version"] == JOB_SCHEMA_VERSION
    assert payload["job_id"] == body["job_id"]
    assert payload["develop_settings"]["ProcessVersion"] == "15.4"
    assert payload["style"]["id"] == style_id
    assert payload["photo"]["photo_id"] == photo_id
    assert payload["preset_path"] == body["preset_path"]
    assert payload["server_url"].startswith("http://127.0.0.1:")
    assert isinstance(payload["develop_settings"]["ToneCurvePV2012"], list)

    plugin_dir = settings.lightroom_root / "Modules" / PLUGIN_FOLDER_NAME
    assert (plugin_dir / "Info.lua").is_file()


def test_send_without_install_keeps_job_but_reports_missing_plugin(tmp_path):
    client, settings = make_lightroom_client(tmp_path)
    photo_id, style_id = upload_analyze(client)

    response = client.post(
        f"/api/photos/{photo_id}/lightroom",
        json={"style_id": style_id, "intensity": 0.5, "install_plugin": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "plugin_not_installed"
    assert body["plugin_installed"] is False
    assert Path(body["preset_path"]).is_file()
    assert list((settings.lightroom_root / BRIDGE_DIR_NAME / "inbox").glob("*.json"))
    plugin_dir = settings.lightroom_root / "Modules" / PLUGIN_FOLDER_NAME
    assert not plugin_dir.exists()


def test_bridge_expands_environment_variables_in_configured_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AIPA_TEST_ROOT", str(tmp_path))
    token = "%AIPA_TEST_ROOT%" if os.name == "nt" else "${AIPA_TEST_ROOT}"
    monkeypatch.setenv("AI_PHOTO_AGENT_LIGHTROOM_ROOT", f"{token}/Lightroom")

    bridge = LightroomBridge()

    assert bridge.configured_root() == (tmp_path / "Lightroom").resolve()


def test_send_without_lightroom_returns_actionable_status(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_PHOTO_AGENT_LIGHTROOM_ROOT", raising=False)
    monkeypatch.setattr(bridge_module, "_default_lightroom_roots", lambda: ())
    bridge = LightroomBridge()
    application = create_app(
        storage=FileStorage(tmp_path / "appdata"),
        analysis_provider=MockAnalysisProvider(),
        lightroom_bridge=bridge,
    )
    client = TestClient(application)
    photo_id, style_id = upload_analyze(client)

    response = client.post(
        f"/api/photos/{photo_id}/lightroom",
        json={"style_id": style_id, "intensity": 0.5},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "lightroom_not_found"
    assert body["job_id"] is None
    assert body["preset_path"] is None
    assert body["xmp_url"].endswith(".xmp")
    assert "Lightroom" in body["message"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AI Photo Agent - 自然纪实 - 70", "AI Photo Agent - 自然纪实 - 70"),
        ("bad/name:with*chars?", "bad_name_with_chars_"),
        ("   ...   ", "AI Photo Agent Preset"),
        ("CON", "_CON"),
        ("com1.jpg", "_com1.jpg"),
        ("LPT9", "_LPT9"),
    ],
)
def test_preset_filename_sanitization(raw, expected):
    assert _safe_preset_filename(raw) == expected


def test_plugin_package_is_complete_and_uses_required_apis():
    info = (PLUGIN_SOURCE_DIR / "Info.lua").read_text(encoding="utf-8")
    assert "LrInitPlugin" in info
    assert "LrLibraryMenuItems" in info
    assert "LrExportMenuItems" in info
    menu_files = re.findall(r'file\s*=\s*"([^"]+)"', info)
    assert menu_files
    for file_name in menu_files:
        assert (PLUGIN_SOURCE_DIR / file_name).is_file()

    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert "applyDevelopSettings" in actions
    assert "createDevelopSnapshot" in actions
    assert "photo:createVirtualCopy" in actions
    assert "USE_VIRTUAL_COPY" in (PLUGIN_SOURCE_DIR / "Config.lua").read_text(encoding="utf-8")
    assert 'switchToModule("develop")' in actions
    assert "ToneCurvePV2012" not in actions  # settings are data-driven from the service
    assert "requestJpegThumbnail" in actions
    assert "postMultipart" in (PLUGIN_SOURCE_DIR / "Http.lua").read_text(encoding="utf-8")
    assert (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").is_file()
    assert (PLUGIN_SOURCE_DIR / "ReviewWindowTopmost.ps1").is_file()
    assert "schema_version" in JOB_SCHEMA_VERSION or JOB_SCHEMA_VERSION == "aipa_lightroom_job_v1"


def test_plugin_menu_has_no_probe_entries():
    info = (PLUGIN_SOURCE_DIR / "Info.lua").read_text(encoding="utf-8")
    assert "Probe" not in info


def test_plugin_analyzes_through_progress_job():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert "/analyze/jobs" in actions
    assert "ProgressWindow.run" in actions
    # The old blocking call stays only as the fallback for an older service.
    assert '"/analyze"' in actions
    http = (PLUGIN_SOURCE_DIR / "Http.lua").read_text(encoding="utf-8")
    assert "get_with_timeout" in http
    assert "post_json_with_timeout" in http
    config = (PLUGIN_SOURCE_DIR / "Config.lua").read_text(encoding="utf-8")
    assert "JOB_POLL_SECONDS" in config
    assert "JOB_TIMEOUT_SECONDS" in config
    progress = (PLUGIN_SOURCE_DIR / "ProgressWindow.lua").read_text(encoding="utf-8")
    assert "STAGE_TITLES" in progress
    assert "estimate_progress" in progress


def test_plugin_progress_prefers_modal_dialog_and_keeps_bezel_fallback():
    progress = (PLUGIN_SOURCE_DIR / "ProgressWindow.lua").read_text(encoding="utf-8")
    # A real progress window: the bezel alone was invisible during analysis.
    assert "showModalProgressDialog" in progress
    assert "callWithContext" in progress
    assert "setCaption" in progress
    assert "setPortionComplete" in progress
    assert "isCanceled" in progress
    # Older SDKs must still get some feedback.
    assert "function ProgressWindow.wait_for_job" in progress
    assert "showBezel" in progress


def test_plugin_cancel_posts_to_the_cancel_endpoint():
    # Regression: the cancel call used the status path, which is GET-only, so
    # the service answered 405 and the analysis kept running.
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert 'path .. "/cancel"' in actions
    assert 'Http.post_json_with_timeout(path .. "/cancel"' in actions


def test_plugin_cancel_is_delivered_and_retried():
    # Cancelling in the dialog stopped the wait but the service kept analysing,
    # so the next click looked queued behind an abandoned task.
    progress = (PLUGIN_SOURCE_DIR / "ProgressWindow.lua").read_text(encoding="utf-8")
    assert "api.cancel(path)" in progress
    assert progress.count("api.cancel(path)") == 2, "cancel must be retried once"
    assert "cancel acknowledged by the service" in progress
    assert "ProgressWindow.CANCEL_MESSAGE" in progress


def test_plugin_falls_back_to_the_original_file_when_the_thumbnail_fails():
    # Lightroom answers "error loading thumb" for photos it cannot preview
    # (16-bit TIFF, damaged cache, offline file). Upload the original instead of
    # dead-ending with that raw message.
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert "local function original_image_file(photo)" in actions
    assert 'photo:getRawMetadata("path")' in actions
    assert "LrFileUtils.readFile(path)" in actions
    assert 'photo:getRawMetadata("fileFormat")' in actions
    assert 'photo:getRawMetadata("fileSize")' in actions
    # getRawMetadata throws on unknown keys: "pathname" is not a documented one
    # and produced "出现了内部错误: Unknown key: pathname" on a real click.
    assert 'getRawMetadata("pathname")' not in actions
    assert "Http.upload_image(image_data, file_name, content_type)" in actions
    assert 'Config.THUMBNAIL_RETRY_SIZE' in actions
    # The fallback must run before the upload, not after it.
    assert actions.index("original_image_file(photo)") < actions.index(
        "Http.upload_image(image_data, file_name, content_type)"
    )


def test_plugin_rejects_unsupported_formats_with_a_named_error():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert "ORIGINAL_CONTENT_TYPES" in actions
    assert "jpg = \"image/jpeg\"" in actions
    assert "cr3 = \"image/x-canon-cr3\"" in actions
    assert "原文件上传只支持" in actions


def test_plugin_cancel_is_not_reported_as_an_error():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert "analysis == ProgressWindow.CANCEL_MESSAGE" in actions
    assert "照片没有改动" in actions


def test_plugin_menu_entries_never_fail_silently():
    info = (PLUGIN_SOURCE_DIR / "Info.lua").read_text(encoding="utf-8")
    menu_files = sorted(set(re.findall(r'file\s*=\s*"([^"]+)"', info)))
    assert len(menu_files) == 5
    for file_name in menu_files:
        source = (PLUGIN_SOURCE_DIR / file_name).read_text(encoding="utf-8")
        # Without this, Lightroom swallows exceptions and the menu item looks
        # like it did nothing at all.
        assert "attachErrorDialogToFunctionContext" in source, file_name
        assert "log_info(" in source, file_name


def test_plugin_menu_reachable_from_any_module():
    # The SDK only defines three menu sections: LrLibraryMenuItems (图库, visible
    # in the Library module only), LrExportMenuItems (文件, visible in every
    # module) and LrHelpMenuItems. There is no Develop-module menu API, so the
    # Develop workflow keeps launchable through the File menu.
    info = (PLUGIN_SOURCE_DIR / "Info.lua").read_text(encoding="utf-8")
    assert info.count("local function menu_items()") == 1
    assert "LrLibraryMenuItems = menu_items()" in info
    assert "LrExportMenuItems = menu_items()" in info
    for file_name in sorted(set(re.findall(r'file\s*=\s*"([^"]+)"', info))):
        assert (PLUGIN_SOURCE_DIR / file_name).is_file(), file_name


def test_plugin_never_wraps_yielding_calls_in_pcall():
    # A standard pcall introduces a C-call boundary that Lua 5.1 cannot resume
    # across; Lightroom aborts with "Yielding is not allowed within a C or
    # metamethod call". LrTasks.pcall is the SDK's yield-safe guard and is the
    # only wrapper allowed in the async task (1.7.2 keeps the watcher alive
    # with it).
    for file_name in (
        "AnalyzeSelected.lua",
        "ApplyPending.lua",
        "ShowStatus.lua",
        "OpenWeb.lua",
    ):
        source = (PLUGIN_SOURCE_DIR / file_name).read_text(encoding="utf-8")
        assert "pcall(" not in source, file_name
    init_plugin = (PLUGIN_SOURCE_DIR / "InitPlugin.lua").read_text(encoding="utf-8")
    assert "LrTasks.pcall(Actions.poll_inbox)" in init_plugin
    assert "LrTasks.pcall(Actions.recover_bridge_jobs)" in init_plugin
    # Strip the only allowed wrapper: no other pcall may appear.
    assert "pcall(" not in init_plugin.replace("LrTasks.pcall(", "")
    progress = (PLUGIN_SOURCE_DIR / "ProgressWindow.lua").read_text(encoding="utf-8")
    for forbidden in (
        "pcall(LrFunctionContext",
        "pcall(api.",
        "pcall(scope.",
        "pcall(options.on_update",
    ):
        assert forbidden not in progress, forbidden


def test_plugin_clamps_bezel_fade_delay():
    # LrDialogs.showBezel raises "fade delay must be a number of seconds between
    # 0 and 10" for anything longer, which used to kill the action right after
    # the click. Every bezel therefore goes through a clamping helper.
    for file_name in ("Actions.lua", "ProgressWindow.lua"):
        source = (PLUGIN_SOURCE_DIR / file_name).read_text(encoding="utf-8")
        helper = re.search(
            r"local function show_bezel\(message, seconds\).*?\nend", source, re.DOTALL
        )
        assert helper, file_name
        assert "LrDialogs.showBezel(message" in helper.group(0), file_name
        assert "BEZEL_MAX_SECONDS = 10" in source, file_name
        # The helper body must not call itself again (a replace-all once turned
        # it into infinite recursion), so only the definition mentions it.
        assert helper.group(0).count("show_bezel(") == 1, file_name
        remainder = source.replace(helper.group(0), "")
        assert "LrDialogs.showBezel(" not in remainder, file_name


def test_plugin_analyze_shows_feedback_before_catalog_work():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    body = actions.split("function Actions.analyze_and_apply()", 1)[1]
    assert body.index("正在准备照片") < body.index("get_target_photo()")


def test_plugin_startup_self_check_reports_service_reachability():
    init = (PLUGIN_SOURCE_DIR / "InitPlugin.lua").read_text(encoding="utf-8")
    assert "availability_summary" in init
    info = (PLUGIN_SOURCE_DIR / "Info.lua").read_text(encoding="utf-8")
    assert "minor = 7" in info


def test_plugin_previews_before_keeping():
    # 1.2.0 turns "apply immediately" into "preview, then confirm": the original
    # develop settings are captured and a Before snapshot is written before the
    # style is ever applied, so cancelling can always restore the photo.
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    session = actions.split("local function begin_preview_session(", 1)[1].split(
        "local function delete_preview_snapshot(", 1
    )[0]
    assert session.index("getDevelopSettings()") < session.index("createDevelopSnapshot(")

    body = actions.split("function Actions.analyze_and_apply()", 1)[1]
    assert body.index("begin_preview_session(") < body.index("preview_style(")
    assert body.index("preview_style(") < body.index("confirm_keep(")
    for label in ("保留这些调整", "换一个风格", "取消并还原"):
        assert label in actions, label
    confirm = re.search(r"LrDialogs\.confirm\((.*?)\n    \)", actions, re.DOTALL)
    assert confirm, "confirm_keep must call LrDialogs.confirm"
    assert '"换一个风格"' in confirm.group(1)


def test_plugin_cancel_restores_original():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert "applyDevelopSettings(original" in actions
    assert "deleteDevelopSnapshot(" in actions
    assert actions.index("local function restore_preview(") < actions.index(
        "function Actions.analyze_and_apply()"
    )


def test_plugin_style_picker_previews_instead_of_applies():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert "应用并预览" not in actions
    assert "预览效果" in actions


def test_plugin_parameter_summary_covers_core_sliders():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    body = actions.split("local PARAMETER_ROWS = {", 1)[1].split(
        "local function preview_style(", 1
    )[0]
    for label in ("曝光", "对比度", "高光", "阴影", "色温", "自然饱和度", "锐化", "降噪"):
        assert label in body, label
    assert "本次没有需要应用的调整。" in body


def test_plugin_style_picker_defaults_to_full_intensity():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    assert "props.intensity = 100" in window
    assert "props.intensity = 70" not in actions + window
    assert "默认 70" not in actions


def test_plugin_unwraps_job_status_before_showing_analysis():
    # ProgressWindow.run() resolves to the task *status* (analysis nested under
    # .analysis). Passing that straight to format_analysis() produced an empty
    # "总体判断：" dialog and an empty style picker that looked like a cancel.
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    body = actions.split("local function analyze_with_progress(", 1)[1].split(
        "local function upload_and_analyze(", 1
    )[0]
    assert "local status, status_error = ProgressWindow.run" in body
    assert 'status.stage == "failed"' in body
    assert "return status.analysis" in body
    assert body.index("return status.analysis") > body.index("ProgressWindow.run")


def test_plugin_analysis_job_request_is_a_json_object():
    # The SDK JSON encoder renders an empty Lua table as `[]`. A strict service
    # body model answered 422 to that, which silently forced the plug-in onto
    # the blocking endpoint (no progress window). Always post a real object.
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    job_call = actions.split('"/analyze/jobs",', 1)[1].split(")", 1)[0]
    assert "provider = Config.DEFAULT_PROVIDER" in job_call
    assert "{}," not in job_call

    config = (PLUGIN_SOURCE_DIR / "Config.lua").read_text(encoding="utf-8")
    assert 'DEFAULT_PROVIDER = "default"' in config


def test_plugin_starts_local_service_before_uploading():
    # 1.2.1: the FastAPI service is usually down on the first click of the day,
    # so the plug-in launches it and waits instead of failing with "请求失败".
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    body = actions.split("function Actions.analyze_and_apply()", 1)[1]
    assert body.index("ensure_service()") < body.index("upload_and_analyze(")

    bootstrap = actions.split("local function ensure_service()", 1)[1].split(
        "local function analyze_with_progress(", 1
    )[0]
    assert "AUTO_START_SERVICE" in bootstrap
    assert "LrTasks.execute(" in bootstrap
    assert "LrTasks.sleep(1)" in bootstrap
    # The wait loop must observe the real service, not just sleep.
    assert bootstrap.index("service_reachable()") < bootstrap.index("LrTasks.sleep(1)")


def test_plugin_service_launch_is_detached_and_configurable():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    command = actions.split("local function service_launch_command()", 1)[1].split(
        "local function service_reachable()", 1
    )[0]
    # PowerShell Start-Process keeps the service alive after cmd.exe exits and
    # hides its window, unlike `start /B`.
    assert "Start-Process" in command
    assert "-WindowStyle Hidden" in command
    assert "run_app.py" in command
    assert "SERVICE_PROJECT_DIR" in actions

    config = (PLUGIN_SOURCE_DIR / "Config.lua").read_text(encoding="utf-8")
    assert "AUTO_START_SERVICE = true" in config
    assert "SERVICE_START_WAIT_SECONDS = 45" in config
    assert "SERVICE_PROJECT_DIR" in config


def test_plugin_config_supports_local_overrides():
    config = (PLUGIN_SOURCE_DIR / "Config.lua").read_text(encoding="utf-8")
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    example = (PLUGIN_SOURCE_DIR / "Config.local.example.lua").read_text(encoding="utf-8")

    # Machine-specific values live in the git-ignored Config.local.lua, not in
    # the tracked defaults.
    assert 'SERVICE_PROJECT_DIR = ""' in config
    assert "Config.local.lua" in config
    assert "修改 Config.lua" not in actions
    assert "在 Config.local.lua 中设置 SERVICE_PROJECT_DIR" in actions
    assert "SERVICE_PROJECT_DIR" in example
    assert "SERVER_URL" in example


def test_plugin_service_launch_never_wraps_yielding_calls_in_pcall():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    bootstrap = actions.split("local function ensure_service()", 1)[1].split(
        "local function analyze_with_progress(", 1
    )[0]
    assert "pcall(" not in bootstrap


def test_plugin_review_window_wraps_long_text():
    # 1.4.1: static_text renders exactly one line unless height_in_lines = -1 and a
    # width are both set ("LrView text properties"), so 1.4.0 clipped both the
    # analysis text and the parameter summary to a single line.
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    assert window.count("height_in_lines = -1") >= 2
    assert "height_in_lines = STATUS_LINES" in window
    assert "STATUS_LINES = 3" in window
    assert "local function split_lines(" in window
    assert "local function paragraph_nodes(" in window
    assert "unpack(paragraph_nodes(factory, options.analysis_text))" in window
    assert "ANALYSIS_VIEW_HEIGHT = 220" in window
    assert "DETAIL_VIEW_HEIGHT = 140" in window
    # A frame remembered by 1.4.0 would restore a window sized for clipped text.
    assert 'ReviewWindow.FRAME_KEY = "AIPhotoAgentReview2"' in window

    info = (PLUGIN_SOURCE_DIR / "Info.lua").read_text(encoding="utf-8")
    assert "minor = 7" in info


def test_plugin_reviews_inside_one_window():
    # 1.4.0: one floating window replaces the analysis message box, the modal
    # style picker and the confirmation dialog that followed the progress window.
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    assert "LrDialogs.presentFloatingDialog(" in window
    assert "blockTask = false" in window
    assert "save_frame = ReviewWindow.FRAME_KEY" in window
    assert "windowWillClose" in window

    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert "ReviewWindow.run {" in actions
    body = actions.split("function Actions.analyze_and_apply()", 1)[1]
    message = 'LrDialogs.message("AI Photo Agent - 修图建议"'
    # The standalone analysis dialog survives only in the fallback branch.
    assert body.count(message) == 1
    assert body.index("ReviewWindow.available()") < body.index(message)


def test_plugin_review_window_covers_preview_keep_cancel():
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    for label in ("预览效果", "保留调整", "取消并还原"):
        assert label in window, label
    for widget in ("push_button", "scrolled_view", "separator", "popup_menu", "slider"):
        assert widget in window, widget
    # 1.6.2 dropped the in-window compare button: rewriting develop settings from
    # inside the polling loop could throw and tear the session down, and
    # Lightroom's own backslash before/after already does that job.
    assert "对比原图" not in window
    assert "state.request = \"compare\"" not in window
    assert "按 \\\\ 对比修改前后" in window


def test_plugin_review_window_runs_io_outside_button_callbacks():
    # Button callbacks run on the UI's nested loop, where yielding across a C
    # boundary fails. ReviewWindow only records the request; the loop (and the
    # injected Actions callbacks) perform the HTTP and the catalog writes.
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    for forbidden in ("Http.", "withWriteAccessDo", "applyDevelopSettings", "LrDialogs.confirm("):
        assert forbidden not in window, forbidden
    assert "while not state.closed" in window
    assert "LrTasks.sleep(" in window
    for request in ("state.request = \"preview\"",
                    "state.request = \"keep\"", "state.request = \"cancel\""):
        assert request in window, request


def test_plugin_review_window_falls_back_to_the_modal_flow():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    body = actions.split("function Actions.analyze_and_apply()", 1)[1]
    assert "ReviewWindow.available()" in body
    # The pre-1.4 three-step flow must still exist for SDK < 5.0.
    assert "choose_style(" in body
    assert "confirm_keep(" in body
    assert "LrDialogs.confirm(" in actions


def test_plugin_review_window_never_wraps_yielding_calls_in_pcall():
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    for forbidden in (
        "pcall(LrFunctionContext",
        "pcall(dialog_api.",
        "pcall(options.",
        "pcall(state.",
        "pcall(LrDialogs.",
    ):
        assert forbidden not in window, forbidden


def test_plugin_prefers_the_server_parameter_summary():
    # The backend renders one summary string from app/services/parameter_specs.py,
    # so a newly supported slider shows up in Lightroom without duplicating labels.
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert "local function parameter_summary_of(development)" in actions
    assert "development.parameters_summary" in actions
    assert "parameter_summary = parameter_summary_of," in actions
    assert "return format_parameters(development.parameters)" in actions


def test_review_window_renders_the_server_summary():
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    assert "options.parameter_summary(development)" in window
def test_review_window_can_be_brought_back_to_the_front():
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    info = (PLUGIN_SOURCE_DIR / "Info.lua").read_text(encoding="utf-8")

    # Lightroom hides a floating dialog behind the main window the moment the
    # user clicks the catalog, and the SDK has no always-on-top flag. 1.7.0
    # therefore does not trust toFront(): the menu asks the running loop to
    # close the old window and present a fresh one, and only falls back to a
    # disk rebuild when the loop does not acknowledge.
    assert "ReviewWindow._dialog = api" in window
    assert "function ReviewWindow.bring_to_front()" in window
    assert "pcall(api.toFront)" in window
    assert "selectionChangeObserver = function()" in window
    assert "sourceChangeObserver = function()" in window
    assert "ReviewWindow._dialog = nil" in window

    assert "function Actions.show_review_window()" in actions
    assert "ReviewWindow.request_recall()" in actions
    assert "ReviewWindow.recall_acknowledged(recall_token)" in actions
    assert "ReviewWindow.release_stale()" in actions
    assert "the review loop did not answer; releasing the stale session" in actions
    assert "当前没有打开的分析窗口" in actions

    assert 'file = "ShowReview.lua"' in info
    assert (PLUGIN_SOURCE_DIR / "ShowReview.lua").is_file()
    assert "minor = 7" in info


def test_review_window_re_presents_itself_when_the_handle_is_missing():
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    # onShow does not always fire, so the recall path must not depend on the
    # dialog handle: the menu asks the running loop to present a fresh window
    # on the same state instead, and the loop acknowledges the request.
    assert "ReviewWindow._recall = false" in window
    assert "function ReviewWindow.request_recall()" in window
    assert "function ReviewWindow.is_active()" in window
    assert "function ReviewWindow.recall_acknowledged(" in window
    assert "ReviewWindow._recall_seq" in window
    assert "ReviewWindow._recall_ack" in window
    assert "local function build_contents()" in window
    assert "local function present()" in window
    assert window.count("present()") >= 3
    assert "if ReviewWindow._recall then" in window
    assert "review: recall requested, presenting the dialog again" in window
    assert "review: recall acknowledged (seq=" in window
    assert "ReviewWindow.release_stale()" in window
    assert "review: released a stale review session" in window
    assert "state.suppress_close = true" in window
    assert "review: previous dialog closed for a re-present" in window
    assert "review: onShow fired, toFront=" in window

    assert "ReviewWindow.request_recall()" in actions
    assert "asked the review window to re-present itself" in actions
    assert "RecallWindow" not in actions  # typo guard: the module is ReviewWindow
    assert "no live review loop (" in actions
    assert "LrTasks.sleep(0.25)" in actions


def test_plugin_review_observers_never_touch_the_dialog():
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")

    # 1.6.4: observers run in Lightroom's UI context. Touching the dialog there
    # (toFront() on a handle Lightroom already destroyed) unwinds the review
    # session silently, which is the "window is on the taskbar but can never be
    # raised again" failure. They must only set a flag.
    assert "ReviewWindow._wake = true" in window
    for marker in ("selectionChangeObserver = function()", "sourceChangeObserver = function()"):
        start = window.index(marker)
        body = window[start : start + 160]
        assert "ReviewWindow._wake = true" in body, body
        assert "bring_to_front" not in body, body

    # The loop performs the raise instead of the observer.
    assert "if ReviewWindow._wake then" in window
    assert "raised the dialog after a selection change" in window
    # A closed window must never be handed to toFront().
    assert "windowWillClose = function()" in window
    closing = window[window.index("windowWillClose = function()") :][:220]
    assert "ReviewWindow._dialog = nil" in closing, closing


def test_plugin_rebuilds_the_review_window_from_saved_state():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")

    # 1.7.0: the review session lives on disk as well as in memory, so the menu
    # can always build a fresh window even after the original task died.
    assert 'local REVIEW_STATE_FILE = "review-state.json"' in actions
    assert "local REVIEW_STATE_TTL_SECONDS = 7 * 24 * 60 * 60" in actions
    assert "local clear_review_state" in actions
    assert "local function save_review_state(state)" in actions
    assert "local function load_review_state()" in actions
    assert "function clear_review_state()" in actions
    assert "state.saved_at = os.time()" in actions
    assert "the saved review state expired" in actions
    assert "function Actions.resume_review(state)" in actions
    assert "save_review_state(review_state)" in actions
    assert "local state = load_review_state()" in actions
    assert "resume_review: rebuilding the review window from the saved state" in actions
    assert "photo_path = tostring(photo:getRawMetadata(\"path\") or \"\")" in actions
    # A new analysis supersedes the previous photo's saved review, and every
    # terminal review path clears it again.
    assert actions.count("clear_review_state()") >= 8

    # Closing a window the user already dismissed must not kill the session.
    assert "pcall(handle.close)" in window
    start = window.index("local handle = ReviewWindow._dialog")
    closing = window[start : start + 700]
    assert "ReviewWindow._dialog = nil" in closing
    assert "review: closing the dialog failed" in closing


def test_plugin_falls_back_to_a_modal_review_when_the_window_cannot_return():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    # Modal dialogs always sit on top, so they are the guaranteed way to finish
    # a review when the floating panel cannot be rebuilt.
    assert "function Actions.review_with_state(state)" in actions
    assert "review_with_state: running the modal review flow" in actions
    assert "show_review_window: falling back to the modal review flow" in actions
    modal = actions[actions.index("function Actions.review_with_state(state)") :]
    modal = modal[: modal.index("function Actions.show_review_window()")]
    for call in ("choose_style(state.analysis)", "Http.post_json(", "preview_style(", "confirm_keep("):
        assert call in modal, call
    # A failed floating rebuild must hand over to the modal flow, not error out.
    resume = actions[actions.index("function Actions.resume_review(state)") :]
    resume = resume[: resume.index("function Actions.review_with_state(state)")]
    assert "return false" in resume


def test_plugin_shows_when_the_strength_was_adapted():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    assert "local function adaptive_note(development)" in actions
    assert "已按原图亮度自适应到" in actions
    assert actions.count("adaptive_note(development)") >= 2

def test_plugin_uses_the_single_window_review_by_default():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    config = (PLUGIN_SOURCE_DIR / "Config.lua").read_text(encoding="utf-8")

    # 1.7.0: the single floating window is back as the default; it is the only
    # review UI that keeps the photo visible while the preview is written. The
    # modal flow is now only the SDK < 5.0 / failed-window fallback.
    assert "USE_FLOATING_REVIEW = true" in config
    assert "if Config.USE_FLOATING_REVIEW and ReviewWindow.available() then" in actions
    assert "analyze_and_apply: using the single-window review flow" in actions
    assert "analyze_and_apply: using the modal review flow" in actions


def test_plugin_ships_the_review_topmost_helper():
    helper = PLUGIN_SOURCE_DIR / "ReviewWindowTopmost.ps1"
    assert helper.is_file()
    script = helper.read_text(encoding="utf-8")

    # The helper must be a self-contained Win32 watchdog: find the review frame,
    # restore it, drop the minimize box and keep it above Lightroom while
    # Lightroom is the foreground app.
    for marker in (
        "EnumWindows",
        "SetWindowPos",
        "HWND_TOPMOST",
        "HWND_NOTOPMOST",
        "WS_MINIMIZEBOX",
        "AgWinFrame",
        "TitlePrefix",
        "Once",
        "ForceTopmost",
        "SW_RESTORE",
    ):
        assert marker in script, marker
    # Local-only helper: no network, no focus stealing.
    for forbidden in ("Invoke-WebRequest", "http://", "https://", "Start-BitsTransfer"):
        assert forbidden not in script, forbidden
    assert "SetForegroundWindow(" not in script


def test_plugin_starts_the_topmost_helper_for_each_review():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    config = (PLUGIN_SOURCE_DIR / "Config.lua").read_text(encoding="utf-8")

    assert "KEEP_REVIEW_WINDOW_ON_TOP = true" in config
    assert "ReviewWindowTopmost.ps1" in actions
    assert "local function start_review_window_topmost()" in actions
    # 1.7.1 used package.config here; Lightroom's embedded Lua has no `package`
    # library and the menu action died with "attempt to index global 'package'".
    assert "package.config" not in actions
    assert 'LrPathUtils.getStandardFilePath("temp")' in actions
    assert 'temp_dir:sub(1, 1) == "/"' in actions
    assert "topmost helper is missing at" in actions
    # Best effort: a failed/missing helper must never abort the review.
    assert "topmost helper launcher exited with" in actions
    assert "LrTasks.execute(command)" in actions
    assert "LrTasks.pcall(start_review_window_topmost)" in actions
    assert "review: topmost helper failed to start" in actions
    # It must run before the window is presented, for both analyze and resume.
    body = actions.split("local function review_analysis(", 1)[1]
    assert body.index("LrTasks.pcall(start_review_window_topmost)") < body.index(
        "ReviewWindow.run {"
    )
    # The helper function itself must never be called bare in review_analysis.
    assert "    start_review_window_topmost()" not in body


def test_plugin_review_loop_has_a_heartbeat_and_ack():
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    # The menu must be able to distinguish a buried window from a dead loop.
    for marker in (
        "ReviewWindow._pulse",
        "ReviewWindow._busy",
        "ReviewWindow.LIVENESS_SECONDS",
        "ReviewWindow.BUSY_LIMIT_SECONDS",
        "function ReviewWindow._touch()",
        "function ReviewWindow.is_live()",
        "function ReviewWindow.is_busy_alive()",
        "ReviewWindow._recall_ack = recall_seq",
    ):
        assert marker in window, marker
    assert "ReviewWindow.is_live()" in actions
    assert "ReviewWindow.is_busy_alive()" in actions


def test_plugin_menu_recall_never_leaves_a_dead_session():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    window = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")

    body = actions.split("function Actions.show_review_window()", 1)[1].split(
        "function Actions.analyze_and_apply()", 1
    )[0]
    # Ask the loop, poll for its acknowledgement, treat a busy preview as alive,
    # and only then release the stale session and rebuild from disk.
    assert body.index("ReviewWindow.request_recall()") < body.index(
        "recall_acknowledged(recall_token)"
    )
    assert body.index("recall_acknowledged(recall_token)") < body.index(
        "ReviewWindow.is_busy_alive()"
    )
    assert body.index("ReviewWindow.is_busy_alive()") < body.index("LrTasks.sleep(0.25)")
    assert body.index("LrTasks.sleep(0.25)") < body.index("ReviewWindow.release_stale()")
    assert body.index("ReviewWindow.release_stale()") < body.index("load_review_state()")
    assert "正在重新打开修图窗口" in body

    # A window closed by the user must not be resurrected by a pending recall.
    assert "review: could not raise the dialog; presenting a fresh one" in window
    assert "if state.closed then" in window


def test_plugin_clears_the_review_state_after_every_review():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    assert "review: cleared the saved review state" in actions
    assert "function clear_review_state()" in actions

    modal = actions.split("function Actions.review_with_state(state)", 1)[1].split(
        "function Actions.review_diagnostic()", 1
    )[0]
    # selection cancel / develop error / preview error / keep / cancel
    assert modal.count("clear_review_state()") >= 5
    body = actions.split("function Actions.analyze_and_apply()", 1)[1]
    # start / selection cancel / develop error / preview error / keep / cancel
    # plus the floating-flow keep/cancel branches
    assert body.count("clear_review_state()") >= 6


def test_plugin_guards_against_starting_a_second_review():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    body = actions.split("function Actions.analyze_and_apply()", 1)[1]
    assert body.index("ReviewWindow.is_live()") < body.index("clear_review_state()")
    assert "another review window or analysis is active" in body
    assert "已经有一个修图窗口" in body
    # The in-flight analysis lock is released through the yield-safe wrapper,
    # so an exception can never leave it stuck and block every later click.
    assert "local run_ok, run_error = LrTasks.pcall(run_analysis_flow)" in body
    assert "analysis_in_progress = false" in body


def test_plugin_resume_review_respects_the_floating_switch():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")
    resume = actions.split("function Actions.resume_review(state)", 1)[1].split(
        "function Actions.review_with_state(state)", 1
    )[0]
    assert "if not (Config.USE_FLOATING_REVIEW and ReviewWindow.available()) then" in resume
    assert "resume_review: floating review is disabled or unavailable" in resume


def test_plugin_explains_why_there_is_no_review_window():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    # Every branch of the recovery chain must leave a log line, otherwise the
    # user report "it says there is no window" cannot be diagnosed.
    assert "function Actions.review_diagnostic()" in actions
    assert "评审状态：" in actions
    assert "show_review_window: invoked (floating=" in actions
    assert "review: saved the review state to " in actions
    assert "review: no saved review state at " in actions
    assert "review: could not decode the saved review state" in actions
    assert "review: the saved review state is incomplete" in actions
    # The menu is never a dead end: with nothing to resume it offers to analyze.
    assert "没有找到未完成的评审。要现在分析当前选中的照片吗？" in actions


def test_plugin_writes_the_review_state_atomically_in_user_data():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    # LrFileUtils has no writeFile on the user's Lightroom build: calling it
    # raised "attempt to call field 'writeFile' (a nil value)" right after
    # analysis, killing the flow before the review window opened.
    assert "LrFileUtils.writeFile(" not in actions
    assert "local function write_text_file(path, contents)" in actions
    # The state used to live inside the plug-in bundle and was truncated by a
    # crash mid-write; 1.7.2 writes a sibling temp file, moves it into place and
    # stores it under Lightroom's userData directory.
    assert 'pcall(io.open, temp_path, "w")' in actions
    assert "local temp_path = path .. \".tmp\"" in actions
    assert "pcall(LrFileUtils.move, temp_path, path)" in actions
    assert "LrPathUtils.getStandardFilePath, \"userData\"" in actions
    assert "local function legacy_review_state_path()" in actions
    assert "write_text_file(path, encoded)" in actions
    # LrFileUtils.readFile drops the last byte of each line, which corrupts the
    # saved JSON, so the read path uses io as well.
    assert "local function read_text_file(path)" in actions
    assert "read_text_file(path)" in actions
    assert 'file:read("*a")' in actions
    # Clearing the state must never raise: a failed delete after "keep" used to
    # surface a raw Lua error instead of the final confirmation.
    assert "local removed, remove_error = pcall(LrFileUtils.delete, path)" in actions


def test_plugin_watcher_survives_a_failed_poll():
    init_plugin = (PLUGIN_SOURCE_DIR / "InitPlugin.lua").read_text(encoding="utf-8")

    # A single Lua error inside poll_inbox (a failed catalog write or an
    # unreadable bridge directory) used to terminate the async watcher for the
    # rest of the Lightroom session, silently disabling auto-apply.
    assert "local poll_ok, poll_error = LrTasks.pcall(Actions.poll_inbox)" in init_plugin
    assert 'Actions.log_info("watcher: poll failed: " .. tostring(poll_error))' in init_plugin
    assert "LrTasks.pcall(Actions.recover_bridge_jobs)" in init_plugin


def test_plugin_serialises_analyses_and_restores_the_previous_state():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    # Two menu clicks used to upload and analyze twice, overwriting the first
    # review state and creating duplicate Before snapshots.
    assert "local analysis_in_progress = false" in actions
    assert "if analysis_in_progress or ReviewWindow.is_live()" in actions
    assert "analysis_in_progress = true" in actions
    assert "analysis_in_progress = false" in actions
    # A failed run must leave the previously saved review untouched.
    assert "local previous_state = load_review_state()" in actions
    assert "save_review_state(previous_state)" in actions
    # A click while an analysis is still running is refused with a clear hint.
    assert "已经有一个修图窗口" in actions
    assert "if analysis_in_progress or ReviewWindow.is_live()" in actions


def test_plugin_never_rewrites_the_before_snapshot():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    assert 'local PREVIEW_SNAPSHOT_NAME = "AI Photo Agent - Before"' in actions
    # Both the preview session and a bridge apply reuse the same snapshot;
    # updateInPlace=true keeps exactly one reversible Before state per photo.
    assert actions.count("createDevelopSnapshot(PREVIEW_SNAPSHOT_NAME, true)") >= 2
    assert 'createDevelopSnapshot("AI Photo Agent - Before"' not in actions
    assert "target:getDevelopSnapshots()" in actions


def test_plugin_verifies_the_bridge_target_photo_identity():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    assert "local function job_original_filename(job)" in actions
    assert "job.photo.original_filename" in actions
    assert 'pcall(photo.getRawMetadata, photo, "fileName")' in actions
    assert "任务目标照片与当前选中照片不一致" in actions
    assert "job.target_verified = job_has_identity(job)" in actions
    # Jobs from an older service carry no identity: still applied, but the user
    # is told that the target photo could not be verified.
    assert "无法自动校验目标照片" in actions
    assert "已应用待处理修图（无法校验目标照片，请核对）" in actions


def test_plugin_recovers_jobs_stranded_by_a_crash():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    assert "function Actions.recover_bridge_jobs()" in actions
    assert "local PROCESSING_STALE_SECONDS = 10 * 60" in actions
    assert "local PROCESSING_MAX_ATTEMPTS = 3" in actions
    assert "job.attempts = attempts + 1" in actions
    assert "bridge: recovered %d stale job(s), failed %d, pruned %d old artifact(s)" in actions


def test_plugin_cancels_the_server_job_on_timeout():
    progress = (PLUGIN_SOURCE_DIR / "ProgressWindow.lua").read_text(encoding="utf-8")

    assert "local function cancel_server_job(api, path, log, reason)" in progress
    assert progress.count("api.cancel(path)") == 2
    assert "state.timed_out" in progress
    assert 'cancel_server_job(api, path, log, "user cancel")' in progress
    assert 'cancel_server_job(api, path, options.log or function() end, "timeout")' in progress


def test_plugin_only_falls_back_for_missing_job_endpoints():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    # A transient 500/422 must not silently downgrade to the sync endpoint,
    # which hides real service errors and loses progress.
    assert 'job_error_text:find("HTTP 404", 1, true)' in actions
    assert 'job_error_text:find("HTTP 405", 1, true)' in actions
    assert 'job_error_text:find("HTTP 501", 1, true)' in actions
    assert "analyze: job API unavailable, falling back to sync analyze" in actions


def test_plugin_validates_the_fallback_project_directory():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    # The optional SERVICE_PROJECT_DIR override is only trusted when it really
    # contains run_app.py; otherwise auto-start reports a clear error instead of
    # launching PowerShell in an arbitrary directory.
    assert 'LrFileUtils.exists(LrPathUtils.child(plugin_root, "run_app.py")) == "file"' in actions
    assert 'LrFileUtils.exists(LrPathUtils.child(configured, "run_app.py")) == "file"' in actions
    assert "没有找到项目目录（run_app.py）" in actions


def test_plugin_shows_the_real_plugin_version():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    # Info.lua defines VERSION (uppercase); the status menu used to read
    # _PLUGIN.version and always fell back to 1.1.0.
    assert "local version = _PLUGIN.VERSION or _PLUGIN.version or {}" in actions


def test_plugin_truncates_text_on_utf8_boundaries():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    assert "local function utf8_safe_truncate(text, max_bytes, suffix)" in actions
    assert actions.count("utf8_safe_truncate(") >= 3
    assert ":sub(1, 4000)" not in actions


def test_plugin_caches_the_bridge_directory():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    assert "local bridge_dir_cache = nil" in actions
    assert "force_refresh" in actions
    # The hint file is read through the io-based helper, never LrFileUtils.readFile.
    assert "local hint = read_text_file(hint_path)" in actions


def test_plugin_quotes_the_powershell_launcher():
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    assert "local function ps_quote(value)" in actions
    assert "ps_quote(python)" in actions
    assert "ps_quote(project_dir)" in actions
    assert "ps_quote(script)" in actions


def test_review_window_distinguishes_a_user_closed_window():
    review = (PLUGIN_SOURCE_DIR / "ReviewWindow.lua").read_text(encoding="utf-8")
    actions = (PLUGIN_SOURCE_DIR / "Actions.lua").read_text(encoding="utf-8")

    # A closed window must not be reported as a "cancel" button decision: the
    # caller needs to know that nothing was applied so it can restore the photo.
    assert 'return nil, "closed"' in review
    assert 'window_error == "closed"' in actions
