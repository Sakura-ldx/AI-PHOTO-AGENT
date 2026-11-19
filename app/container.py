from __future__ import annotations

from dataclasses import dataclass

from app.services.analysis_tasks import AnalysisTaskRegistry
from app.services.lightroom_bridge import LightroomBridge
from app.services.storage import FileStorage
from app.services.workflows import LearningWorkflow, PhotoWorkflow


@dataclass(frozen=True, slots=True)
class ServiceContainer:
    storage: FileStorage
    photos: PhotoWorkflow
    learning: LearningWorkflow
    lightroom: LightroomBridge
    analysis_tasks: AnalysisTaskRegistry
