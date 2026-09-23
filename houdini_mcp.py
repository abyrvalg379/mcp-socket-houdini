#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP Socket for Houdini — stdio MCP server (mcp-socket family).

Chain: MCP client → this process → TCP 127.0.0.1:9877 → the TCP listener
inside Houdini (houdini_mcp_server.py, autostarted via scripts/456.py or the
package). Wire protocol is the shared mcp-socket / blender-mcp 1.6.x one.

Run (registered in ZCode config → mcp.servers.houdini):
    python.exe houdini_mcp.py [--port 9877]

Hand-rolled MCP JSON-RPC (newline-delimited over stdio), stdlib only —
same proven pattern as the Blender and Maya servers of the family.
"""

from __future__ import annotations

import json
import os
import socket
import sys

_DEFAULT_PORT = 9877
_CALL_TIMEOUT = 180.0  # seconds; generous for heavy scene queries


def _bridge_port() -> int:
    for i, part in enumerate(sys.argv):
        if part == "--port" and i + 1 < len(sys.argv):
            return int(sys.argv[i + 1])
    env = os.environ.get("HOUDINI_MCP_SOCKET_PORT")
    if env:
        return int(env)
    return _DEFAULT_PORT


def _bridge_call(cmd_type: str, params: dict, port: int) -> dict:
    """One bridge command: fresh socket, JSON in, JSON out (no framing —
    accumulate bytes until they parse whole, matching the mcp-socket wire)."""
    with socket.create_connection(("127.0.0.1", port), timeout=_CALL_TIMEOUT) as sock:
        sock.settimeout(_CALL_TIMEOUT)
        sock.sendall(json.dumps({"type": cmd_type, "params": params}).encode("utf-8"))
        buf = b""
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            buf += chunk
            try:
                return json.loads(buf.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
    raise RuntimeError("Bridge closed the connection before replying")


TOOLS = [
    {"name": "ping_houdini", "bridge": "ping", "params": [],
     "description": ("Check whether the Houdini MCP bridge is running and "
                     "reachable. Returns Houdini version, pid, port, hip "
                     "file, fps, object counts.")},
    {"name": "execute_houdini_code", "bridge": "execute_houdini_code",
     "params": ["code", "undo_chunk"],
     "description": ("Execute a Python code inside the running Houdini "
                     "instance. Returns stdout output and/or the expression "
                     "value. The hou module is preinjected. Each call is one "
                     "undo group ('MCP Socket: ...') when undo tracking is "
                     "on; undo_chunk=False disables undo recording for the "
                     "call.")},
    {"name": "undo_agent_session", "bridge": "undo_agent_session",
     "params": [],
     "description": ("PerformUndo() while the top of the Houdini undo stack "
                     "is an 'MCP Socket' labeled group — walks the agent's "
                     "steps back one by one, stops at the first non-MCP "
                     "entry (the user's own work is never touched).")},
    {"name": "get_scene_info", "bridge": "get_scene_info", "params": [],
     "description": ("Scene overview: hip file, modified flag, fps, frame, "
                     "playback range, object counts, /obj /out /stage /ch "
                     "contexts with children.")},
    {"name": "get_hierarchy", "bridge": "get_hierarchy",
     "params": ["include_sops", "max_nodes"],
     "description": ("/obj node tree: full paths, node types, depth, display "
                     "flag. SOP networks inside geo nodes are skipped unless "
                     "include_sops=true. Capped at max_nodes (default 800).")},
    {"name": "get_screenshot", "bridge": "get_screenshot",
     "params": ["filepath", "mode"],
     "description": ("Screenshot of Houdini. mode 'window' (default): Qt "
                     "grab of the whole main window — works even when the "
                     "window is occluded, good for UI verification. mode "
                     "'flipbook': true viewport render of the Scene Viewer "
                     "with its current flipbook settings (output path and "
                     "frame range follow those settings; may be slow). "
                     "Returns the file path.")},
    {"name": "get_console_log", "bridge": "get_console_log",
     "params": ["last_n", "filter", "stream"],
     "description": ("Ring buffer of stdout/stderr of the whole Houdini "
                     "session (global tee + per-execution captures; last_n "
                     "default 50; filter substring; stream "
                     "'stdout'/'stderr'/both).")},
    {"name": "clear_console_log", "bridge": "clear_console_log",
     "params": [],
     "description": "Clear the console ring buffer."},
    {"name": "list_instances", "bridge": "list_instances", "params": [],
     "description": ("Live Houdini instances from the %TEMP% registry "
                     "(pid, port, version, hip). Multi-instance aware: the "
                     "bridge auto-offsets the port for a second Houdini.")},
    {"name": "export_fbx", "bridge": "export_fbx",
     "params": ["path", "preset", "scope"],
     "description": ("Export FBX with PROKLADKA neutral settings (binary, "
                     "convertunits=1 — meters with correct unit "
                     "declaration): preset 'neutral' (default) or a "
                     "receiver note preset 'maya'/'houdini'/'ue'. scope "
                     "'selected' (default) or 'scene'. Object transform is "
                     "baked through an idempotent mcp_bake_xform node.")},
    {"name": "import_fbx", "bridge": "import_fbx",
     "params": ["path", "container"],
     "description": ("Import FBX via hou.hipFile.importFBX under a subnet "
                     "container (identity transform, default on). Reports "
                     "bbox in meters (Houdini units are meters natively) "
                     "and flags roots over 50 m. No auto-rescale, ever.")},
    {"name": "replay_last_session", "bridge": "replay_last_session",
     "params": [],
     "description": ("Re-run the modifying commands of the last recorded "
                     "agent session from the JSONL log (read-only steps are "
                     "skipped, a failed step does not stop the rest). "
                     "Audits what the agent did; replayed steps are marked "
                     "replay:true in the log.")},
    {"name": "get_session_log_path", "bridge": "get_session_log_path",
     "params": [],
     "description": ("Path of the newest JSONL agent-session log "
                     "(%TEMP%/mcp_socket_houdini/sessions).")},
]


def _tool_schema(tool: dict) -> dict:
    props: dict = {}
    required = []
    for param in tool["params"]:
        if param == "code":
            props[param] = {"type": "string",
                            "description": "Python code to execute in Houdini"}
            required.append(param)
        elif param == "undo_chunk":
            props[param] = {"type": "boolean",
                            "description": "wrap the call in one undo group "
                                           "(default true)"}
        elif param == "include_sops":
            props[param] = {"type": "boolean"}
        elif param == "container":
            props[param] = {"type": "boolean",
                            "description": "group import under a subnet "
                                           "container (default true)"}
        elif param == "preset":
            props[param] = {"type": "string",
                            "enum": ["neutral", "maya", "houdini", "ue"],
                            "description": "export preset (default neutral)"}
        elif param == "scope":
            props[param] = {"type": "string", "enum": ["selected", "scene"],
                            "description": "export scope (default selected)"}
        elif param == "mode":
            props[param] = {"type": "string", "enum": ["window", "flipbook"],
                            "description": "capture target (default window)"}
        elif param == "stream":
            props[param] = {"type": "string",
                            "description": '"stdout" | "stderr" | "" for both'}
        elif param in ("last_n", "max_nodes"):
            props[param] = {"type": "integer"}
        else:  # filepath / path
            props[param] = {"type": "string"}
    return {"type": "object", "properties": props, "required": required}


def _tool_definitions() -> list:
    return [{"name": t["name"], "description": t["description"],
             "inputSchema": _tool_schema(t)} for t in TOOLS]


def _call_tool(name: str, arguments: dict, port: int) -> tuple:
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        raise ValueError(f"Unknown tool: {name}")
    params = {k: v for k, v in (arguments or {}).items() if v is not None}
    reply = _bridge_call(tool["bridge"], params, port)
    if reply.get("status") == "success":
        result = reply.get("result", "")
        text = result if isinstance(result, str) else json.dumps(
            result, indent=2, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text or "(no output)"}]}, False
    return {"content": [{"type": "text",
                         "text": f"Bridge error: {reply.get('message', 'unknown')}"}],
            "isError": True}, True


# ── MCP JSON-RPC loop (newline-delimited over stdio) ───────────────────────

def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, data: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": data})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": message}})


def _handle(msg: dict, port: int) -> None:
    method = msg.get("method", "")
    req_id = msg.get("id")

    if method == "initialize":
        _result(req_id, {
            "protocolVersion": msg.get("params", {}).get("protocolVersion",
                                                         "2025-11-25"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mcp-socket-houdini", "version": "0.1.1"},
        })
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        _result(req_id, {"tools": _tool_definitions()})
    elif method == "tools/call":
        try:
            name = msg["params"].get("name", "")
            content, is_error = _call_tool(name,
                                           msg["params"].get("arguments", {}),
                                           port)
            result = dict(content)
            if is_error:
                result["isError"] = True
            _result(req_id, result)
        except Exception as exc:  # noqa: BLE001 — report as tool error
            _result(req_id, {"content": [{"type": "text",
                                          "text": f"MCP Socket Houdini error: {exc}"}],
                             "isError": True})
    elif req_id is not None:
        _error(req_id, -32601, f"Method not found: {method}")


def main() -> None:
    port = _bridge_port()
    print(f"[MCP_Socket_Houdini_server] started, bridge port {port}",
          file=sys.stderr, flush=True)
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            print(f"[MCP_Socket_Houdini_server] JSON parse error: {exc}",
                  file=sys.stderr, flush=True)
            continue
        try:
            _handle(msg, port)
        except Exception:  # noqa: BLE001 — keep the server alive no matter what
            import traceback
            print(f"[MCP_Socket_Houdini_server] handler error:\n"
                  f"{traceback.format_exc()}", file=sys.stderr, flush=True)
            if msg.get("id") is not None:
                _error(msg["id"], -32603, "internal error")


if __name__ == "__main__":
    main()
