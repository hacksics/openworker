"""Delegate implementation work to the Claude Code CLI.

OpenWorker's own edit tools make roughly one change per model round-trip, which spends
the session's context on the mechanics of a feature rather than on coordinating it. The
Developer persona instead hands a whole task to Claude Code — an agent that explores,
edits, runs the tests, and iterates in its *own* context — and gets back a bounded
report. The session keeps its context for the conversation with the user.

The ``CodingAgent`` boundary is the same hedge ``Executor`` is in ``tools/shell.py``.
Today the only implementation shells out to the ``claude`` CLI, which exposes no hook for
approving individual tool calls, so one OpenWorker approval necessarily covers the whole
delegated run. A future ``ClaudeCodeSDK`` built on ``claude-agent-sdk``'s ``can_use_tool``
callback can route each of Claude Code's decisions back through ``PermissionEngine`` and
the Inbox — answerable from Slack — without the tool, the catalog entry, or the persona
manifest changing.

Risk: ``delegate_coding_task`` is classified EXEC, so plan/discuss mode block it outright
and interactive mode requires approval. That approval is coarse *by construction*: the
subprocess writes files and runs commands under its own policy and OpenWorker cannot gate
each one. Two things bound the blast radius — it runs with ``--permission-mode
acceptEdits`` and never ``bypassPermissions``, and it is scoped to the session's granted
roots via ``cwd`` plus ``--add-dir``. Approving a delegation is closer to approving a
build script than to approving a single write; the persona prompt says so too.

The Anthropic key is injected into the child environment only when one resolves, because
the CLI may already hold its own credentials from ``claude auth login``. It is never
echoed into the returned dict, which the model sees (secrets stay out of context).

Session continuity matters here: a delegate that forgot everything between turns would
re-explore the repo on every follow-up. The CLI's own ``session_id`` is captured from the
event stream and replayed with ``--resume``, so "now add tests for that" continues the
same Claude Code session. The instance lives as long as the engine that owns it, which is
one per OpenWorker session.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

import aisuite as ai

from ..progress import emits_progress, report_progress, truncate

_IS_WINDOWS = sys.platform == "win32"

# A delegated task is a whole feature, not one command, so the persistent shell's 600s
# ceiling is far too low. Still capped: a wedged child must not hold the turn forever.
_DEFAULT_TIMEOUT = 1800.0
_MAX_TIMEOUT = 3600.0

# The report is a summary, not a transcript — context saving is the entire point of
# delegating, so what comes back to the model stays bounded.
_MAX_RESULT_CHARS = 20_000

# Claude Code tool names that name a file they changed, used to report files touched.
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}

# The CLI's own `--permission-mode` choices (verified against 2.1.x) MINUS
# `bypassPermissions`: a mode that skips every prompt would hand the child more authority
# than the user granted OpenWorker itself, so it is not constructible here. `acceptEdits`
# auto-approves edits and common filesystem commands; `plan` makes the delegate read-only.
VALID_PERMISSION_MODES = {"plan", "acceptEdits", "auto", "manual", "dontAsk"}


class CodingAgent(ABC):
    """A delegated coding agent: hand it a task, get back a report.

    Implementations own their own permission policy — the caller has already obtained a
    single OpenWorker approval for the delegation as a whole.
    """

    @abstractmethod
    def run(self, task: str, *, timeout: Optional[float] = None) -> dict[str, Any]:
        """Run `task` to completion and return a bounded, model-safe report."""

    def interrupt(self) -> None:  # pragma: no cover - default no-op
        """Stop an in-flight run (wired to the engine's interrupt hooks)."""


class ClaudeCodeCLI(CodingAgent):
    """`CodingAgent` backed by the `claude` CLI in headless (`-p`) mode.

    Reads the `stream-json` event stream rather than plain text so the report can name
    the tools the delegate ran, the files it changed, and what it cost — and so the
    session id survives even a run that times out.
    """

    def __init__(
        self,
        *,
        workspace: str | Path,
        roots: Optional[list] = None,
        secrets: Optional[Any] = None,
        binary: str = "claude",
        permission_mode: str = "acceptEdits",
        model: Optional[str] = None,
        default_timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.workspace = str(Path(workspace).expanduser().resolve())
        if permission_mode not in VALID_PERMISSION_MODES:
            raise ValueError(f"unsupported permission mode: {permission_mode!r}")
        self._permission_mode = permission_mode
        self._binary = binary
        self._model = model
        self._secrets = secrets
        self._default_timeout = default_timeout
        # Additional granted roots beyond the workspace, so a delegate can read a folder
        # the user explicitly added. The workspace itself arrives as cwd.
        self._extra_dirs = [
            p
            for p in (str(getattr(r, "path", r)) for r in (roots or []))
            if p != self.workspace
        ]
        self._session_id: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # -- environment ------------------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = {**os.environ}
        # Only set a key if one resolves: the CLI may already be authenticated via
        # `claude auth login`, and an empty ANTHROPIC_API_KEY would break that.
        try:
            from ..providers.anthropic_provider import resolve_api_key

            key = resolve_api_key(self._secrets)
        except Exception:  # pragma: no cover - key resolution must never block a run
            key = None
        if key:
            env["ANTHROPIC_API_KEY"] = key
        return env

    def _argv(self, task: str) -> list[str]:
        argv = [
            self._binary,
            "-p",
            task,
            "--output-format",
            "stream-json",
            # stream-json in print mode needs --verbose to emit the full event stream
            # (tool_use blocks), which is what the report is built from.
            "--verbose",
            "--permission-mode",
            self._permission_mode,
        ]
        if self._session_id:
            argv += ["--resume", self._session_id]
        if self._model:
            argv += ["--model", self._model]
        for extra in self._extra_dirs:
            argv += ["--add-dir", extra]
        return argv

    # -- run --------------------------------------------------------------------
    def run(self, task: str, *, timeout: Optional[float] = None) -> dict[str, Any]:
        if not task.strip():
            return {"error": "task must not be empty"}
        if shutil.which(self._binary) is None:
            return {
                "error": (
                    f"the Claude Code CLI ({self._binary!r}) is not on PATH. Install it "
                    "with `npm install -g @anthropic-ai/claude-code`, then check "
                    "`claude --version`."
                )
            }
        limit = min(float(timeout or self._default_timeout), _MAX_TIMEOUT)
        spawn_kwargs: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if _IS_WINDOWS
            else {"start_new_session": True}
        )
        try:
            proc = subprocess.Popen(
                self._argv(task),
                cwd=self.workspace,
                stdin=subprocess.DEVNULL,  # headless: never block waiting on input
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=self._env(),
                **spawn_kwargs,
            )
        except OSError as exc:
            return {"error": f"failed to start Claude Code: {exc}"}

        with self._lock:
            self._proc = proc
        try:
            events, stderr, timed_out = self._drain(proc, limit)
        finally:
            with self._lock:
                self._proc = None
        return self._report(events, stderr, timed_out, proc.returncode)

    def _drain(
        self, proc: subprocess.Popen, limit: float
    ) -> tuple[list[dict], str, bool]:
        """Collect parsed stdout events until the child exits or the deadline passes.

        stdout and stderr get their own reader threads: draining only one while the other
        fills its pipe buffer would deadlock a chatty run.
        """
        events: queue.Queue = queue.Queue()
        errs: list[str] = []

        def read_stdout() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    events.put(line)
            finally:
                events.put(None)  # EOF sentinel

        def read_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                errs.append(line)

        threading.Thread(target=read_stdout, daemon=True).start()
        threading.Thread(target=read_stderr, daemon=True).start()

        parsed: list[dict] = []
        deadline = time.monotonic() + limit
        timed_out = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                self.interrupt()
                break
            try:
                line = events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue  # progress noise or a partial line — not fatal
            if isinstance(event, dict):
                parsed.append(event)
                # Live view: forward what the delegate is doing while it's still running.
                for item in progress_items(event):
                    report_progress(**item)
        try:
            proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return parsed, "".join(errs), timed_out

    def interrupt(self) -> None:
        """Terminate an in-flight delegation. Claude Code exits 143 on SIGTERM, aborting
        its turn and reaping the command tree it spawned."""
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        if _IS_WINDOWS:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    # -- report -----------------------------------------------------------------
    def _report(
        self,
        events: list[dict],
        stderr: str,
        timed_out: bool,
        exit_code: Optional[int],
    ) -> dict[str, Any]:
        summary = summarize_events(events)
        # Capture the session id even from a failed or timed-out run so the next
        # delegation resumes instead of re-exploring the repo from scratch.
        if summary.get("session_id"):
            self._session_id = summary["session_id"]

        report: dict[str, Any] = {"exit_code": exit_code}
        if summary["tools"]:
            report["tools_used"] = summary["tools"]
        if summary["files_changed"]:
            report["files_changed"] = summary["files_changed"]
        if summary["cost_usd"] is not None:
            report["cost_usd"] = summary["cost_usd"]

        text = summary["result"]
        if len(text) > _MAX_RESULT_CHARS:
            # Keep the tail: a delegate's verdict and next steps land at the end.
            text = text[-_MAX_RESULT_CHARS:]
            report["truncated"] = True
        if text:
            report["report"] = text

        if timed_out:
            report["error"] = (
                "the delegated task was interrupted after hitting its time limit; the "
                "work so far is on disk. Check `git_diff`, then delegate a narrower "
                "follow-up (it resumes the same session)."
            )
        elif summary["is_error"] or (exit_code not in (0, None)):
            report["error"] = (
                summary["result"] or stderr.strip()[-2000:] or "Claude Code failed"
            )
            report.pop("report", None)
        elif not text:
            report["error"] = "Claude Code produced no report"
        return report


def progress_items(event: dict) -> list[dict[str, Any]]:
    """The progress items one `stream-json` event is worth, for the live view.

    Deliberately lossy: the delegate's own narration and the tools it runs are what tell a
    watching user it's making headway. Raw tool inputs are not forwarded — a `target` is a
    file path or a short command, never a file's contents.
    """
    kind = event.get("type")
    if kind != "assistant":
        return []
    items: list[dict[str, Any]] = []
    for block in (event.get("message") or {}).get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = truncate(block.get("text") or "")
            if text:
                items.append({"kind": "narration", "text": text})
        elif block.get("type") == "tool_use":
            data = block.get("input") or {}
            target = data.get("file_path") or data.get("command") or data.get("pattern")
            items.append(
                {
                    "kind": "tool",
                    "tool": str(block.get("name") or "?"),
                    **({"target": truncate(str(target), 120)} if target else {}),
                }
            )
    return items


def summarize_events(events: list[dict]) -> dict[str, Any]:
    """Fold a `stream-json` event list into the fields the report is built from.

    Split out from the class so it is directly testable against recorded streams.
    """
    tools: dict[str, int] = {}
    files: list[str] = []
    session_id: Optional[str] = None
    result, cost, usage, is_error = "", None, None, False

    for event in events:
        sid = event.get("session_id")
        if isinstance(sid, str) and sid:
            session_id = sid
        kind = event.get("type")
        if kind == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "?")
                tools[name] = tools.get(name, 0) + 1
                if name in _EDIT_TOOLS:
                    path = (block.get("input") or {}).get("file_path")
                    if isinstance(path, str) and path and path not in files:
                        files.append(path)
        elif kind == "result":
            result = str(event.get("result") or "")
            cost = event.get("total_cost_usd")
            usage = event.get("usage")
            is_error = bool(event.get("is_error"))

    return {
        "tools": tools,
        "files_changed": files,
        "session_id": session_id,
        "result": result,
        "cost_usd": cost,
        "usage": usage,
        "is_error": is_error,
    }


_DELEGATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_coding_task",
        "description": (
            "Delegate an implementation task to Claude Code — a coding agent that "
            "explores the repo, edits files, runs tests, and iterates in its own "
            "context, then reports back. Use it for anything that changes code. State "
            "the outcome you want plus the constraints (which files, how to verify); it "
            "cannot see your conversation, so the task must stand alone. Follow-up "
            "calls resume the same Claude Code session, so you can say 'now add tests "
            "for that'. It writes files and runs commands in the workspace, so one "
            "approval covers the whole run — batch a coherent unit of work per call "
            "rather than one file at a time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "The self-contained task: the goal, the relevant files or "
                        "modules, any conventions to follow, and how to verify it."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        f"Max seconds to wait (default {int(_DEFAULT_TIMEOUT)}, max "
                        f"{int(_MAX_TIMEOUT)}). Raise it for large features."
                    ),
                },
            },
            "required": ["task"],
        },
    },
}


def claude_code_tools(agent: CodingAgent) -> list:
    """Return the delegation tool bound to a `CodingAgent`."""

    def delegate_coding_task(
        task: str, timeout_seconds: Optional[int] = None
    ) -> dict:
        timeout = None
        if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0:
            timeout = min(float(timeout_seconds), _MAX_TIMEOUT)
        return agent.run(task, timeout=timeout)

    wrapped = ai.tool(
        delegate_coding_task,
        metadata=ai.ToolMetadata(
            category="shell",
            risk_level="high",
            capabilities=["run_command"],
            requires_approval=True,
        ),
    )
    wrapped.__coworker_schema__ = _DELEGATE_SCHEMA
    # Minutes-long by nature: tell the engine to stream this call's progress rather than
    # going silent between TOOL_STARTED and TOOL_FINISHED.
    emits_progress(wrapped)
    return [wrapped]
