"""Desktop shell: the local single-user application service and its view-models.

``DesktopService`` is the local loopback boundary a desktop UI binds to. It exposes
task entry and guided questions, route preview, confirmation/approval/incident
inboxes, timeline/cancellation/audit, provider connection management over the
credential vault, worker health with trusted-vs-untrusted labels, and offline data
export/deletion — all as secret-free, serializable views. The shell cannot execute
arbitrary code or bypass backend policy; ``confirm_assembly`` — the one execution
door — runs only previewed marketplace contracts on backend-configured executors
and refuses reserved actions. See ``docs/ADAPTER_MATURITY.md``.
"""

from .loopback import DesktopLoopbackApp
from .service import DesktopService
from .views import (
    ActionView,
    AssemblyPayoutView,
    AssemblyPreviewView,
    AssemblyRunStepView,
    AssemblyRunView,
    AssemblyStepView,
    AuditEntryView,
    AuditView,
    BlueprintView,
    ExecutionLabel,
    ExportBundle,
    InboxItem,
    ProviderConnectionView,
    QuestionView,
    RoutePreview,
    TaskView,
    TimelineEvent,
    WorkerHealthView,
)

__all__ = [
    "ActionView",
    "AssemblyPayoutView",
    "AssemblyPreviewView",
    "AssemblyRunStepView",
    "AssemblyRunView",
    "AssemblyStepView",
    "AuditEntryView",
    "AuditView",
    "BlueprintView",
    "DesktopLoopbackApp",
    "DesktopService",
    "ExecutionLabel",
    "ExportBundle",
    "InboxItem",
    "ProviderConnectionView",
    "QuestionView",
    "RoutePreview",
    "TaskView",
    "TimelineEvent",
    "WorkerHealthView",
]
