from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import AppSettings, expand_path
from app.errors import StorageOperationError
from app.schemas import (
    DevelopResponse,
    LightroomPluginInstallResponse,
    LightroomSendResponse,
    LightroomStatusResponse,
)


BASE_DIR = Path(__file__).resolve().parents[2]
PLUGIN_SOURCE_DIR = BASE_DIR / "lightroom" / "AIPhotoAgent.lrplugin"
PLUGIN_FOLDER_NAME = "AIPhotoAgent.lrplugin"
PRESET_FOLDER_NAME = "AI Photo Agent"
BRIDGE_DIR_NAME = "AI Photo Agent Bridge"
JOB_SCHEMA_VERSION = "aipa_lightroom_job_v1"
DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
MAX_PRESET_FILENAME_LENGTH = 120
_UNSAFE_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class LightroomLocations:
    root: Path
    presets_dir: Path
    modules_dir: Path
    bridge_dir: Path
    inbox_dir: Path
    done_dir: Path
    failed_dir: Path

    def create(self) -> None:
        for directory in (
            self.presets_dir,
            self.modules_dir,
            self.inbox_dir,
            self.done_dir,
            self.failed_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


class LightroomBridge:
    """Install presets/plugin and enqueue jobs for the Lightroom Classic plugin."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        server_url: str | None = None,
    ) -> None:
        self.settings = settings
        self._server_url = server_url

    @property
    def server_url(self) -> str:
        configured = os.getenv("AI_PHOTO_AGENT_SERVER_URL") or self._server_url
        return (configured or DEFAULT_SERVER_URL).rstrip("/")

    def configured_root(self) -> Path | None:
        if self.settings is not None and self.settings.lightroom_root is not None:
            return expand_path(self.settings.lightroom_root).resolve()
        override = os.getenv("AI_PHOTO_AGENT_LIGHTROOM_ROOT")
        if override and override.strip():
            return expand_path(override.strip()).resolve()
        return None

    def detect_root(self) -> Path | None:
        configured = self.configured_root()
        if configured is not None:
            return configured
        for candidate in _default_lightroom_roots():
            if candidate.is_dir():
                return candidate.resolve()
        return None

    def locations(self, *, create: bool = False) -> LightroomLocations | None:
        root = self.detect_root()
        if root is None:
            return None
        if not root.exists() and self.configured_root() is None:
            return None
        locations = LightroomLocations(
            root=root,
            presets_dir=root / "Develop Presets",
            modules_dir=root / "Modules",
            bridge_dir=root / BRIDGE_DIR_NAME,
            inbox_dir=root / BRIDGE_DIR_NAME / "inbox",
            done_dir=root / BRIDGE_DIR_NAME / "done",
            failed_dir=root / BRIDGE_DIR_NAME / "failed",
        )
        if create:
            locations.create()
        return locations

    def status(self) -> LightroomStatusResponse:
        root = self.detect_root()
        locations = self.locations()
        plugin_installed = bool(
            locations is not None and self._plugin_registered(locations)
        )
        if locations is None:
            message = (
                "未检测到 Lightroom Classic。仍可下载 XMP 手动导入；"
                "也可设置 AI_PHOTO_AGENT_LIGHTROOM_ROOT 指向 Lightroom 用户目录。"
            )
        elif plugin_installed:
            message = (
                "已检测到 Lightroom Classic，插件已注册。点击“发送到 Lightroom”后，"
                "在 Lightroom 中选中照片并运行“图库 > 增效工具附加功能 > "
                "AI Photo Agent - 应用 Web 端待处理修图”；插件会写入 Before 快照、"
                "应用参数并切换到修改照片模块。"
            )
        else:
            message = (
                "已检测到 Lightroom Classic，但插件尚未注册。"
                "请通过“文件 > 增效工具管理器 > 添加”选择 "
                "lightroom/AIPhotoAgent.lrplugin，然后重启 Lightroom。"
            )
        return LightroomStatusResponse(
            detected=locations is not None,
            lightroom_root=str(locations.root) if locations else None,
            presets_dir=str(locations.presets_dir) if locations else None,
            modules_dir=str(locations.modules_dir) if locations else None,
            bridge_inbox=str(locations.inbox_dir) if locations else None,
            plugin_installed=plugin_installed,
            plugin_source=str(PLUGIN_SOURCE_DIR),
            server_url=self.server_url,
            message=message,
        )

    def install_plugin(self) -> LightroomPluginInstallResponse:
        if not (PLUGIN_SOURCE_DIR / "Info.lua").is_file():
            raise StorageOperationError(
                "The Lightroom plugin package is missing from the project checkout."
            )
        locations = self.locations(create=True)
        if locations is None:
            return LightroomPluginInstallResponse(
                installed=False,
                plugin_dir=None,
                source_dir=str(PLUGIN_SOURCE_DIR),
                message="未检测到 Lightroom Classic，无法自动安装插件。",
            )
        destination = locations.modules_dir / PLUGIN_FOLDER_NAME
        try:
            shutil.copytree(PLUGIN_SOURCE_DIR, destination, dirs_exist_ok=True)
        except OSError as exc:
            raise StorageOperationError(
                "Unable to install the Lightroom plugin into the Lightroom modules folder."
            ) from exc
        self._write_bridge_hint(locations)
        return LightroomPluginInstallResponse(
            installed=(destination / "Info.lua").is_file(),
            plugin_dir=str(destination),
            source_dir=str(PLUGIN_SOURCE_DIR),
            message=(
                "Lightroom 插件已安装。请在 Lightroom Classic 中通过“文件 > 增效工具管理器”"
                "启用，并在“图库 > 增效工具附加功能”中使用。"
            ),
        )

    def send(
        self,
        development: DevelopResponse,
        *,
        install_plugin: bool = True,
    ) -> LightroomSendResponse:
        status = self.status()
        if not status.detected:
            return LightroomSendResponse(
                photo_id=development.photo_id,
                style_id=development.style_id,
                intensity=development.intensity,
                preset_name=development.preset_name,
                status="lightroom_not_found",
                job_id=None,
                preset_path=None,
                plugin_installed=False,
                lightroom_root=None,
                bridge_inbox=None,
                xmp_url=development.xmp_url,
                message=status.message,
            )

        locations = self.locations(create=True)
        if locations is None:  # pragma: no cover - defensive
            return LightroomSendResponse(
                photo_id=development.photo_id,
                style_id=development.style_id,
                intensity=development.intensity,
                preset_name=development.preset_name,
                status="lightroom_not_found",
                xmp_url=development.xmp_url,
                message=status.message,
            )

        if install_plugin:
            self.install_plugin()
        else:
            self._write_bridge_hint(locations)
        plugin_installed = (
            locations.modules_dir / PLUGIN_FOLDER_NAME / "Info.lua"
        ).is_file()

        preset_path = self._install_preset(development, locations)
        job_id = self._enqueue_job(development, locations, preset_path)
        if plugin_installed:
            message = (
                "已写入 Lightroom 预设并排队。请在 Lightroom 中选中目标照片，"
                "然后运行“图库 > 增效工具附加功能 > AI Photo Agent - 应用 Web 端待处理修图”。"
                "插件会写入 AI Photo Agent - Before 快照、应用参数并切换到修改照片模块。"
            )
            send_status = "queued"
        else:
            message = (
                "已写入 Lightroom 预设，但插件尚未注册。请通过增效工具管理器添加 "
                "lightroom/AIPhotoAgent.lrplugin，重启 Lightroom 后重试。"
            )
            send_status = "plugin_not_installed"

        return LightroomSendResponse(
            photo_id=development.photo_id,
            style_id=development.style_id,
            intensity=development.intensity,
            preset_name=development.preset_name,
            status=send_status,
            job_id=job_id,
            preset_path=str(preset_path),
            plugin_installed=plugin_installed,
            lightroom_root=str(locations.root),
            bridge_inbox=str(locations.inbox_dir),
            xmp_url=development.xmp_url,
            message=message,
        )

    def _plugin_registered(self, locations: LightroomLocations) -> bool:
        if (locations.modules_dir / PLUGIN_FOLDER_NAME / "Info.lua").is_file():
            return True
        preferences_dir = locations.root / "Preferences"
        if not preferences_dir.is_dir():
            return False
        source = str(PLUGIN_SOURCE_DIR)
        variants = {
            source,
            source.replace("\\", "\\\\"),
            source.replace("\\", "/"),
        }
        try:
            preference_files = tuple(preferences_dir.glob("*.agprefs"))
        except OSError:
            return False
        for preference_file in preference_files:
            try:
                text = preference_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "AgSdkPluginLoader_installedPluginPaths" in text and any(
                variant in text for variant in variants
            ):
                return True
        return False

    def _write_bridge_hint(self, locations: LightroomLocations) -> None:
        """Tell the plug-in where the bridge lives, independent of OS path rules."""

        # No trailing newline: Lightroom Lua strips the last byte of every line
        # it reads, so a newline-terminated hint loses the final path character.
        content = str(locations.bridge_dir).encode("utf-8")
        # Only the *installed* copy is written: the source tree is version
        # controlled, and a test run with a temporary bridge dir would otherwise
        # overwrite it with a path that only exists until the process exits.
        targets: list[Path] = []
        installed_plugin_dir = locations.modules_dir / PLUGIN_FOLDER_NAME
        if installed_plugin_dir.is_dir():
            targets.append(installed_plugin_dir)
        for target in targets:
            try:
                _atomic_write_bytes(target / "bridge-path.txt", content)
            except OSError:
                # The hint is an optimization; the plug-in still probes the
                # standard Lightroom locations if it cannot be written.
                continue

    def _install_preset(
        self,
        development: DevelopResponse,
        locations: LightroomLocations,
    ) -> Path:
        if development.preset_path is None or not development.preset_path.is_file():
            raise StorageOperationError(
                "The generated Lightroom preset file is missing from storage."
            )
        preset_folder = locations.presets_dir / PRESET_FOLDER_NAME
        preset_folder.mkdir(parents=True, exist_ok=True)
        target_name = _safe_preset_filename(development.preset_name) + ".xmp"
        target_path = preset_folder / target_name
        try:
            content = development.preset_path.read_bytes()
            _atomic_write_bytes(target_path, content)
        except OSError as exc:
            raise StorageOperationError(
                "Unable to install the Lightroom preset into the Lightroom preset folder."
            ) from exc
        return target_path

    def _enqueue_job(
        self,
        development: DevelopResponse,
        locations: LightroomLocations,
        preset_path: Path,
    ) -> str:
        created_at = datetime.now(timezone.utc)
        job_id = f"job_{created_at.strftime('%Y%m%d%H%M%S%f')}_{uuid4().hex[:8]}"
        payload: dict[str, Any] = {
            "schema_version": JOB_SCHEMA_VERSION,
            "job_id": job_id,
            "created_at": created_at.isoformat(),
            "server_url": self.server_url,
            "photo": {
                "photo_id": development.photo_id,
                "style_id": development.style_id,
                "intensity": development.intensity,
                # Basename only (sanitized at upload time); lets the plug-in
                # verify the queued job still targets the selected photo.
                "original_filename": development.original_filename,
            },
            "style": {
                "id": development.style_id,
                "name": development.style_name,
            },
            "preset_name": development.preset_name,
            "preset_path": str(preset_path),
            "mapping_version": development.mapping_version,
            "develop_settings_version": development.develop_settings_version,
            "develop_settings": development.develop_settings,
            "parameters": development.parameters.model_dump(mode="json"),
        }
        target_path = locations.inbox_dir / f"{job_id}.json"
        try:
            _atomic_write_json(target_path, payload)
        except OSError as exc:
            raise StorageOperationError(
                "Unable to queue the job for the Lightroom plugin."
            ) from exc
        return job_id


def _default_lightroom_roots() -> tuple[Path, ...]:
    if os.name == "nt":
        appdata = os.getenv("APPDATA")
        roots: list[Path] = []
        if appdata:
            roots.append(Path(appdata) / "Adobe" / "Lightroom")
        roots.append(Path.home() / "Documents" / "Adobe" / "Lightroom")
        return tuple(roots)
    return (
        Path.home() / "Library" / "Application Support" / "Adobe" / "Lightroom",
        Path.home() / "Documents" / "Adobe" / "Lightroom",
    )


def _safe_preset_filename(preset_name: str) -> str:
    cleaned = _UNSAFE_FILENAME_CHARACTERS.sub("_", preset_name).strip(" .")
    if not cleaned:
        cleaned = "AI Photo Agent Preset"
    return cleaned[:MAX_PRESET_FILENAME_LENGTH].rstrip(" .")


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    _atomic_write_bytes(path, content)
