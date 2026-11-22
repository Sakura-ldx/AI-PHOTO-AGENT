from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from typing import Protocol
from uuid import uuid4

from app.errors import (
    AnalysisCancelled,
    ProviderOutputError,
    ProviderServiceError,
    ProviderTimeoutError,
)
from app.schemas import AnalysisTaskStage, AnalysisTaskStatus, ImageAnalysis

LOGGER = logging.getLogger(__name__)

ANALYSIS_TASK_VERSION = "analysis_task_v1"
ANALYSIS_TASK_RETENTION_SECONDS = 3600.0
# Cancelling does not abort an HTTP request that is already in flight, so a
# capped thread pool would let one abandoned analysis block the next one. Each
# task therefore gets its own daemon thread. Two limits keep that bounded: the
# runner cap governs how many non-cancelled analyses may run at once, and the
# in-flight cap counts every live thread (including cancelled analyses that are
# still draining a provider call), so repeated submit+cancel cannot grow the
# thread count without bound.
ANALYSIS_TASK_MAX_RUNNERS = 8
ANALYSIS_TASK_MAX_IN_FLIGHT = ANALYSIS_TASK_MAX_RUNNERS * 2
ACTIVE_STAGES: frozenset[AnalysisTaskStage] = frozenset(
    {"queued", "measuring", "provider", "validating"}
)

ProgressReporter = Callable[[AnalysisTaskStage, str], None]
CancellationCheck = Callable[[], bool]
TaskRunner = Callable[[ProgressReporter, CancellationCheck], ImageAnalysis]


class AnalysisWorkflow(Protocol):
    """The slice of ``PhotoWorkflow`` the task registry depends on."""

    def analyze_with_progress(
        self,
        photo_id: str,
        provider_name: str = "default",
        *,
        progress: ProgressReporter | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> ImageAnalysis: ...


_BUSY_MESSAGE = "当前分析任务过多，请等待现有任务结束后重试。"

_FAILURE_MESSAGES: dict[type[Exception], str] = {
    ProviderTimeoutError: "分析超时，请确认本地服务与网络正常后重试。",
    ProviderServiceError: "模型服务返回错误，请稍后重试。",
    ProviderOutputError: (
        "模型返回的内容不完整（多半是输出被长度限制截断），已自动重试过一次；"
        "仍失败请再点一次分析，或换一张更简单的照片试试。"
    ),
}


@dataclass(slots=True)
class AnalysisTask:
    photo_id: str
    started_at: float
    stage: AnalysisTaskStage = "queued"
    stage_started_at: float = 0.0
    progress_hint: str = "已排队，等待开始分析"
    analysis: ImageAnalysis | None = None
    error: str | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    cancel_event: Event = field(default_factory=Event, repr=False)
    slot_released: bool = field(default=False, repr=False)
    in_flight_released: bool = field(default=False, repr=False)
    task_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if self.stage_started_at == 0.0:
            self.stage_started_at = self.started_at

    @property
    def is_active(self) -> bool:
        return self.stage in ACTIVE_STAGES

    def status(self, *, now: float | None = None) -> AnalysisTaskStatus:
        reference = now if now is not None else time.monotonic()
        elapsed = max(0.0, reference - self.started_at)
        return AnalysisTaskStatus(
            task_id=self.task_id,
            photo_id=self.photo_id,
            stage=self.stage,
            elapsed_seconds=round(elapsed, 3),
            progress_hint=self.progress_hint,
            analysis=self.analysis if self.stage == "succeeded" else None,
            error=self.error,
        )


class AnalysisTaskRegistry:
    """Run photo analysis in the background so clients can poll progress.

    Lightroom cannot show progress for a single blocking HTTP request, so the
    plug-in creates a task, then polls its stage while the analysis runs in a
    background thread. Results still land in the same photo session that the
    synchronous endpoint writes, so both paths stay interchangeable.

    Cancelling releases the concurrency slot immediately. The abandoned request
    may keep running until its own timeout, but its result is discarded and it
    never holds up the next analysis. Its thread still occupies an admission
    slot until it actually exits, which is what bounds the total thread count.
    """

    def __init__(
        self,
        *,
        max_workers: int = ANALYSIS_TASK_MAX_RUNNERS,
        retention_seconds: float = ANALYSIS_TASK_RETENTION_SECONDS,
        max_in_flight: int | None = None,
    ) -> None:
        self._lock = Lock()
        self._tasks: dict[str, AnalysisTask] = {}
        self._max_runners = max(1, max_workers)
        self._max_in_flight = max(
            self._max_runners,
            max_in_flight if max_in_flight is not None else ANALYSIS_TASK_MAX_IN_FLIGHT,
        )
        self._running = 0
        self._in_flight = 0
        self._retention_seconds = retention_seconds
        self._closed = False

    @property
    def in_flight(self) -> int:
        """Number of analysis threads that have not exited yet, cancelled included."""

        with self._lock:
            return self._in_flight

    def submit(self, photo_id: str, runner: TaskRunner) -> AnalysisTask:
        self._reap_expired()
        task = AnalysisTask(photo_id=photo_id, started_at=time.monotonic())
        with self._lock:
            self._tasks[task.task_id] = task
        if not self._reserve_slot(task):
            return task
        thread = Thread(
            target=self._run,
            args=(task, runner),
            name=f"aipa-analysis-{task.task_id[:8]}",
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError as exc:
            # The OS refused a new thread (for example because the process is at
            # its thread limit). Surface it as a failed task instead of leaking
            # a reserved slot or letting the exception escape to the API layer.
            LOGGER.warning(
                "Could not start photo analysis thread photo_id=%s task_id=%s error=%s",
                task.photo_id,
                task.task_id,
                exc,
            )
            with self._lock:
                task.stage = "failed"
                task.progress_hint = "分析失败"
                task.error = _failure_message(exc)
                task.finished_at = time.monotonic()
            self._release_slot(task)
            self._release_in_flight(task)
        return task

    def get(self, task_id: str) -> AnalysisTask | None:
        self._reap_expired()
        with self._lock:
            return self._tasks.get(task_id)

    def status(self, task_id: str) -> AnalysisTaskStatus | None:
        # Snapshot under the lock so a poll never observes a half-written stage
        # or an analysis from a task that has not been marked succeeded yet.
        self._reap_expired()
        with self._lock:
            task = self._tasks.get(task_id)
            return None if task is None else task.status()

    def cancel(self, task_id: str) -> AnalysisTaskStatus | None:
        task = self.get(task_id)
        if task is None:
            return None
        with self._lock:
            if not task.is_active:
                return task.status()
            task.cancel_requested = True
            task.cancel_event.set()
            task.stage = "cancelled"
            task.progress_hint = "已取消"
            task.error = None
            task.finished_at = time.monotonic()
        self._release_slot(task)
        return task.status()

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = list(self._tasks.values())
            now = time.monotonic()
            cancelled: list[AnalysisTask] = []
            for task in tasks:
                if not task.is_active:
                    continue
                task.cancel_requested = True
                task.cancel_event.set()
                task.stage = "cancelled"
                task.progress_hint = "已取消"
                task.error = None
                task.finished_at = now
                cancelled.append(task)
        for task in cancelled:
            self._release_slot(task)

    def _reserve_slot(self, task: AnalysisTask) -> bool:
        with self._lock:
            running = self._running
            in_flight = self._in_flight
            if self._closed or running >= self._max_runners or in_flight >= self._max_in_flight:
                busy = True
            else:
                busy = False
                self._running = running + 1
                self._in_flight = in_flight + 1
        if not busy:
            return True
        task.stage = "failed"
        task.progress_hint = "分析失败"
        task.error = _BUSY_MESSAGE
        task.finished_at = time.monotonic()
        LOGGER.warning(
            "Refused photo analysis task photo_id=%s task_id=%s: %d runners, %d in flight",
            task.photo_id,
            task.task_id,
            running,
            in_flight,
        )
        return False

    def _release_slot(self, task: AnalysisTask) -> None:
        with self._lock:
            if task.slot_released:
                return
            task.slot_released = True
            self._running = max(0, self._running - 1)

    def _release_in_flight(self, task: AnalysisTask) -> None:
        with self._lock:
            if task.in_flight_released:
                return
            task.in_flight_released = True
            self._in_flight = max(0, self._in_flight - 1)

    def _run(self, task: AnalysisTask, runner: TaskRunner) -> None:
        def report(stage: AnalysisTaskStage, hint: str) -> None:
            with self._lock:
                if task.cancel_requested:
                    return
                task.stage = stage
                task.progress_hint = hint
                task.stage_started_at = time.monotonic()

        def cancelled() -> bool:
            return task.cancel_event.is_set()

        try:
            analysis = runner(report, cancelled)
        except AnalysisCancelled:
            with self._lock:
                task.stage = "cancelled"
                task.progress_hint = "已取消"
                task.error = None
        except Exception as exc:  # noqa: BLE001 - surfaced as task state
            error = _failure_message(exc)
            with self._lock:
                task.error = error
                if not task.cancel_requested:
                    task.stage = "failed"
                    task.progress_hint = "分析失败"
            LOGGER.warning(
                "Photo analysis task failed photo_id=%s task_id=%s error=%s",
                task.photo_id,
                task.task_id,
                exc,
            )
        else:
            with self._lock:
                task.analysis = analysis
                if not task.cancel_requested:
                    task.stage = "succeeded"
                    task.progress_hint = "分析完成"
                task.error = None
        finally:
            with self._lock:
                task.finished_at = time.monotonic()
            self._release_slot(task)
            self._release_in_flight(task)

    def _reap_expired(self) -> None:
        if self._retention_seconds <= 0:
            return
        cutoff = time.monotonic() - self._retention_seconds
        with self._lock:
            expired = [
                task_id
                for task_id, task in self._tasks.items()
                if task.finished_at is not None and task.finished_at < cutoff
            ]
            for task_id in expired:
                self._tasks.pop(task_id, None)


def _failure_message(exc: Exception) -> str:
    for error_type, message in _FAILURE_MESSAGES.items():
        if isinstance(exc, error_type):
            return message
    return "分析失败，请稍后重试。"


def submit_analysis(
    registry: AnalysisTaskRegistry,
    workflow: AnalysisWorkflow,
    photo_id: str,
    provider_name: str = "default",
) -> AnalysisTask:
    """Queue ``PhotoWorkflow.analyze_with_progress`` on the registry."""

    def runner(report: ProgressReporter, cancelled: CancellationCheck) -> ImageAnalysis:
        return workflow.analyze_with_progress(
            photo_id,
            provider_name,
            progress=report,
            cancelled=cancelled,
        )

    return registry.submit(photo_id, runner)
