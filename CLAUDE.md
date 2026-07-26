# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OpenWorker is a local-first AI coworker desktop app: a **Python agent server** (`coworker/`) that owns the agent loop, tools, and connectors, plus a **React + Tauri desktop shell** (`surfaces/gui/`) that is a thin client of it. Everything runs on the user's machine with their own model keys. The engine is built on [aisuite](https://github.com/andrewyng/aisuite) (git-pinned in `pyproject.toml`), which supplies the unified provider API, toolkit metadata, and docstring→JSON-schema extraction.

| Directory | What's in it |
|---|---|
| `coworker/` | Python backend — engine, providers, connectors, MCP, memory, personas, automations, server |
| `surfaces/gui/` | React UI + Tauri (Rust) shell that supervises the server |
| `stt/` | `ocw-stt` — Rust dictation **library**, compiled into the Tauri shell (not a separate process) |
| `packaging/` | Dev bootstrap, PyInstaller server bundle, DMG/Windows installers, update manifest |
| `tests/` | Backend suite (flat `test_*.py`, no subdirectories) |

## Commands

### Bootstrap (once per checkout)

```bash
bash packaging/setup_dev_env.sh     # creates .venv at repo root, installs -e ".[messaging,dev]"
```

Everything from-source expects the venv at `./.venv`. The Tauri shell falls back to `.venv/bin/openworker-server` when no packaged sidecar binary exists.

### Run

```bash
# Terminal 1 — agent server
.venv/bin/openworker-server --cwd ~/some/project --port 8765

# Terminal 2 — browser UI on :1420
cd surfaces/gui && npm install && npm run dev

# …or the desktop app instead (supervises the server itself, no terminal 1)
cd surfaces/gui && npm run tauri dev
```

Dev runs on a **fixed port 1420 with `strictPort`** so the Tauri webview always loads the Vite instance Tauri spawned; `tauri.conf.json`'s `devUrl` must stay in sync. (Both `surfaces/gui/README.md` and `e2e/README.md` still say 5173 — they are stale.)

Start the server **before** Vite: the standalone server writes a per-launch token to `<state-dir>/sidecar-<port>.token`, and `vite.config.ts` reads that file *at config-load time* and bakes it into the bundle as `__COWORKER_DEV_TOKEN__`. So **restart Vite whenever you restart the server.** The desktop app injects an in-memory token instead and never writes it to disk.

### Test

```bash
.venv/bin/pytest                                    # backend suite
.venv/bin/pytest tests/test_engine.py               # one file
.venv/bin/pytest tests/test_engine.py::test_name    # one test
.venv/bin/pytest -k permission                      # by keyword

cd surfaces/gui
npm test                                  # vitest unit (src/**/*.test.ts{,x})
npx vitest run src/streamGate.test.ts     # single unit file
npx vitest run -t "authenticates REST"    # single unit test by name
npm run e2e                               # hermetic Playwright (no Python needed)
npx playwright test e2e/settings.spec.ts  # single spec
npx playwright test -g "renames a session"  # single e2e test by title
npm run e2e:ui                            # Playwright watch/inspect mode
npm run e2e:live                          # against a REAL backend on :8765 (not CI)
npx tsc --noEmit                          # typecheck (no npm script for it)
```

`asyncio_mode = "auto"` is set, so async tests need no decorator. Unit tests sit *beside* their source (`src/foo.test.ts`), not in a `__tests__` directory.

The Slack harness also runs standalone against a live dev server:

```bash
python -m coworker.testing.fake_slack --port 8910   # prints the SLACK_API_URL to export
```

### CI gates

`.github/workflows/ci.yml` runs exactly three jobs on push/PR: `pytest tests -q` (Python 3.12 only, installed via `pip install -e ".[messaging,dev]"`), `npm test`, and `npm run e2e`. That is the whole contributor gate.

**There is no linter, formatter, or typecheck job**, and no config for one exists (no ruff/black/mypy/eslint/prettier/pre-commit). `tsc` runs only inside `npm run build`, which CI never invokes — run `npx tsc --noEmit` yourself before finishing GUI work (`tsconfig.json` is `strict` plus `noUnusedLocals`/`noUnusedParameters`). Python is nevertheless formatted black-style (88 cols, double quotes, `from __future__ import annotations` at the top of nearly every module); match it by hand.

The Rust crates (`stt/`, `src-tauri/`) are not built or tested in CI at all. Releases run from tags and **hard-fail if the tag doesn't match `surfaces/gui/src-tauri/tauri.conf.json`'s `version`**, which is the single source of truth for the app version.

## Architecture

### The turn engine

`coworker/engine.py` (`TurnEngine`) is the center of gravity. One user turn spans many model↔tool iterations until the model stops requesting tools, a rail trips, or it's interrupted. It is async, but wraps blocking provider/tool calls in `asyncio.to_thread` so consuming UIs stay responsive. When the model requests several tool calls at once, **low-risk calls (reads, searches) run concurrently while writes and shell stay strictly ordered.**

Tool calls are **authorized sequentially first** (approval prompts are interactive), then executed. Approvals are out-of-band: on `needs_user` the engine emits `PERMISSION_REQUIRED` and awaits an injected async `approver`. Swapping that approver is how one engine serves inline GUI prompts, the Inbox, and unattended automation runs.

Three tools — `request_directory`, `propose_plan`, `ask_user` — are **engine-intercepted and bypass the registry and permission path entirely**, because the user's out-of-band answer *is* the consent. Their registered callables are schema carriers with a "not available in this surface" fallback body; injected callbacks do the real work.

`coworker/events.py` defines the `EventType` enum that is **the only contract between the engine and any surface** (GUI/TUI/IDE). Adding a surface means consuming these events; adding engine behavior usually means adding one. (Its docstring still claims "no token streaming in v1" — stale; deltas exist.)

`coworker/agent.py`'s `build_engine()` is the **single assembly point for everything the model can see**: persona tools (via `catalog.expand`), MCP/connector tools, messaging, memory, skills, subagents, scheduling. If a tool isn't registered there it does not exist to the model. Capability branching is by `Agent` *traits* (`family`, `messaging`, `connectors`), not by agent name. Note that it monkey-attaches `engine.executor`, `engine.roots`, `engine.todo`, etc., which the manager and WS handler then read.

### Permissions, risk, and the Inbox

This is the most security-sensitive area of the codebase; several files carry "why" comments documenting past incidents. Read them before relaxing anything.

- **`coworker/risk.py`** — every tool has a `RiskClass`: `READ` (always allowed), `WRITE_LOCAL` (path-scoped), `EXEC` (shell), `EXTERNAL` (off-machine side effects). Effective risk = user override ?? by-name table ?? aisuite `requires_approval` metadata ?? `READ`.
- **`coworker/permissions.py`** — `PermissionEngine.evaluate()` returns allow/deny/`needs_user`. It only *decides*; the turn engine routes. Modes: `discuss` and `plan` (read-only), `interactive` (default), `auto`, `custom`.
- Command allowlisting matches **parsed argv prefixes**, and rejects outright any command containing shell metacharacters (`;  &  |  >  <  \`  $(  (`  newlines). Prefix-matching raw strings was unsafe — `git status` would have auto-approved `git status && rm -rf ~`.
- **`DEFAULT_ALLOWED_COMMANDS` is intentionally empty.** There is no generally safe executable; nominally read-only programs can read secrets outside the workspace or execute helpers (`find -exec`, pytest collection).
- **`coworker/inbox.py`** — the cross-session human-attention queue. The central design decision: **all four interactive prompts (approval, question, directory, plan) are parked as Inbox items in *both* attended and unattended mode.** A `visibility` field (`VIS_INLINE` vs `VIS_INBOX`) decides only *where they surface* — inline on the session's live socket, or in the cross-session list mirrored to a bound channel. Either way it's the same parked, awaitable, resolve-from-anywhere record, which is what lets a prompt survive a dropped socket or a server restart and be answered from Slack. Items go `pending → resolved` exactly once, first-responder-wins, keyed idempotently on `(session_id, tool_call_id)`.
- **`coworker/unattended.py`** — a per-session flag for *where the human is reached*. It does **not** change the autonomy ceiling; the permission mode does.

### Configuration and trust boundaries

`coworker/config.py` layers TOML: built-in defaults < global (`<state-dir>/config.toml`) < per-workspace (`<workspace>/.coworker/config.toml`).

The critical invariant: `allowed_commands` and `auto_allow` are **global-only fields**. A repository's `.coworker/config.toml` can request command prefixes, but they stay advisory until the user trusts that exact canonical workspace path (`coworker/workspace_trust.py`). Trust follows the path, not a config snapshot. Never widen `_WORKSPACE_FIELDS` to include a field that grants authority.

`coworker/secrets.py` — one file-backed store (`0600` JSON) for connector/MCP credentials, keyed `connector[:account]`, with `${ENV_VAR}` indirection resolved at read time. The invariant: **secrets never enter the model's context, prompts, or traces.** The interface is what callers depend on so a Keychain backend can swap in later.

`coworker/environment.py` injects a workspace/git snapshot into the system prompt (saving discovery tool calls) and carries the **folder-scope rule**: the agent works inside the workspace and granted roots only, never sweeping the home directory — on macOS every stray touch fires an OS permission prompt the user can't attribute to anything they did.

`state_dir()` in `secrets.py` is the single source of truth for where everything lives: `$COWORKER_STATE_DIR` → `%APPDATA%\coworker` → `~/.config/coworker`. It holds `secrets.json`, `config.toml`, `mcp.json`, global `AGENTS.md`, `coworker.db` (sessions index + workspaces + memory + audit events), `automation.db`, append-only `conversations/<id>.jsonl` message logs, installed personas and skills, and a dozen small JSON registries (inbox, routing, unattended, wakes, subscriptions, workspace trust, risk overrides). Per-workspace overrides live in `<workspace>/.coworker/`.

On Windows, `os.chmod(0o600)` is a silent no-op, so `_restrict_to_user()` shells out to `icacls` with `(OI)(CI)F` — omitting those inheritance flags produced empty DACLs and `sqlite3 "unable to open database file"` crashes at launch.

### Providers

`coworker/providers/` uses a descriptor + factory pattern mirroring connectors and web-search. A `ProviderDescriptor` declares UI config fields (rendered dynamically by the GUI) and a `build(profile, secrets)` factory. `ProviderRouter` is the single `ProviderClient` handed to every engine: it dispatches on the `provider:` prefix of the model string (`ollama:llama3.3` → Ollama; bare `gpt-5.5` → OpenAI default), strips the prefix, and caches clients. Config changes call `invalidate()` so live engines pick them up without a rebuild.

`ProviderClient` (`base.py`) is deliberately **blocking and loop-free** — no `max_turns`, because the runtime owns the loop. Three native providers (OpenAI, Anthropic Messages, Gemini) plus nine OpenAI-compatible vendors built from a shared factory, two resellers, and two local runtimes (Ollama, LM Studio). **Compat-vendor keys resolve only from that vendor's own profile/env** — never falling back to the OpenAI key, so a configured OpenAI key is never sent to a third-party endpoint; the local pair pass a placeholder key explicitly for the same reason.

The local pair (`LOCAL_PROVIDERS` in `registry.py`) differ from every keyed provider in three ways worth knowing before touching them: they are keyless, so `configured` is true from the first render and **an empty `{}` profile is the valid representation of "connected"** (test it with `is None`, not falsiness — a truthiness check silently disables model discovery); their model lists come from **live probes of the user's machine**, not `matrix.py`, which deliberately curates no local entries; and their picker rows are **liveness-gated**, so a closed server leaves no phantom models behind. LM Studio's discovery prefers its typed `/api/v0/models` over `/v1/models` because the latter mixes embedding models in with chat models.

Each provider's module docstring catalogues the quirks its converter absorbs — read it before editing. Anthropic: tool results must all collapse into the single next user message; thinking blocks replay verbatim with signatures. Gemini: no function-call ids (synthesized and mapped back by name), OpenAPI-3.0-subset schemas, and thought signatures that must be echoed or tool loops break. Provider-private sidecars ride in `AssistantTurn.extras` under underscore keys; **the owning provider reattaches its own, every other provider strips foreign ones.**

`matrix.py` is the curated model list, keyed by the **full routed id** exactly as the router sees it (including reseller names like `together:zai-org/GLM-5.2`). It's intentionally small — current-generation tool-calling models only; arbitrary model strings work at the user's risk.

### Tools, catalog, personas, and subsystems

- `coworker/tools/registry.py` wraps callables into schemas for the model. Permission checks are **not** here — they live in `PermissionEngine`, applied by the turn engine.
- `coworker/tools/shell.py` — `LocalExecutor` keeps **one long-lived shell process** so `cd`, `export`, and activated venvs persist across calls. Background tasks get a detached process that `close()` deliberately does not kill.
- `coworker/tools/subagent.py` — `explore` spawns a read-only child `TurnEngine` in plan mode with no approver and no recursion. That's precisely why it can carry low-risk metadata and therefore run in parallel with other reads.
- `coworker/catalog.py` is the vetted, **platform-owned and closed** `id → capability` layer. A capability bundles tool factories behind a stable id plus its context `requires` and producible `risk`. `expand(ids, context)` turns a persona's `tools:` list into callables, skipping capabilities whose prerequisites are unmet. Third parties gain breadth via MCP, never by adding catalog entries; MCP tools are deliberately outside the catalog.
- `coworker/personas/` — a persona is a manifest (YAML frontmatter + a markdown body that *is* the system prompt) composing catalog capabilities. The built-in surfaces are themselves manifests in the same format third parties use. Parsing is **strict**: an invalid manifest raises rather than yielding a half-broken persona. Third-party installs snapshot the manifest and land disabled + unsurfaced pending consent.
- `coworker/skills/` — Anthropic SKILL.md format with **progressive disclosure**: only name + description enter the prompt; the body loads on demand via `load_skill`.
- `coworker/memory/` — scoped GLOBAL/WORKSPACE/SESSION store behind an ABC, SQLite-backed; memories render into the prompt with `[#id]` markers so the agent can revise them by id.
- `coworker/automation/` — scheduled tasks as persistent entities with a 30s tick, **run-once catch-up** for missed runs and **skip-on-overlap**. Each fire is a real, persisted, *continuable* session the user can reopen. The scheduler spawns runs rather than awaiting them, so one automation blocked on an Inbox approval can't stall the tick loop.

### Connectors and MCP

`coworker/connectors/` is the largest subsystem (~13.5k lines). **Adding a connector is mostly data, not code**: declare it in `descriptors.py` (auth method, form fields, setup instructions, branding, and a `validate()` that confirms the token against the real API), list its tools in `tool_defs.py` (per-tool read/write `kind`, `default_enabled`, and the `target_arg` that makes standing rules possible), and build the callables in `integration_tools.py`.

Four auth paths coexist: manual token paste; **managed OAuth** brokered by OpenWorker Cloud (`cloud.py`) where the broker form-POSTs the token to the local sidecar so **connector tokens never live in cloud storage**; **MCP-backed one-click**, which runs the OAuth flow entirely locally with no broker and exposes a *pinned* tool subset rather than the vendor's full catalog ("drift can only shrink capability, not grow it"); and the **managed Slack/GitHub relay**, which is inbound-only — replies always go desktop → vendor API directly.

`base.py` defines the platform-agnostic adapter contract; replies use an opaque **target token** `platform:chat_id[:thread]`. `gateway.py` routes inbound: allowlist → reply-correlation (`[ow:<id>]`) → handler, and messages from unknown senders are **parked** for one-step allow-and-deliver rather than dropped.

`coworker/mcp/` is a custom async client on the official `mcp` SDK. Each server runs in a **dedicated asyncio task** that opens and closes its transport in the *same* task (the SDK's anyio cancel scopes require it — no `nest_asyncio`, no second loop). Tools are wrapped as *sync* callables that bridge back via `run_coroutine_threadsafe`. Config is the standard `mcpServers` JSON, paste-compatible with Claude Desktop/Cursor. OAuth tokens go to the `SecretStore`, never to `mcp.json`, which is plain text and meant to be shareable.

### Server

`coworker/server/app.py` — FastAPI control plane every surface rides on. Two transports: a **WebSocket** carrying the engine event stream and approval channel, and `/v1/chat/completions`, an OpenAI-compatible proxy so any OpenAI-format client can use the runtime as a backend.

Auth: a per-launch token, sent as an `X-OpenWorker-Token` header on REST and as a WebSocket subprotocol (`["openworker", token]`). The app binds to loopback, but loopback is reachable from any page in the user's browser, so there is also a strict **origin gate** pinned to the Tauri webview origins and localhost dev — CORS never covers WebSockets, and a permissive config previously let any visited website drive a session into shell/file tools. Requests with no `Origin` header (curl, native clients, tests) are treated separately.

Events are **broadcast** to every socket on a session (including the sender), so a second view stays in sync and background turns surface live. `try_mark_running()` is an atomic claim taken *before* scheduling the turn task, so two back-to-back frames can't both start a turn. Validation failures use a distinct `input_rejected` event so the GUI doesn't offer "Retry". Rate limits exist because **the loopback socket is reachable by any local process**.

`coworker/server/manager.py` (`SessionManager`, 3.7k lines) is the god object: one cached engine per session, all stores, the shared `ProviderRouter`, `MCPManager`, `Gateway`, `Scheduler`, and `PersonaRegistry`. It's also the background-turn machinery — `deliver_to_session()` steers a busy session or runs a fresh turn on an idle one **with no live socket attached**, dead-lettering failures. The Slack mention router lives here too: an `@OpenWorker` tag in an unsubscribed channel spawns a session that owns that thread, with a standing `send_message → <thread>` grant so the conversation can't stall on an approval nobody in Slack can see.

### GUI

**`surfaces/gui/src/api.ts` is the single chokepoint for backend access** — every REST call and both WebSockets go through it; no component calls `fetch` directly. A module-local `fetch` wrapper attaches the auth header so no endpoint helper has to remember it. Add new backend calls here.

Endpoint resolution: runtime globals (`window.__COWORKER_HTTP__`, injected by Tauri for its dynamically chosen sidecar port) → Vite env (`VITE_COWORKER_HTTP`/`VITE_COWORKER_WS`) → `127.0.0.1:8765`. One codebase serves both browser dev and desktop.

Transport is REST + WebSocket, **no SSE**. Two sockets: `/ws/session/{id}` (the `Session` class — client sends `user_message`/`approval`/`interrupt`/`set_mode`/…; server sends the `WsEvent {type, data}` union in `src/types.ts`) and `/ws/events` for app-wide push, auto-reconnecting every 5s. `Session` queues frames sent while still `CONNECTING` and flushes on open, and nulls its handlers before `close()` so a dead socket's late events can't clobber its successor.

**There is no state library and no router** — React 18 with `useState` in `App.tsx` and prop drilling. Cross-component signaling goes through `window` CustomEvents dispatched from `api.ts` (`coworker:personas-changed`, `coworker:roots-changed`, `coworker:open-settings`, …); persisted UI state is `localStorage` only. Feature flags (`src/flags.ts`) are read at *render* time so tests can flip them without a reload race.

`src/tauri.ts` uses the injected `window.__TAURI__` global rather than `@tauri-apps/api` packages, so the browser build carries zero Tauri deps and every command degrades gracefully when `isTauri()` is false.

### Tauri shell

`src-tauri/src/lib.rs` is the whole shell. It binds port 0 to **pick a free port** (deliberately not 8765, so the app coexists with a hand-run dev server), spawns the Python server as a supervised child, and passes it a per-launch `COWORKER_API_TOKEN` plus `COWORKER_PARENT_PID` — an explicit PID because under PyInstaller the Python process is a *grandchild*, so `getppid()` wouldn't point at the shell. Server stdout/stderr goes to `<state-dir>/logs/openworker-server.log` (never `/dev/null` — undiagnosable field bugs). Binary resolution ends with a dev fallback to `.venv/bin/openworker-server`.

Voice input is **native-only by design**: `stt/` captures and transcribes locally with a checksum-pinned Whisper model, downloaded only on explicit user action. The browser build shows no mic rather than uploading audio. macOS requires Apple Silicon + 12+; Windows x64 + build ≥ 19045.

## Invariants to preserve

These are load-bearing and cheap to break by accident. Most have a comment at the site explaining the bug that motivated them.

1. **The canonical message history is always OpenAI-shaped.** Providers convert per call, which is what makes switching models mid-conversation a single field write.
2. **`_outbound_messages()` is the sole provider feed — never mutate `engine.messages` to shape a request.** It strips display-only sidecars, drops `role:"notice"` markers, adapts PDFs/images to the *active* model's capabilities, and appends the ephemeral `<system-context>` block to the last user message rather than inserting a mid-thread system message (unreliable across providers). Shaping at send time is what lets a mid-session model switch re-decide everything.
3. **Never leave an orphaned `tool_call`.** On interrupt or denial every pending call still gets a `role:"tool"` error message — hosted chat templates reject orphans, and durable resume would re-prompt them.
4. **Every parked prompt must be idempotent on `(session_id, tool_call_id)`**, or durable resume double-prompts and double-executes.
5. **`roots` and `permissions.task_rules` are shared by reference on purpose.** Copying them breaks runtime folder grants and standing rules minted mid-run, both of which must affect the very next tool call.
6. **Persist on checkpoints, not just at turn end** (`turn_start`, `permission_required`, `iteration_end`, …), so a crash can't eat the conversation.
7. **Fail closed.** MCP tools default to `requires_approval=True`; unclassified pinned tools stay gated; grant parsing drops anything it can't validate; unknown risk resolves to the base table, never a bypass.
8. **Never start an interactive OAuth flow from inside a turn.** A token-less MCP server would open a browser and block *every* session for the full flow timeout. Flows start only from an explicit connect action.
9. **A `_display` key on a tool result is user-facing metadata the model must never see** (e.g. how many results a privacy filter hid). It's audited as a rule-class + count, never as content.
10. **Risk overrides are never written by a persona or package** — a persona declares what it wants; only the user grants trust.

## Testing conventions

`tests/` is **flat** — 80 `test_<subject>.py` files, one per subsystem, ~840 test functions, and a single `conftest.py`. Everything is offline: providers are exercised through locally-defined fakes (`ScriptedProvider`, `FakeOpenAI`, …) rather than a shared fixture library, so expect to define a double in the file you're working in, matching its neighbors.

An **autouse fixture in `tests/conftest.py` isolates `COWORKER_STATE_DIR` for every test.** This is not optional hygiene: without it, any test building a `SessionManager` reads the developer's real machine-global state, including their cloud sign-in — which once emitted real telemetry to the production table. Don't bypass it.

`coworker/testing/fake_slack` ships in the source tree (not `tests/`) because it's a reusable harness: it boots an in-process fake Slack API on an ephemeral port and points the adapter at it via `SLACK_API_URL`, so the real `SlackAdapter`/`slack_bolt` stack runs end-to-end with no network or tokens. Use the `fake_slack` fixture.

The Playwright suite is **hermetic** — `e2e/fixtures.ts` mocks every `/v1` request and the event WebSocket at the network layer, so no Python backend is needed and no real state is touched. The mocked WS is a scripted fake agent speaking the real `{type, data}` protocol (`"run a tool"` in a message triggers a `tool_proposed` + `permission_required` flow), which exercises production send/stream/approve code paths at zero model cost. Mutations persist in per-test in-memory state so re-fetches reflect them. It runs Vite on port **5199** to avoid clashing with a running `npm run dev`. If a flow reads a new endpoint, add its fixture *and* a route branch — the catch-all returns `{}`, which crashes components expecting arrays. See `surfaces/gui/e2e/README.md` for seed data.

## Conventions and gotchas

- Commit subjects are **imperative sentence-case with no prefix** most of the time (`Persist Always-allow grants with the session`). A lowercase `area:` prefix is used for scoped work (`security:`, `gui:`, `engine:`, `updater:`, `packaging:`, `ci:`). Areas are ad-hoc — this is *not* Conventional Commits (no `feat:`, no scopes, no breaking-change footers). Bodies are usually empty; work lands via PR merge commits into `main`.
- The README asks that PRs attach before/after screenshots, and notes that features already on the maintainers' internal roadmap may be declined.
- **Stale paths:** `surfaces/gui/README.md` and many docstrings still prefix paths with `platform/` (`platform/packaging/setup_dev_env.sh`, `platform/.venv`) from before this code moved out of the aisuite monorepo. The repo root *is* the platform; drop the prefix. The README's claim that `docs/` holds "design specs and decision logs" is also stale — it holds `config.example.toml` and one image.
- `docs/config.example.toml` is illustrative and has drifted from the real defaults in `coworker/config.py` (model id, `max_iterations`, and a non-empty `allowed_commands` that the code deliberately leaves empty). Trust `config.py`.
- Windows needs `tzdata` (declared conditionally) — without it every named schedule timezone silently falls back to local time.
- PDF rasterization uses `pypdfium2`, **not** PyMuPDF, whose AGPL license can't ship in the DMG.
- `base: "./"` in `vite.config.ts` is load-bearing: absolute `/assets` URLs 404 under the `tauri://` origin.
- The `Session` WebSocket effect in `App.tsx` intentionally omits `workspace` from its dependency array; adding it reintroduces a "sends twice" bug. There's a comment saying so.
- `aisuite` is a **git-pinned** dependency, so a fresh install needs network access and `git`.
- The PyInstaller bundle is **one-dir, not one-file** (one-file re-extracted ~140MB every launch, costing 6–7s of splash). Experimental connectors are *stripped* from the bundle unless `COWORKER_EXPERIMENTAL=1`.

## Where the documentation actually lives

**Module docstrings in `coworker/` are the primary architecture documentation.** They carry substantial design rationale and frequently name the incident, attack, or rejected alternative that motivated the current shape. Read the docstring before changing a module, and update it when the rationale changes.

Beyond those, the only prose in the repo is `README.md`, `surfaces/gui/README.md`, and the very detailed `surfaces/gui/e2e/README.md`. Design-spec references cited throughout docstrings and test names — `PERSONAS.md`, `UX-DECISIONS.md` (§25 standing rules), `PERMISSIONS-AND-INBOX.md`, `FAKE-SLACK-SPEC.md` — **were left behind in the aisuite monorepo and do not exist here**, as are the `P0`–`P7 gate` / `Phase N` / `§N` milestones in test docstrings. Don't hunt for them; treat the code and its docstrings as authoritative.
