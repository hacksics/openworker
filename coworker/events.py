"""Event model — the contract between the turn engine and any surface (TUI/GUI/IDE).

Granularity is per-token for model output (`assistant_delta`, `reasoning_delta`) and
per-tool otherwise: one `tool_started`/`tool_finished` pair per call. The exception is
`tool_progress`, for tools that run long enough that silence would read as a hang — it
carries display-only detail from inside a call that hasn't returned yet.

Surfaces must tolerate unknown event types: adding one here is routine, and the server
broadcasts every event generically rather than enumerating them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    TURN_START = "turn_start"
    ASSISTANT_DELTA = "assistant_delta"
    REASONING_DELTA = "reasoning_delta"  # model thinking text (display-only, never replayed)
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_PROPOSED = "tool_proposed"
    PERMISSION_REQUIRED = "permission_required"
    DIRECTORY_REQUESTED = "directory_requested"  # agent asks the user to grant a folder
    QUESTION_REQUESTED = (
        "question_requested"  # agent asks the user a free-text/multiple-choice question
    )
    PLAN_PROPOSED = (
        "plan_proposed"  # agent presents a plan for approval (plan mode exit)
    )
    TOOL_STARTED = "tool_started"
    # Display-only progress from inside a still-running tool (see coworker.progress).
    # Emitted only for tools that opt in; a minutes-long delegated task would otherwise
    # look wedged between TOOL_STARTED and TOOL_FINISHED.
    TOOL_PROGRESS = "tool_progress"
    TOOL_FINISHED = "tool_finished"
    ITERATION_END = "iteration_end"
    TURN_END = "turn_end"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
