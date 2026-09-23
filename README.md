# MCP Socket for Houdini

Local MCP bridge for Side Effects Houdini — the Houdini branch of the [mcp-socket](https://github.com/abyrvalg379/mcp-socket) family. A TCP listener runs inside Houdini and speaks the same wire protocol as the Blender and Maya bridges (blender-mcp 1.6.x compatible), so any MCP client can drive Houdini through typed tools.

Part of the **STUKACH — Pipeline Asset Validation System** toolset.

**Author:** Maksim Kovalev · **Version:** 0.1.1 · **License:** GPL-3.0

*Документация на русском: [README.ru.md](README.ru.md)*

## How it works

```
MCP client → houdini_mcp.py (stdio) → TCP 127.0.0.1:9877 → mcp_socket_houdini.server (inside Houdini)
```

Every command is marshalled to Houdini's main thread through a
`hou.ui.addEventLoopCallback` job queue (`queueToMainThread` does not exist
in Houdini 20.5); the socket itself lives on a daemon thread. Wire protocol —
one JSON document per request, no framing:

```
→ {"type": "ping", "params": {}}
← {"status": "success", "result": {...}}
```

## Tools (13)

| Tool | Purpose |
|------|---------|
| `ping_houdini` | Houdini version, pid, port, hip file, fps, object counts |
| `execute_houdini_code` | Python inside Houdini — `hou` preinjected, stdout/stderr captured, optional `result` variable returned JSON-safely; one call = one "MCP Socket" undo group |
| `undo_agent_session` | performUndo() while the top of the undo stack is an MCP Socket group — the user's own entries are never touched |
| `get_scene_info` | hip file, modified flag, fps, frame, playback range, /obj /out /stage /ch contexts |
| `get_hierarchy` | /obj node tree (paths, types, depth, display flag), capped |
| `get_screenshot` | mode `window` (default): Qt grab of the main window, occlusion-proof; mode `flipbook`: true viewport render with the current flipbook settings |
| `get_console_log` | ring buffer of the whole Houdini session's stdout/stderr (global tee + per-execution captures) |
| `clear_console_log` | clear the ring |
| `list_instances` | live Houdini instances from a `%TEMP%` registry |
| `export_fbx` | PROKLADKA neutral export via the filmboxfbx ROP: binary, `convertunits=1` (meters), object transform baked through an idempotent `mcp_bake_xform` node |
| `import_fbx` | `hou.hipFile.importFBX` under a subnet container (identity transform); bbox reported in meters (Houdini units), roots over 50 m flagged; no auto-rescale, ever |
| `replay_last_session` | re-run the modifying commands of the last recorded session from the JSONL log |
| `get_session_log_path` | path of the newest JSONL session log |

Session log: every recorded command appends a JSON line to
`%TEMP%/mcp_socket_houdini/sessions/`; a >10 s gap starts a new file, the 30
newest are kept. `replay_last_session` skips read-only steps and marks
replayed ones `replay: true`.

The **MCP Socket** shelf button (installed as a Houdini package, visible via
the `+` button in the shelf tab bar) opens a window with the same sections as
the Blender N-panel: status header, Undo Agent Work, Agent Sessions, console
log viewer, Pipeline FBX.

## Install

1. Grab `mcp_socket_houdini_v*.zip` from the
   [latest release](https://github.com/abyrvalg379/mcp-socket-houdini/releases/latest)
   and unpack.
2. With Houdini **closed**, run the installer with hython:

   ```
   "C:\Program Files\Side Effects Software\Houdini 20.5.278\bin\hython.exe" install_mcp_socket_hou.py
   ```

   It assembles a Houdini package in the pref dir (resolved via
   `hou.getenv("HOUDINI_USER_PREF_DIR")`), installs the shelf and the
   autostart hook (`scripts/456.py`).
3. Restart Houdini — the bridge listens on `127.0.0.1:9877` (busy → 9878, ...).

### Connect from any MCP client

Zero dependencies beyond the Python standard library:

```json
{
  "mcpServers": {
    "houdini": {
      "command": "python",
      "args": ["<pref>/scripts/houdini_mcp.py"]
    }
  }
}
```

`--port 9878` or the `HOUDINI_MCP_SOCKET_PORT` env var selects a second
Houdini instance.

## Security

localhost-only, no authentication, and `execute_houdini_code` runs arbitrary
Python in your Houdini — this is a single-workstation tool for artist+agent
workflows, not a service. Do not expose the port.

## Related tools

- [mcp-socket](https://github.com/abyrvalg379/mcp-socket) — the Blender branch of the family
- [mcp-socket-maya](https://github.com/abyrvalg379/mcp-socket-maya) — the Maya branch of the family
- [PROKLADKA](https://github.com/abyrvalg379/prokladka) — FBX bridge Blender ↔ Maya ↔ Houdini ↔ UE
- [STUKACH](https://github.com/abyrvalg379/STUKACH) / [STUKACH_Maya](https://github.com/abyrvalg379/STUKACH_Maya) — pipeline asset validators
