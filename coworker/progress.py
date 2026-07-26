"""Live progress from inside a long-running tool.

Most tools return in well under a second, so one TOOL_STARTED / TOOL_FINISHED pair
describes them completely. A delegated coding task (``tools/claude_code``) runs for
minutes, and a surface that shows nothing until it returns reads as a hang — the user
cannot tell a working delegate from a wedged one. Such a tool publishes progress here and
the turn engine forwards each item as a ``TOOL_PROGRESS`` event on the session's live
stream, alongside every other event the surface already consumes.

The channel is a ContextVar rather than a callback threaded through every tool signature:
tools execute via ``asyncio.to_thread``, which copies the caller's ``contextvars.Context``
into the worker thread, so the engine can bind a per-call sink that the tool picks up
without knowing the engine exists. Outside a turn — a tool called directly, a unit test —
the var is unset and ``report_progress`` is a no-op, so call sites need no guard.

Opt-in is explicit and per-tool (``emits_progress``, read by the engine through
``wants_progress``), following the ``__coworker_schema__`` marker convention in
``tools/shell``. Tools that don't opt in keep the plain execute path, so the ordinary
sub-second tool pays nothing for this.

Progress is display-only. It never enters the model's context — the model sees only the
tool's return value — and a failure to report can never fail the tool's real work.
"""

from __future__ import annotations

import contextvars
from typing import Any, Callable, Optional

ProgressSink = Callable[[dict], None]

# Bound by the turn engine for the duration of one tool call; unset outside a turn.
_sink: contextvars.ContextVar[Optional[ProgressSink]] = contextvars.ContextVar(
    "coworker_progress_sink", default=None
)

# Progress items are broadcast to every socket on the session, so keep them small.
MAX_TEXT_CHARS = 200


def report_progress(**fields: Any) -> None:
    """Publish one progress item from inside a tool. No-op when nothing is listening."""
    sink = _sink.get()
    if sink is None:
        return
    try:
        sink(dict(fields))
    except Exception:
        # Cosmetic by definition: a reporting failure must never break the real work.
        pass


def truncate(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Clip a progress string to a broadcast-friendly length."""
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def bind_sink(sink: Optional[ProgressSink]) -> contextvars.Token:
    """Bind `sink` for the current context; pass the token to `reset_sink` when done."""
    return _sink.set(sink)


def reset_sink(token: contextvars.Token) -> None:
    _sink.reset(token)


def emits_progress(func: Any) -> Any:
    """Mark a tool callable as one whose execution the engine should stream."""
    func.__coworker_emits_progress__ = True
    return func


def wants_progress(func: Any) -> bool:
    return bool(getattr(func, "__coworker_emits_progress__", False))
