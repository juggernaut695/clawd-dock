# pc-manager-mcp — project context

A Windows side-dock app + MCP server that:

- Renders a frameless docked HUD on the right edge of the primary monitor (480 px wide,
  registered as a Win32 AppBar so other windows reflow around it).
- Hosts four tabs: **Voice** (Claude voice assistant w/ mascot), **Tasks** (checklist),
  **Code** (Claude Code session navigator + chat windows), **Monitor** (CPU/RAM/GPU stats
  + Clear RAM).
- Exposes 19 MCP tools to Claude (system stats, RAM trim, tasks CRUD, Claude-instance
  groups, persistent memory).

## Stack

- **pywebview 6.2.1** + **Edge WebView2** for the dock window.
- **React 18 + Babel** loaded via CDN, no bundler. Source in `pc_manager/web/`.
- **WebSocket bridge** on `127.0.0.1:7654` for two stream types:
  - `/term/<id>` — xterm.js ↔ ConPTY (via `pywinpty`).
  - `/chat/<id>` — Code-tab chat: streams `claude -p --output-format stream-json
    --include-partial-messages` text deltas + session id + result meta.
- **Claude Agent SDK** (`claude-agent-sdk`) for the persistent Voice-tab session.
- **MCP server** (`pc_manager/server.py`) — 19 tools, used by Claude when
  spawned via the dock.

## Files

- `pc_manager/dock.py` — pywebview launcher, AppBar registration, JS-Python API,
  WebSocket server, `clear_ram` skip-set logic.
- `pc_manager/web/index.html` — HTML shell + chat-window CSS + composer / popover styles.
- `pc_manager/web/side-dock.css` — dock surface styles (panels, header pills, frosted glass).
- `pc_manager/web/app.jsx` — main React app (dock + full-chat-window mode behind `#mode=chat`).
- `pc_manager/web/clawd.jsx` — voice mascot animation states.
- `pc_manager/voice.py` — voice assistant (SAPI Mark / edge-tts Aria, persistent SDK
  session, listener pause guards).
- `pc_manager/server.py` — MCP server registration of the 19 tools.
- `pc_manager/terminal.py` — ConPTY wrapper, terminal registry.
- `pc_manager/ram_clear.py` — `EmptyWorkingSet` driver with `min_working_set_mb` filter
  to avoid trimming small graphics helpers (which causes UI flicker).

## Run

```bat
pythonw -m pc_manager.dock
```

Or `start.bat` for a no-console launch.

Set `PC_MANAGER_DEBUG=1` to enable WebView2 DevTools (F12 in the dock window).

## Known constraints

- **Dock window is opaque** (`background_color="#0a0e1a"`). `transparent=True` on the dock
  paints an uncomposited white vertical strip on the left edge — WebView2's transparent
  backbuffer can't stay in sync with a long-lived, full-height, complex-DOM window.
  Frosted-glass effect is built up via CSS layers (radial-gradient ambient wash + noise
  grain in `.app::before/::after`, multi-stop linear gradient + inset highlights on `.panel`),
  not `backdrop-filter`.
- **Chat window stays transparent** (`transparent=True` works there because the surface is
  smaller, simpler DOM, recreated fresh per `show()`). DWM acrylic + rounded corners
  applied in `_chrome_chat_window`.
- **No `backdrop-filter` anywhere on the dock CSS** — Monitor's stacked panels overload
  WebView2's compositor and render black on certain GPU paths.
- **Clear RAM** filters by minimum working-set size (≥ 25 MB) and skips
  `pythonw / msedgewebview2 / dwm / explorer` by name to avoid blanking the dock during
  the trim.
