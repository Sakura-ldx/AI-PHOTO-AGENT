from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from app.adapters.analysis import MockAnalysisProvider
from app.errors import AnalysisCancelled, ProviderTimeoutError
from app.schemas import AnalysisTaskStatus, ImageAnalysis, ImageMetadata
from app.services.analysis_tasks import (
    ACTIVE_STAGES,
    AnalysisTask,
    AnalysisTaskRegistry,
    submit_analysis,
)


def make_analysis() -> ImageAnalysis:
    metadata = ImageMetadata(
        width=128,
        height=96,
        format="JPEG",
        color_mode="RGB",
        file_size=1024,
        content_type="image/jpeg",
    )
    return MockAnalysisProvider().analyze(Path("unused.jpg"), metadata)


def wait_for(registry: AnalysisTaskRegistry, task_id: str, timeout: float = 5.0) -> AnalysisTaskStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = registry.status(task_id)
        if status is not None and status.stage not in ACTIVE_STAGES:
            return status
        time.sleep(0.01)
    raise AssertionError(f"task {task_id} did not finish within {timeout}s")


def test_registry_reports_stages_in_order_and_keeps_result():
    registry = AnalysisTaskRegistry(max_workers=1)
    analysis = make_analysis()
    seen: list[str] = []
    release = threading.Event()

    def runner(report, cancelled):
        for stage in ("measuring", "provider"):
            report(stage, f"阶段 {stage}")
            seen.append(stage)
        release.wait(2.0)
        report("validating", "正在校验并保存修图建议")
        seen.append("validating")
        return analysis

    task = registry.submit("photo-1", runner)
    release.wait(2.0)
    assert seen[:2] == ["measuring", "provider"]
    release.set()
    status = wait_for(registry, task.task_id)
    assert status.stage == "succeeded"
    assert status.analysis is not None
    assert status.error is None
    assert status.progress_hint == "分析完成"
    assert seen == ["measuring", "provider", "validating"]
    registry.shutdown()


def test_registry_maps_known_errors_to_chinese_messages():
    registry = AnalysisTaskRegistry(max_workers=1)

    def runner(report, cancelled):
        raise ProviderTimeoutError("boom")

    task = registry.submit("photo-1", runner)
    status = wait_for(registry, task.task_id)
    assert status.stage == "failed"
    assert status.analysis is None
    assert status.error == "分析超时，请确认本地服务与网络正常后重试。"
    registry.shutdown()


def test_registry_maps_unknown_errors_to_generic_message():
    registry = AnalysisTaskRegistry(max_workers=1)

    def runner(report, cancelled):
        raise RuntimeError("unexpected")

    task = registry.submit("photo-1", runner)
    status = wait_for(registry, task.task_id)
    assert status.stage == "failed"
    assert status.error == "分析失败，请稍后重试。"
    registry.shutdown()


def test_cancel_marks_active_task_and_ignores_late_result():
    registry = AnalysisTaskRegistry(max_workers=1)
    analysis = make_analysis()
    release = threading.Event()

    def runner(report, cancelled):
        report("provider", "正在调用模型分析照片")
        release.wait(2.0)
        return analysis

    task = registry.submit("photo-1", runner)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        status = registry.status(task.task_id)
        if status is not None and status.stage == "provider":
            break
        time.sleep(0.01)
    cancelled = registry.cancel(task.task_id)
    assert cancelled is not None
    assert cancelled.stage == "cancelled"
    assert cancelled.progress_hint == "已取消"
    release.set()
    time.sleep(0.2)
    status = registry.status(task.task_id)
    assert status.stage == "cancelled"
    assert status.analysis is None
    registry.shutdown()


def test_cancel_finished_task_is_a_noop():
    registry = AnalysisTaskRegistry(max_workers=1)
    analysis = make_analysis()
    task = registry.submit("photo-1", lambda report, cancelled: analysis)
    wait_for(registry, task.task_id)
    status = registry.cancel(task.task_id)
    assert status is not None
    assert status.stage == "succeeded"
    assert status.analysis is not None
    registry.shutdown()


def test_unknown_task_returns_none():
    registry = AnalysisTaskRegistry(max_workers=1)
    assert registry.get("missing") is None
    assert registry.status("missing") is None
    assert registry.cancel("missing") is None
    registry.shutdown()


def test_expired_tasks_are_reaped():
    registry = AnalysisTaskRegistry(max_workers=1, retention_seconds=0.01)
    task = registry.submit("photo-1", lambda report, cancelled: make_analysis())
    wait_for(registry, task.task_id)
    time.sleep(0.05)
    registry._reap_expired()
    assert registry.get(task.task_id) is None
    registry.shutdown()


def test_task_status_hides_result_until_success():
    task = AnalysisTask(photo_id="photo-1", started_at=time.monotonic())
    status = task.status()
    assert isinstance(status, AnalysisTaskStatus)
    assert status.stage == "queued"
    assert status.analysis is None
    assert status.elapsed_seconds >= 0.0


def test_submit_analysis_uses_workflow_progress_hook():
    registry = AnalysisTaskRegistry(max_workers=1)
    analysis = make_analysis()
    calls: list[tuple[str, str]] = []

    class FakeWorkflow:
        def analyze_with_progress(self, photo_id, provider_name, *, progress=None, cancelled=None):
            calls.append((photo_id, provider_name))
            assert progress is not None
            progress("provider", "正在调用模型分析照片")
            return analysis

    task = submit_analysis(registry, FakeWorkflow(), "photo-9", "deepseek")
    status = wait_for(registry, task.task_id)
    assert status.stage == "succeeded"
    assert calls == [("photo-9", "deepseek")]
    registry.shutdown()


def test_progress_callback_failure_does_not_break_analysis(tmp_path):
    import asyncio
    from io import BytesIO

    from app.services.storage import FileStorage
    from app.services.workflows import PhotoWorkflow

    storage = FileStorage(tmp_path)
    buffer = BytesIO()
    Image.new("RGB", (64, 48), color=(120, 140, 160)).save(buffer, format="JPEG")

    class Upload:
        filename = "sample.jpg"
        content_type = "image/jpeg"

        def __init__(self) -> None:
            self._consumed = False

        async def read(self, size: int = -1) -> bytes:
            del size
            if self._consumed:
                return b""
            self._consumed = True
            return buffer.getvalue()

    session = asyncio.run(storage.save_upload(Upload()))

    def exploding(_stage, _hint):
        raise RuntimeError("client went away")

    workflow = PhotoWorkflow(storage=storage, analysis_provider=MockAnalysisProvider())
    result = workflow.analyze_with_progress(session.id, "default", progress=exploding)
    assert result.recommended_styles
    assert storage.load_session(session.id).analysis is not None


def test_cancel_releases_the_slot_so_the_next_analysis_starts():
    registry = AnalysisTaskRegistry(max_workers=1)
    analysis = make_analysis()
    blocked = threading.Event()
    started = threading.Event()

    def slow(report, cancelled):
        started.set()
        blocked.wait(5.0)
        return analysis

    first = registry.submit("photo-1", slow)
    assert started.wait(2.0), "slow runner never started"

    cancelled = registry.cancel(first.task_id)
    assert cancelled is not None
    assert cancelled.stage == "cancelled"

    second = registry.submit("photo-2", lambda report, cancelled: analysis)
    status = wait_for(registry, second.task_id, timeout=2.0)
    assert status.stage == "succeeded"

    blocked.set()
    registry.shutdown()


def test_cancelled_runner_sees_the_request_and_leaves_result_empty():
    registry = AnalysisTaskRegistry(max_workers=1)
    noticed: list[bool] = []
    started = threading.Event()

    def cooperative(report, cancelled):
        report("provider", "正在调用模型分析照片")
        started.set()
        while not cancelled():
            time.sleep(0.01)
        noticed.append(True)
        raise AnalysisCancelled("cancelled")

    task = registry.submit("photo-1", cooperative)
    assert started.wait(2.0), "runner never started"
    registry.cancel(task.task_id)

    deadline = time.monotonic() + 2.0
    while not noticed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert noticed == [True]

    status = registry.status(task.task_id)
    assert status.stage == "cancelled"
    assert status.analysis is None
    registry.shutdown()


def test_shutdown_marks_active_tasks_cancelled():
    registry = AnalysisTaskRegistry(max_workers=1)
    started = threading.Event()
    release = threading.Event()

    def slow(report, cancelled):
        report("provider", "正在调用模型分析照片")
        started.set()
        release.wait(2.0)
        return make_analysis()

    task = registry.submit("photo-1", slow)
    assert started.wait(2.0), "runner never started"

    registry.shutdown()
    status = registry.status(task.task_id)
    assert status is not None
    assert status.stage == "cancelled"
    assert status.progress_hint == "已取消"
    assert status.error is None

    # A late result from the abandoned thread must not resurrect the task.
    release.set()
    deadline = time.monotonic() + 2.0
    while registry.in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert registry.status(task.task_id).stage == "cancelled"


def test_runner_cap_fails_fast_instead_of_queueing():
    registry = AnalysisTaskRegistry(max_workers=1)
    blocked = threading.Event()
    started = threading.Event()

    def slow(report, cancelled):
        started.set()
        blocked.wait(5.0)
        return make_analysis()

    first = registry.submit("photo-1", slow)
    assert started.wait(2.0), "slow runner never started"
    second = registry.submit("photo-2", lambda report, cancelled: make_analysis())

    status = registry.status(second.task_id)
    assert status.stage == "failed"
    assert status.error is not None
    assert "过多" in status.error
    assert first.task_id != second.task_id

    blocked.set()
    registry.shutdown()


def test_cancelled_analysis_never_touches_the_session(tmp_path):
    import asyncio
    from io import BytesIO

    from app.services.storage import FileStorage
    from app.services.workflows import PhotoWorkflow

    storage = FileStorage(tmp_path)
    buffer = BytesIO()
    Image.new("RGB", (64, 48), color=(120, 140, 160)).save(buffer, format="JPEG")

    class Upload:
        filename = "sample.jpg"
        content_type = "image/jpeg"

        def __init__(self) -> None:
            self._consumed = False

        async def read(self, size: int = -1) -> bytes:
            del size
            if self._consumed:
                return b""
            self._consumed = True
            return buffer.getvalue()

    session = asyncio.run(storage.save_upload(Upload()))
    provider_calls: list[Path] = []

    class TrackingProvider(MockAnalysisProvider):
        def analyze(self, image_path, metadata, *, consistency_context=""):
            provider_calls.append(image_path)
            return super().analyze(
                image_path, metadata, consistency_context=consistency_context
            )

    workflow = PhotoWorkflow(storage=storage, analysis_provider=TrackingProvider())
    with pytest.raises(AnalysisCancelled):
        workflow.analyze_with_progress(session.id, "default", cancelled=lambda: True)

    assert provider_calls == []
    assert storage.load_session(session.id).analysis is None
