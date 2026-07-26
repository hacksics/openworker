"""Delegating coding tasks to the Claude Code CLI.

Covers the report folding (pure, against a recorded `stream-json` stream), the subprocess
path against a fake `claude` on PATH (no network, no real CLI, no model spend), session
resumption, the fail-closed paths (missing binary, error result, empty task), and the two
properties that make the Developer persona safe: the delegation is EXEC-gated, and the
persona has no write tool of its own.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time

import pytest

from coworker.agents.base import AgentContext
from coworker.catalog import CATALOG, capability, expand
from coworker.engine import TurnEngine
from coworker.events import EventType
from coworker.permissions import Mode, PermissionEngine
from coworker.personas.registry import PersonaRegistry
from coworker.progress import emits_progress, report_progress, wants_progress
from coworker.providers import (
    AssistantTurn,
    ModelCapabilities,
    ProviderClient,
    ToolCall,
)
from coworker.risk import RiskClass, classify
from coworker.tools.claude_code import (
    ClaudeCodeCLI,
    claude_code_tools,
    progress_items,
    summarize_events,
)
from coworker.tools import ToolRegistry
from coworker.tools.todo import TodoList

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# A recorded stream-json run: init carries the session id, the assistant turn carries
# tool_use blocks, the final result carries the verdict and cost.
STREAM = [
    {"type": "system", "subtype": "init", "session_id": "sess-abc"},
    {
        "type": "assistant",
        "session_id": "sess-abc",
        "message": {
            "content": [
                {"type": "text", "text": "Looking at the parser."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "/w/a.py"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/a.py"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/b.py"}},
                {"type": "tool_use", "name": "Bash", "input": {"command": "pytest"}},
            ]
        },
    },
    {
        "type": "result",
        "session_id": "sess-abc",
        "result": "Added the flag and a test. pytest passes.",
        "total_cost_usd": 0.42,
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "is_error": False,
    },
]


def _fake_claude(tmp_path, events, *, exit_code=0, args_file=None):
    """Install a fake `claude` on PATH that replays `events` as stream-json.

    Records its argv to `args_file` so tests can assert on the flags we pass (--resume,
    --permission-mode, …) without a real CLI.
    """
    lines = "\n".join(json.dumps(e) for e in events)
    script = tmp_path / "bin" / "claude"
    script.parent.mkdir(parents=True, exist_ok=True)
    record = f'printf "%s\\n" "$@" > {args_file}\n' if args_file else ""
    script.write_text(
        "#!/bin/sh\n"
        f"{record}"
        f"cat <<'OCW_EOF'\n{lines}\nOCW_EOF\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script.parent


needs_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="fake CLI is a POSIX shell script"
)


# -- report folding (pure) ------------------------------------------------------
def test_summarize_counts_tools_and_collects_changed_files():
    s = summarize_events(STREAM)
    assert s["tools"] == {"Read": 1, "Edit": 2, "Bash": 1}
    # Only edit-shaped tools contribute paths, deduped, in first-seen order.
    assert s["files_changed"] == ["/w/a.py", "/w/b.py"]
    assert s["session_id"] == "sess-abc"
    assert s["cost_usd"] == 0.42
    assert s["is_error"] is False
    assert "pytest passes" in s["result"]


def test_summarize_tolerates_malformed_blocks():
    s = summarize_events(
        [
            {"type": "assistant", "message": {"content": ["not-a-dict", {}]}},
            {"type": "assistant", "message": {}},
            {"type": "unknown"},
        ]
    )
    assert s["tools"] == {} and s["files_changed"] == [] and s["result"] == ""


# -- subprocess path -----------------------------------------------------------
@needs_posix
def test_delegation_reports_result_tools_and_cost(tmp_path, monkeypatch):
    bindir = _fake_claude(tmp_path, STREAM)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    agent = ClaudeCodeCLI(workspace=tmp_path)

    out = agent.run("add a --verbose flag")

    assert out["exit_code"] == 0
    assert "pytest passes" in out["report"]
    assert out["tools_used"] == {"Read": 1, "Edit": 2, "Bash": 1}
    assert out["files_changed"] == ["/w/a.py", "/w/b.py"]
    assert out["cost_usd"] == 0.42
    assert "error" not in out


@needs_posix
def test_follow_up_resumes_the_same_claude_code_session(tmp_path, monkeypatch):
    args_file = tmp_path / "argv.txt"
    bindir = _fake_claude(tmp_path, STREAM, args_file=args_file)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    agent = ClaudeCodeCLI(workspace=tmp_path)

    agent.run("first task")
    first = args_file.read_text(encoding="utf-8")
    assert "--resume" not in first  # nothing to resume yet
    assert "acceptEdits" in first  # never bypassPermissions
    assert "stream-json" in first

    agent.run("now add tests")
    second = args_file.read_text(encoding="utf-8")
    # The session id captured from the first run is replayed, so the delegate keeps its
    # context instead of re-exploring the repo.
    assert "--resume" in second and "sess-abc" in second


@needs_posix
def test_error_result_surfaces_as_error_not_report(tmp_path, monkeypatch):
    events = [
        {"type": "system", "session_id": "sess-err"},
        {"type": "result", "result": "could not apply the patch", "is_error": True},
    ]
    bindir = _fake_claude(tmp_path, events, exit_code=1)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    out = ClaudeCodeCLI(workspace=tmp_path).run("do a thing")

    assert "could not apply the patch" in out["error"]
    assert "report" not in out  # a failed run must not read as a result


@needs_posix
def test_session_id_is_captured_even_from_a_failed_run(tmp_path, monkeypatch):
    events = [
        {"type": "system", "session_id": "sess-partial"},
        {"type": "result", "result": "boom", "is_error": True},
    ]
    bindir = _fake_claude(tmp_path, events, exit_code=1)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    agent = ClaudeCodeCLI(workspace=tmp_path)

    agent.run("failing task")

    assert agent._session_id == "sess-partial"  # so the retry resumes, not restarts


def test_missing_cli_fails_closed_with_actionable_message(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no `claude` anywhere
    out = ClaudeCodeCLI(workspace=tmp_path).run("anything")
    assert "not on PATH" in out["error"]
    assert "npm install -g @anthropic-ai/claude-code" in out["error"]


def test_empty_task_is_rejected(tmp_path):
    assert "error" in ClaudeCodeCLI(workspace=tmp_path).run("   ")


def test_bypass_permission_mode_is_not_constructible(tmp_path):
    # The delegate must never get more authority than the user granted OpenWorker.
    with pytest.raises(ValueError):
        ClaudeCodeCLI(workspace=tmp_path, permission_mode="bypassPermissions")


def test_api_key_never_appears_in_the_report(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    monkeypatch.setenv("PATH", str(tmp_path))
    out = ClaudeCodeCLI(workspace=tmp_path).run("anything")
    assert "sk-ant-secret-value" not in json.dumps(out)


# -- risk + permissions --------------------------------------------------------
def test_delegation_is_exec_risk():
    assert classify("delegate_coding_task") is RiskClass.EXEC


def test_read_only_modes_block_delegation(tmp_path):
    for mode in (Mode.PLAN, Mode.DISCUSS):
        engine = PermissionEngine(workspace_root=tmp_path, mode=mode)
        decision = engine.evaluate("delegate_coding_task", {"task": "x"})
        assert not decision.allowed and not decision.needs_user


def test_interactive_mode_requires_approval(tmp_path):
    engine = PermissionEngine(workspace_root=tmp_path, mode=Mode.INTERACTIVE)
    decision = engine.evaluate("delegate_coding_task", {"task": "x"})
    assert not decision.allowed and decision.needs_user


def test_empty_command_arg_cannot_hit_the_shell_allowlist(tmp_path):
    # delegate_coding_task is EXEC but has no `command` argument; a configured allowlist
    # must not accidentally auto-approve it.
    engine = PermissionEngine(
        workspace_root=tmp_path, mode=Mode.INTERACTIVE, allowed_commands=["git status"]
    )
    decision = engine.evaluate("delegate_coding_task", {"task": "x"})
    assert not decision.allowed and decision.needs_user


# -- catalog + persona ---------------------------------------------------------
def _names(tools):
    return {getattr(t, "__name__", "") for t in tools}


def test_claude_code_capability_yields_the_delegation_tool(tmp_path):
    ctx = AgentContext(workspace=tmp_path, todo=TodoList())
    assert _names(expand(["claude_code"], ctx)) == {"delegate_coding_task"}


def test_claude_code_capability_needs_a_workspace():
    assert not capability("claude_code").available(AgentContext())


def test_claude_code_declares_both_exec_and_write_risk():
    risk = capability("claude_code").risk
    assert RiskClass.EXEC in risk and RiskClass.WRITE_LOCAL in risk


def test_readonly_files_capability_has_no_write_tools(tmp_path):
    ctx = AgentContext(workspace=tmp_path)
    names = _names(expand(["code_files_readonly"], ctx))
    assert "read_file" in names
    assert not names & {"write_file", "replace_in_file", "apply_patch"}


def test_developer_persona_delegates_every_write(tmp_path):
    """The load-bearing property: the persona's only write path is the gated delegation."""
    reg = PersonaRegistry(state_path=tmp_path / "personas.json")
    agent = reg.agent("developer")
    names = _names(agent.build_tools(AgentContext(workspace=tmp_path, todo=TodoList())))

    assert "delegate_coding_task" in names
    assert not names & {
        "write_file",
        "replace_in_file",
        "apply_patch",
        "apply_unified_diff",
        "run_shell",
    }


def test_developer_persona_is_opt_in(tmp_path):
    # Built-ins other than the default ship disabled; the user enables it in Settings.
    reg = PersonaRegistry(state_path=tmp_path / "personas.json")
    assert "developer" in reg.ids()
    assert not reg.is_enabled("developer")


def test_catalog_still_exposes_the_original_capabilities():
    assert {"code_files", "files", "git", "search", "shell", "todo"} <= set(CATALOG)


# -- live progress streaming ---------------------------------------------------
class _ScriptedProvider(ProviderClient):
    """Requests `tool_name` once, then finishes."""

    def __init__(self, tool_name: str):
        self._turns = [
            AssistantTurn(
                tool_calls=[ToolCall(id="call_1", name=tool_name, arguments={})],
                finish_reason="tool_calls",
            ),
            AssistantTurn(text="done", finish_reason="stop"),
        ]

    def complete(self, *, model, messages, tools=None, **settings):
        return self._turns.pop(0)

    def capabilities(self, model):
        return ModelCapabilities()


async def _run_engine(tmp_path, tool, *, mode=Mode.AUTO):
    registry = ToolRegistry()
    registry.register(tool)
    engine = TurnEngine(
        provider=_ScriptedProvider(tool.__name__),
        registry=registry,
        permissions=PermissionEngine(workspace_root=tmp_path, mode=mode),
        model="gpt-5.5",
    )
    events = []
    async for event in engine.run("go"):
        events.append((time.monotonic(), event))
    return engine, events


def _slow_reporter():
    def slow_tool() -> dict:
        """A slow tool that reports progress.

        Args:
        """
        for i in range(3):
            time.sleep(0.05)
            report_progress(kind="tool", tool=f"Step{i}", target=f"file{i}.py")
        return {"ok": True}

    return emits_progress(slow_tool)


async def test_progress_arrives_while_the_tool_is_still_running(tmp_path):
    _, events = await _run_engine(tmp_path, _slow_reporter())
    kinds = [e.type for _, e in events]
    assert kinds.count(EventType.TOOL_PROGRESS) == 3

    progress_at = [t for t, e in events if e.type is EventType.TOOL_PROGRESS]
    started_at = [t for t, e in events if e.type is EventType.TOOL_STARTED][0]
    finished_at = [t for t, e in events if e.type is EventType.TOOL_FINISHED][0]
    # The whole point: they land between start and finish, not batched at the end.
    assert all(started_at < t < finished_at for t in progress_at)
    assert progress_at[0] < progress_at[-1]


async def test_progress_events_identify_the_tool_and_carry_the_payload(tmp_path):
    _, events = await _run_engine(tmp_path, _slow_reporter())
    first = next(e for _, e in events if e.type is EventType.TOOL_PROGRESS)
    assert first.data["name"] == "slow_tool"  # which tool call this belongs to
    assert first.data["kind"] == "tool"
    assert first.data["target"] == "file0.py"


async def test_progress_is_display_only_and_never_enters_the_history(tmp_path):
    engine, _ = await _run_engine(tmp_path, _slow_reporter())
    blob = json.dumps(engine.messages)
    # The model sees the tool's return value, never the progress items.
    assert "Step0" not in blob and "file0.py" not in blob


async def test_a_tool_that_does_not_opt_in_streams_nothing(tmp_path):
    def quiet_tool() -> dict:
        """A tool that reports progress but never opted in.

        Args:
        """
        report_progress(kind="tool", tool="Ignored")
        return {"ok": True}

    _, events = await _run_engine(tmp_path, quiet_tool)
    assert not [e for _, e in events if e.type is EventType.TOOL_PROGRESS]
    assert [e for _, e in events if e.type is EventType.TOOL_FINISHED]  # still ran


async def test_progress_reported_just_before_returning_is_still_flushed(tmp_path):
    """Guards the post-completion drain: without it the last items are dropped."""

    def late_tool() -> dict:
        """Reports right before returning.

        Args:
        """
        report_progress(kind="tool", tool="LastGasp")
        return {"ok": True}

    _, events = await _run_engine(tmp_path, emits_progress(late_tool))
    payloads = [e.data for _, e in events if e.type is EventType.TOOL_PROGRESS]
    assert [p["tool"] for p in payloads] == ["LastGasp"]


def test_report_progress_outside_a_turn_is_a_noop():
    report_progress(kind="tool", tool="Nobody listening")  # must not raise


def test_the_delegation_tool_opts_into_streaming():
    tool = claude_code_tools(ClaudeCodeCLI(workspace="."))[0]
    assert wants_progress(tool)


# -- progress item shaping -----------------------------------------------------
def test_progress_items_covers_narration_and_tool_use():
    items = progress_items(STREAM[1])
    assert items[0] == {"kind": "narration", "text": "Looking at the parser."}
    assert items[1] == {"kind": "tool", "tool": "Read", "target": "/w/a.py"}
    assert {"kind": "tool", "tool": "Bash", "target": "pytest"} in items


def test_progress_items_ignores_non_assistant_events():
    assert progress_items(STREAM[0]) == []  # system/init
    assert progress_items(STREAM[2]) == []  # result — TOOL_FINISHED covers it


def test_progress_items_never_forwards_file_contents():
    # A Write's `content` must not ride along to the surface; only its path does.
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Write",
                    "input": {"file_path": "/w/x.py", "content": "SECRET BODY"},
                }
            ]
        },
    }
    assert progress_items(event) == [
        {"kind": "tool", "tool": "Write", "target": "/w/x.py"}
    ]


@needs_posix
def test_delegation_streams_progress_from_the_real_event_stream(tmp_path, monkeypatch):
    bindir = _fake_claude(tmp_path, STREAM)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    seen: list[dict] = []
    from coworker.progress import bind_sink, reset_sink

    token = bind_sink(seen.append)
    try:
        ClaudeCodeCLI(workspace=tmp_path).run("add a flag")
    finally:
        reset_sink(token)

    assert {"kind": "narration", "text": "Looking at the parser."} in seen
    assert {"kind": "tool", "tool": "Edit", "target": "/w/a.py"} in seen
