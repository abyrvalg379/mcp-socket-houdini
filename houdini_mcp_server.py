# -*- coding: utf-8 -*-
"""MCP Socket for Houdini — TCP bridge running inside Houdini (mcp-socket family).

Same wire protocol as the Blender and Maya branches of the family
(mcp-socket / blender-mcp 1.6.x):

  client sends   {"type": "<command>", "params": {...}}
  server replies {"status": "success", "result": ...}
                 {"status": "error",   "message": "..."}

No framing byte — both sides accumulate bytes and try json.loads until the
document parses whole. Default port 9877 (the historic Houdini bridge port);
busy → auto-offset 9878, 9879, ... (second Houdini).

Commands
    ping                  versions, pid, port, hip, fps, counts
    get_scene_info        hip, modified, fps, frame, playback range, contexts
    get_hierarchy         /obj node tree (paths, types, depth, display flag)
    get_screenshot        mode "window" (default): Qt grab of the main
                          window (occlusion-proof); mode "flipbook": true
                          viewport render with current flipbook settings
    get_console_log       ring buffer of stdout/stderr (global tee + per-exec)
    clear_console_log
    execute_houdini_code  eval→exec with hou preinjected; stdout/stderr
                          captured, optional ``result`` variable returned
                          JSON-safely; one undo group per call
    undo_agent_session    performUndo() while the top of the undo stack is an
                          "MCP Socket" labeled group (honest, capped)
    list_instances        live Houdini instances from the %TEMP% registry
    export_fbx            PROKLADKA neutral export via filmboxfbx ROP: binary,
                          convertunits=1 (meters); object transform baked
                          through an xform node (idempotent "mcp_bake_xform")
    import_fbx            hou.hipFile.importFBX under a subnet container
                          (identity transform), bbox in meters (Houdini units
                          are meters natively), roots over 50 m flagged
    replay_last_session   re-run the modifying commands of the last recorded
                          session; read-only steps are skipped
    get_session_log_path  path of the newest JSONL session log

Threading: the socket lives on a daemon thread, but hou calls belong to
Houdini's main thread. Dispatch = hou.ui.addEventLoopCallback draining a
queue on the main event loop (queueToMainThread / scheduleOnMainThread do
not exist in Houdini 20.5). Headless (hython): handlers run directly.

Session log: every recorded command appends a JSON line to
``%TEMP%/mcp_socket_houdini/sessions/session_<stamp>.jsonl``; a >10 s gap
starts a new file, the 30 newest files are kept.
"""

from __future__ import annotations

import contextlib
import glob
import io
import json
import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from typing import Any, Dict, Optional

import hou

_TAG = "[MCP_Socket_Houdini]"
VERSION = "0.1.0"

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 9877
_PORT_OFFSETS = 10          # busy port → try 9878, 9879, ... (second Houdini)
_CLIENT_IDLE_TIMEOUT = 60.0
_HANDLER_WARN_SECONDS = 30.0

_SESSION_GAP = 10.0         # session-log border, seconds (family-wide)
_SESSIONS_KEEP = 30         # newest JSONL files kept
_HEARTBEAT_MS = 10000       # instance-registry heartbeat
_STALE_SECONDS = 25.0       # registry entry older than this = dead instance
_UNDO_LABEL = "MCP Socket"  # undo group prefix, also the undo_agent_session anchor
_UNDO_MAX_STEPS = 200       # cap for one undo_agent_session call

_server = None              # MCPSocketServer
_thread: Optional[threading.Thread] = None
_window = None              # Qt window singleton
_heartbeat_timer = None
_event_cb_installed = False
_tees: Dict[str, Any] = {}

# ── console ring (global tee + per-execution captures) ───────────────────

_LOG_LOCK = threading.Lock()
_LOG_RING: deque = deque(maxlen=500)   # entries: {"ts", "stream", "text"}


def _log_append(stream: str, text: str) -> None:
    if not text:
        return
    with _LOG_LOCK:
        _LOG_RING.append({"ts": time.strftime("%H:%M:%S"),
                          "stream": stream, "text": text[:4000]})


class _Tee:
    """Write-through stream wrapper feeding the console ring.

    Snippet executions bypass it (redirect_stdout inside the handler), so a
    snippet's prints land in the ring exactly once; system messages and
    prints from other tools flow through the tee.
    """

    def __init__(self, original, stream: str):
        self._original = original
        self._stream = stream
        self._partial = ""

    def write(self, text):
        try:
            self._original.write(text)
        except Exception:  # noqa: BLE001 — never break the real stream
            pass
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            _log_append(self._stream, line)
        return len(text)

    def flush(self):
        try:
            self._original.flush()
        except Exception:  # noqa: BLE001
            pass

    def __getattr__(self, name):
        return getattr(self._original, name)


def _install_log_tee() -> None:
    """Idempotent; re-checked by the heartbeat (self-heal after reloads)."""
    for name in ("stdout", "stderr"):
        current = getattr(sys, name, None)
        if isinstance(current, _Tee) and current._stream == name:
            continue
        tee = _Tee(current, name)
        _tees[name] = tee
        setattr(sys, name, tee)


def _restore_streams() -> None:
    for name, tee in _tees.items():
        try:
            if getattr(sys, name, None) is tee:
                setattr(sys, name, tee._original)
        except Exception:  # noqa: BLE001
            pass
    _tees.clear()


# ── main-thread dispatch ──────────────────────────────────────────────────

_MAIN_QUEUE: "queue.Queue" = queue.Queue()


def _event_loop_callback() -> None:
    """Runs on Houdini's main thread every event-loop iteration; drains the
    job queue. Cheap when idle (one queue.Empty check)."""
    while True:
        try:
            fn, box, ev = _MAIN_QUEUE.get_nowait()
        except queue.Empty:
            return
        try:
            box["res"] = fn()
        except Exception as exc:  # noqa: BLE001 — reported to the caller
            box["err"] = exc
            box["tb"] = traceback.format_exc()
        finally:
            ev.set()


def _run_on_main(fn, timeout: float = 180.0):
    """Run fn() on Houdini's main thread, return its result / re-raise.

    Headless (hython — no UI event loop): run directly on this thread."""
    if not hou.isUIAvailable():
        return fn()
    ev = threading.Event()
    box: Dict[str, Any] = {}
    _MAIN_QUEUE.put((fn, box, ev))
    if not ev.wait(timeout):
        raise RuntimeError("main-thread job timed out after %ss "
                           "(modal dialog open?)" % timeout)
    if "err" in box:
        _log_append("stderr", box.get("tb", str(box["err"])))
        raise box["err"]
    return box["res"]


# ── undo helpers ──────────────────────────────────────────────────────────

def _undo_group(label: str):
    """Context manager: one undo stack entry, or a no-op when undo is
    unavailable (headless / disabled)."""
    try:
        if hou.isUIAvailable() and hou.undos.areEnabled():
            return hou.undos.group(label)
    except Exception:  # noqa: BLE001
        pass
    return contextlib.nullcontext()


def _top_undo_label() -> str:
    try:
        labels = hou.undos.undoLabels()
    except Exception:  # noqa: BLE001
        return ""
    return labels[-1] if labels else ""   # newest last (verified 20.5)


# ── helpers ───────────────────────────────────────────────────────────────

def _jsonify(value: Any) -> Any:
    """Best-effort JSON-safe conversion of an execution result."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _sessions_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "mcp_socket_houdini", "sessions")


def _registry_dir() -> str:
    return os.path.join(tempfile.gettempdir(), "mcp_socket_houdini_instances")


def _hip_info() -> Dict[str, Any]:
    try:
        path = hou.hipFile.path()
    except Exception:  # noqa: BLE001
        path = ""
    try:
        modified = bool(hou.hipFile.hasUnsavedChanges())
    except Exception:  # noqa: BLE001
        modified = None
    return {"hip": path or "(untitled)", "modified": modified,
            "fps": hou.fps(), "frame": hou.frame(),
            "playback_range": list(hou.playbar.playbackRange())}


def _obj_counts() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    obj = hou.node("/obj")
    if obj is None:
        return counts
    for child in obj.children():
        tname = child.type().name()
        counts[tname] = counts.get(tname, 0) + 1
    return counts


# ── session log (JSONL, family architecture) ──────────────────────────────

_RECORD_SKIP = {"ping", "get_console_log", "clear_console_log",
                "list_instances", "get_session_log_path", "replay_last_session"}
_REPLAY_READONLY = {"ping", "get_scene_info", "get_hierarchy", "get_screenshot",
                    "get_console_log", "clear_console_log", "list_instances",
                    "get_session_log_path", "replay_last_session",
                    "undo_agent_session"}

_record_state = {"file": None, "last_ts": 0.0}


def _prune_sessions(keep: int) -> None:
    files = glob.glob(os.path.join(_sessions_dir(), "session_*.jsonl"))
    if len(files) <= keep:
        return
    files.sort(key=os.path.getmtime)
    for path in files[:-keep]:
        try:
            os.remove(path)
        except OSError:
            pass


def _record(command: dict, replay: bool = False) -> None:
    ctype = command.get("type")
    if not isinstance(ctype, str) or ctype in _RECORD_SKIP:
        return
    now = time.time()
    st = _record_state
    if st["file"] is None or (now - st["last_ts"]) > _SESSION_GAP:
        os.makedirs(_sessions_dir(), exist_ok=True)
        _prune_sessions(_SESSIONS_KEEP)
        base = os.path.join(_sessions_dir(),
                            "session_%s.jsonl" % time.strftime("%Y%m%d_%H%M%S"))
        path, n = base, 1
        while os.path.exists(path):     # same-second collision guard
            n += 1
            path = base.replace(".jsonl", "_%d.jsonl" % n)
        st["file"] = path
    st["last_ts"] = now
    entry = {"ts": time.strftime("%H:%M:%S"), "type": ctype,
             "params": command.get("params") or {}, "replay": bool(replay)}
    try:
        with open(st["file"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print("%s session log write failed: %s" % (_TAG, exc))


def _last_session_file() -> Optional[str]:
    files = glob.glob(os.path.join(_sessions_dir(), "session_*.jsonl"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


# ── handlers (all run on Houdini's main thread) ───────────────────────────

def _h_ping(params: dict) -> dict:
    server = _server
    info = _hip_info()
    return {
        "app": "houdini",
        "bridge_version": VERSION,
        "houdini_version": hou.applicationVersionString(),
        "pid": os.getpid(),
        "port": server.port if server else None,
        "hip": info["hip"],
        "modified": info["modified"],
        "fps": info["fps"],
        "frame": info["frame"],
        "playback_range": info["playback_range"],
        "obj_counts": _obj_counts(),
        "ui_available": hou.isUIAvailable(),
        "top_undo": _top_undo_label(),
        "sessions_dir": _sessions_dir(),
    }


def _h_get_scene_info(params: dict) -> dict:
    info = _hip_info()
    contexts = {}
    for ctx in ("/obj", "/out", "/stage", "/ch"):
        node = hou.node(ctx)
        if node is None:
            continue
        children = [(c.name(), c.type().name()) for c in node.children()]
        contexts[ctx] = {"count": len(children), "children": children[:40]}
    return {"hip": info["hip"], "modified": info["modified"],
            "fps": info["fps"], "frame": info["frame"],
            "playback_range": info["playback_range"],
            "obj_counts": _obj_counts(), "contexts": contexts}


def _h_get_hierarchy(params: dict) -> dict:
    include_sops = bool(params.get("include_sops", False))
    max_nodes = int(params.get("max_nodes", 800))
    nodes, truncated = [], False

    def flag(node, flag_):
        try:
            return bool(node.isGenericFlagSet(flag_))
        except Exception:  # noqa: BLE001 — flags not meaningful for this type
            return None

    def walk(node, depth):
        nonlocal truncated
        for child in node.children():
            if len(nodes) >= max_nodes:
                truncated = True
                return
            is_obj = child.type().category().name() == "Object"
            if not is_obj and not include_sops:
                continue
            nodes.append({
                "path": child.path(),
                "name": child.name(),
                "type": child.type().name(),
                "depth": depth,
                "display": flag(child, hou.nodeFlag.Display) if is_obj else None,
            })
            if is_obj and child.children():
                walk(child, depth + 1)

    root = hou.node("/obj")
    if root is not None:
        walk(root, 0)
    return {"returned": len(nodes), "truncated": truncated, "nodes": nodes}


def _h_get_screenshot(params: dict) -> dict:
    """mode 'window' (default): Qt grab of the whole Houdini main window —
    works even when the window is occluded (offscreen widget render; fine
    for UI verification, honest about being a widget render). mode
    'flipbook': the true viewport render through viewer.flipbook() with the
    CURRENT flipbook settings — output path/frames follow those settings and
    a stale range can be slow; the generated file is located by mtime."""
    mode = params.get("mode", "window")
    if mode not in ("window", "flipbook"):
        raise ValueError("mode must be 'window' or 'flipbook'")
    filepath = params.get("filepath") or os.path.join(
        tempfile.gettempdir(), "mcp_socket_houdini",
        "shot_%d.png" % int(time.time() * 1000))
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    if mode == "window":
        win = hou.qt.mainWindow()
        if win is None:
            raise RuntimeError("no Houdini main window")
        pix = win.grab()
        if not pix.save(filepath, "PNG"):
            raise RuntimeError("failed to save %s" % filepath)
        return {"filepath": filepath, "mode": mode,
                "width": pix.width(), "height": pix.height(),
                "note": "Qt widget grab of the main window"}

    viewer = hou.ui.paneTabOfType(hou.paneTabType.SceneViewer)
    if viewer is None:
        raise RuntimeError("no Scene Viewer pane open")
    t0 = time.time()
    viewer.flipbook()
    deadline = t0 + 60.0
    hit = None
    while time.time() < deadline:
        hip_dir = os.path.dirname(hou.hipFile.path() or "") or os.getcwd()
        for root in (hip_dir, os.path.join(hip_dir, "flip"), os.getcwd(),
                     os.path.join(os.getcwd(), "flip"),
                     tempfile.gettempdir()):
            for p in glob.glob(os.path.join(root, "**", "*"), recursive=True):
                try:
                    if (os.path.isfile(p)
                            and p.lower().endswith((".png", ".jpg", ".pic"))
                            and os.path.getmtime(p) >= t0
                            and (hit is None
                                 or os.path.getmtime(p) > os.path.getmtime(hit))):
                        hit = p
                except OSError:
                    pass
        if hit is not None and time.time() - t0 > 3.0:
            break
        time.sleep(1.0)
    if hit is None:
        raise RuntimeError("flipbook produced no file within 60 s (check "
                           "its current settings — output dir and range)")
    return {"filepath": hit, "mode": mode, "width": None, "height": None,
            "note": "viewport flipbook render with current settings"}


def _h_get_console_log(params: dict) -> dict:
    last_n = int(params.get("last_n", 50))
    text_filter = str(params.get("filter", "") or "")
    stream = str(params.get("stream", "") or "")
    with _LOG_LOCK:
        entries = list(_LOG_RING)
    if stream:
        entries = [e for e in entries if e["stream"] == stream]
    if text_filter:
        entries = [e for e in entries if text_filter.lower() in e["text"].lower()]
    return {"total": len(entries), "entries": entries[-last_n:]}


def _h_clear_console_log(params: dict) -> str:
    with _LOG_LOCK:
        _LOG_RING.clear()
    return "console log cleared"


def _h_execute_houdini_code(params: dict) -> dict:
    """eval→exec with hou preinjected; print is the primary channel, an
    optional ``result`` variable is returned JSON-safely. Each call is one
    undo group ("MCP Socket: ...") when undo tracking is on;
    ``undo_chunk=False`` runs with undo recording disabled."""
    code = params.get("code")
    if not code:
        raise ValueError("code is required")
    undo_chunk = params.get("undo_chunk", True)

    out_buf, err_buf = io.StringIO(), io.StringIO()
    namespace = {"__builtins__": __builtins__, "hou": hou}
    ret = None

    def _run_full():
        nonlocal ret
        try:
            ret = eval(compile(code, "<mcp_socket>", "eval"), namespace)
            return
        except SyntaxError:
            pass
        exec(compile(code, "<mcp_socket>", "exec"), namespace)
        ret = namespace.get("result")

    def _wrapped():
        if undo_chunk:
            with _undo_group("%s: code" % _UNDO_LABEL):
                _run_full()
        else:
            try:
                disabler = hou.undos.disabler()
                with disabler:
                    _run_full()
            except Exception:  # noqa: BLE001 — no disabler available
                _run_full()

    try:
        with contextlib.redirect_stdout(out_buf), \
             contextlib.redirect_stderr(err_buf):
            # dispatch already marshals here — no nested _run_on_main
            # (a queued sub-job would deadlock until the timeout)
            _wrapped()
        error_text = ""
    except Exception:  # noqa: BLE001 — traceback is the result
        error_text = traceback.format_exc()
        err_buf.write(error_text)

    _log_append("stdout", out_buf.getvalue())
    _log_append("stderr", err_buf.getvalue())

    return {
        "returned": _jsonify(ret),
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
        "error": bool(error_text),
    }


def _h_undo_agent_session(params: dict) -> dict:
    """performUndo() while the top of the undo stack is an MCP Socket
    labeled group. Houdini records each bridge call as one group, so this
    walks the agent's steps back one by one — capped, and stops at the
    first non-MCP entry (the user's own work is never touched)."""
    if not hou.isUIAvailable():
        raise RuntimeError("no UI: undo stack unavailable in headless mode")
    undone = 0
    stopped_at = ""
    for _ in range(_UNDO_MAX_STEPS):
        top = _top_undo_label()
        if not top:
            break
        if not top.startswith(_UNDO_LABEL):
            stopped_at = top
            break
        hou.undos.performUndo()
        undone += 1
    return {"undone_steps": undone, "stopped_at": stopped_at,
            "message": ("stopped at non-MCP entry: %r" % stopped_at)
                       if stopped_at else "undo stack drained of MCP steps"}


# ── FBX pipeline (PROKLADKA: exporter neutral, receiver converts) ─────────

_FBX_PRESETS = {
    "neutral": ("meters (convertunits=1), binary; the receiver converts to "
                "its own conventions"),
    "maya": ("receiver Maya: FBX-cm numbers with meter declaration; naming "
             "lowercase + _geo/_grp, UV map1"),
    "houdini": ("receiver Houdini: meters native, node suffixes _geo/_vdb/"
                "_abc, attributes on primitives"),
    "ue": ("receiver Unreal: cm (x100 of meters), PascalCase with SM_/SK_/"
           "T_/MI_ prefixes, UV channels"),
}


def _bake_object_transform(geo_node) -> str:
    """For an /obj geo with a transform: insert an xform node after the
    display node and copy t/r/s/p (+ uniform scale) into it, so rotation and
    scale reach the FBX. Idempotent: node 'mcp_bake_xform' is reused.
    (Pattern ported from PROKLADKA's _bake_object_transform.)"""
    disp = geo_node.displayNode()
    if disp is None:
        return geo_node.path()
    bake = geo_node.node("mcp_bake_xform")
    if bake is None:
        bake = geo_node.createNode("xform", "mcp_bake_xform")
        bake.setInput(0, disp)
    for src, dst in (("t", "t"), ("r", "r"), ("s", "s"), ("p", "p")):
        try:
            pt = geo_node.parmTuple(src)
            pd = bake.parmTuple(dst)
            if pt is not None and pd is not None:
                pd.set(pt.eval())
        except Exception:  # noqa: BLE001
            pass
    try:
        us = geo_node.parm("scale")
        if us:
            bake.parm("scale").set(us.eval())
    except Exception:  # noqa: BLE001
        pass
    return bake.path()


def _export_startnode(node) -> str:
    """Selection → filmboxfbx startnode: OBJ geo gets its transform baked,
    a SOP passes through as is."""
    if node.type().category().name() == "Object" and hasattr(node, "displayNode"):
        return _bake_object_transform(node)
    return node.path()


def _h_export_fbx(params: dict) -> dict:
    path = params.get("path")
    if not path:
        raise ValueError("path is required")
    path = os.path.abspath(path)
    preset = params.get("preset", "neutral")
    if preset not in _FBX_PRESETS:
        raise ValueError("unknown preset %r; known: %s"
                         % (preset, sorted(_FBX_PRESETS)))
    scope = params.get("scope", "selected")
    if scope not in ("selected", "scene"):
        raise ValueError("scope must be 'selected' or 'scene'")

    def work():
        out_net = hou.node("/out")
        if out_net is None:
            raise RuntimeError("no /out network")
        if scope == "selected":
            sel = hou.selectedNodes()
            if not sel:
                raise ValueError("scope='selected' but selection is empty")
            start = _export_startnode(sel[0])
        else:
            start = "/obj"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rop = out_net.createNode("filmboxfbx", "mcp_socket_fbx_export")
        try:
            rop.parm("startnode").set(start)
            rop.parm("sopoutput").set(path.replace("\\", "/"))
            ak = rop.parm("exportkind")
            if ak:
                ak.set(0)        # binary (ROP default is ASCII!)
            cu = rop.parm("convertunits")
            if cu:
                cu.set(1)        # meters with correct unit declaration
            tr = rop.parm("trange")
            if tr:
                tr.set(0)        # current frame
            with _undo_group("%s: export_fbx" % _UNDO_LABEL):
                rop.render()
        finally:
            try:
                rop.destroy()
            except Exception:  # noqa: BLE001
                pass

    work()   # dispatch already marshals — no nested _run_on_main (deadlock)
    if not os.path.exists(path):
        raise RuntimeError("FBX export produced no file (see Houdini Console)")
    with open(path, "rb") as fh:
        binary = fh.read(18).startswith(b"Kaydara FBX Binary")
    return {"file": path, "size_bytes": os.path.getsize(path),
            "preset": preset, "scope": scope, "binary": binary,
            "note": _FBX_PRESETS[preset]}


def _h_import_fbx(params: dict) -> dict:
    path = params.get("path")
    if not path or not os.path.exists(path):
        raise ValueError("path does not exist: %r" % path)
    with_container = bool(params.get("container", True))

    def work():
        obj = hou.node("/obj")
        if obj is None:
            raise RuntimeError("no /obj network")

        def all_children(node, acc):
            for c in node.children():
                acc.append(c.path())
                all_children(c, acc)
            return acc

        before = set(all_children(obj, []))
        with _undo_group("%s: import_fbx" % _UNDO_LABEL):
            hou.hipFile.importFBX(path.replace("\\", "/"),
                                  suppress_save_prompt=True,
                                  merge_into_scene=True)
        new = [p for p in all_children(obj, []) if p not in before]
        roots = [hou.node(p) for p in new if p.count("/") == 2]
        roots = [r for r in roots if r is not None]

        bbox_m, oversize = None, []
        for p in new:                     # bbox over ALL new nodes: the FBX
            node = hou.node(p)            # root is a subnet, geometry lives
            if node is None:              # deeper inside it
                continue
            try:
                disp = node.displayNode() if hasattr(node, "displayNode") else None
                geo = disp.geometry() if disp is not None else None
                if geo is None:
                    continue
                bb = geo.boundingBox()
                mn, mx = bb.minvec(), bb.maxvec()
                dims = (mx[0] - mn[0], mx[1] - mn[1], mx[2] - mn[2])
                if bbox_m is None:
                    bbox_m = list(dims)
                else:
                    bbox_m = [max(bbox_m[i], dims[i]) for i in range(3)]
                if max(dims) > 50.0 and p.count("/") == 2:
                    oversize.append(p)
            except Exception:  # noqa: BLE001 — non-geometry nodes
                pass
        if bbox_m is not None:
            bbox_m = [round(d, 4) for d in bbox_m]

        container = None
        if with_container and roots:
            base = os.path.splitext(os.path.basename(path))[0]
            safe = re.sub(r"[^A-Za-z0-9_]", "_", base) or "imported"
            if safe[0].isdigit():
                safe = "_" + safe
            container = obj.createNode("subnet", safe + "_grp")
            for root in roots:
                try:
                    root.moveToGoodLocation(container)
                except Exception:  # noqa: BLE001 — older HOM
                    try:
                        container.addChildNode(root)
                    except Exception:  # noqa: BLE001
                        pass
        return {"nodes": len(new), "roots": [r.path() for r in roots],
                "container": container.path() if container else None,
                "bbox_m": bbox_m, "oversize_roots": oversize}

    result = work()   # dispatch already marshals — no nested _run_on_main
    result.update({
        "file": path,
        "note": ("receiver rule: container subnet identity; Houdini units "
                 "are meters natively; no auto-rescale (check source units "
                 "if sizes are off by x100)"),
    })
    return result


# ── instance registry (multi-Houdini) ─────────────────────────────────────

def _write_registry() -> None:
    try:
        os.makedirs(_registry_dir(), exist_ok=True)
        data = {"pid": os.getpid(),
                "port": _server.port if _server else None,
                "version": VERSION,
                "hip": _hip_info()["hip"],
                "ts": time.time()}
        path = os.path.join(_registry_dir(), "pid_%d.json" % os.getpid())
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError as exc:
        print("%s registry write failed: %s" % (_TAG, exc))


def _remove_registry() -> None:
    try:
        os.remove(os.path.join(_registry_dir(), "pid_%d.json" % os.getpid()))
    except OSError:
        pass


def _h_list_instances(params: dict) -> dict:
    fresh, stale = [], 0
    for path in glob.glob(os.path.join(_registry_dir(), "pid_*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        entry = {"pid": data.get("pid"), "port": data.get("port"),
                 "version": data.get("version"), "hip": data.get("hip"),
                 "age_seconds": round(time.time() - data.get("ts", 0), 1)}
        if entry["age_seconds"] <= _STALE_SECONDS:
            fresh.append(entry)
        else:
            stale += 1
    fresh.sort(key=lambda e: (e["port"] or 0))
    return {"instances": fresh, "stale_seen": stale}


# ── session replay ────────────────────────────────────────────────────────

def _h_replay_last_session(params: dict) -> dict:
    path = _last_session_file()
    if not path:
        return {"replayed": 0, "message": "no session log found"}
    steps = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    steps.append(json.loads(line))
    except (OSError, ValueError) as exc:
        raise RuntimeError("cannot read session log: %s" % exc)

    summary = {"file": path, "total": len(steps), "replayed": 0,
               "skipped_readonly": 0, "skipped_replayed": 0, "failed": []}
    for entry in steps:
        ctype = entry.get("type")
        if ctype in _REPLAY_READONLY:
            summary["skipped_readonly"] += 1
            continue
        if entry.get("replay"):
            summary["skipped_replayed"] += 1
            continue
        command = {"type": ctype, "params": entry.get("params") or {}}
        response = _execute_command(command, replay=True)
        if response.get("status") == "success":
            summary["replayed"] += 1
        else:
            summary["failed"].append({"type": ctype,
                                      "error": response.get("message", "")})
    return summary


def _h_get_session_log_path(params: dict) -> dict:
    return {"dir": _sessions_dir(), "last_file": _last_session_file()}


COMMANDS = {
    "ping": _h_ping,
    "get_scene_info": _h_get_scene_info,
    "get_hierarchy": _h_get_hierarchy,
    "get_screenshot": _h_get_screenshot,
    "get_console_log": _h_get_console_log,
    "clear_console_log": _h_clear_console_log,
    "execute_houdini_code": _h_execute_houdini_code,
    "execute_code": _h_execute_houdini_code,   # old-bridge compatibility
    "undo_agent_session": _h_undo_agent_session,
    "list_instances": _h_list_instances,
    "export_fbx": _h_export_fbx,
    "import_fbx": _h_import_fbx,
    "replay_last_session": _h_replay_last_session,
    "get_session_log_path": _h_get_session_log_path,
}


# ── TCP server ────────────────────────────────────────────────────────────

def _try_parse(buffer: bytes):
    """(command, consumed) or (None, 0) — longest JSON prefix wins."""
    try:
        return json.loads(buffer.decode("utf-8")), len(buffer)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    for i in range(1, len(buffer)):
        try:
            return json.loads(buffer[:i].decode("utf-8")), i
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return None, 0


def _execute_command(command: dict, replay: bool = False) -> dict:
    cmd_type = command.get("type")
    params = command.get("params") or {}
    if not isinstance(cmd_type, str):
        return {"status": "error", "message": "Missing 'type' in command"}
    handler = COMMANDS.get(cmd_type)
    if handler is None:
        return {"status": "error",
                "message": "Unknown command type: %r. Known: %s"
                           % (cmd_type, sorted(set(COMMANDS)))}
    started = time.monotonic()
    try:
        result = handler(params) if isinstance(params, dict) else handler()
    except TypeError as exc:
        return {"status": "error",
                "message": "Bad params for %r: %s" % (cmd_type, exc)}
    except Exception as exc:  # noqa: BLE001 — surface as protocol error
        return {"status": "error", "message": "%s: %s" % (type(exc).__name__, exc)}
    elapsed = time.monotonic() - started
    if elapsed > _HANDLER_WARN_SECONDS:
        print("%s slow command %r: %.1fs" % (_TAG, cmd_type, elapsed))
    _record(command, replay)
    return {"status": "success", "result": result}


class MCPSocketServer:
    """Single-port TCP server marshalling commands to Houdini's main thread."""

    def __init__(self, host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT):
        self.host = host
        self.port = port
        self.preferred_port = port
        self.running = False
        self.socket: Optional[socket.socket] = None
        self._lock = threading.Lock()

    def start(self) -> bool:
        last_error: Optional[OSError] = None
        for offset in range(_PORT_OFFSETS + 1):
            candidate = self.preferred_port + offset
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((self.host, candidate))
            except OSError as exc:
                sock.close()
                last_error = exc
                continue
            self.port = candidate
            break
        else:
            print("%s failed to bind %s:%d-%d: %s"
                  % (_TAG, self.host, self.preferred_port,
                     self.preferred_port + _PORT_OFFSETS, last_error))
            return False
        sock.listen(1)
        sock.settimeout(1.0)
        self.socket = sock
        self.running = True
        print("%s server started on %s:%d (v%s)"
              % (_TAG, self.host, self.port, VERSION))
        return True

    def stop(self) -> None:
        self.running = False
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass
            self.socket = None
        print("%s server stopped" % _TAG)

    def accept_loop(self) -> None:
        while self.running:
            try:
                client, address = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self.running:
                    break
                time.sleep(0.25)
                continue
            threading.Thread(target=self._client_loop, args=(client,),
                             daemon=True).start()

    def _client_loop(self, client: socket.socket) -> None:
        client.settimeout(_CLIENT_IDLE_TIMEOUT)
        buffer = b""
        try:
            while self.running:
                try:
                    data = client.recv(8192)
                except socket.timeout:
                    print("%s client idle, closing" % _TAG)
                    break
                if not data:
                    break
                buffer += data
                command, consumed = _try_parse(buffer)
                if command is None:
                    continue
                buffer = buffer[consumed:]
                self._dispatch(client, command)
        except (ConnectionError, OSError) as exc:
            print("%s connection error: %s" % (_TAG, exc))
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _dispatch(self, client: socket.socket, command: dict) -> None:
        """Run the command on the main thread, reply on the client socket."""
        try:
            response = _run_on_main(lambda: _execute_command(command))
        except Exception as exc:  # noqa: BLE001 — surface as protocol error
            response = {"status": "error",
                        "message": "%s: %s" % (type(exc).__name__, exc)}
        try:
            client.sendall(json.dumps(response).encode("utf-8"))
        except OSError:
            print("%s failed to send reply — client gone" % _TAG)


# ── heartbeat ─────────────────────────────────────────────────────────────

def _heartbeat() -> None:
    _install_log_tee()      # self-heal: reloads may leave stale wrappers
    _write_registry()


def _start_heartbeat() -> None:
    global _heartbeat_timer
    if _heartbeat_timer is not None or not hou.isUIAvailable():
        return
    _heartbeat()
    try:
        from PySide2 import QtCore
        _heartbeat_timer = QtCore.QTimer()
        _heartbeat_timer.timeout.connect(_heartbeat)
        _heartbeat_timer.start(_HEARTBEAT_MS)
    except Exception as exc:  # noqa: BLE001 — headless etc.
        print("%s heartbeat unavailable: %s" % (_TAG, exc))
        _heartbeat_timer = None


def _stop_heartbeat() -> None:
    global _heartbeat_timer
    if _heartbeat_timer is not None:
        try:
            _heartbeat_timer.stop()
        except Exception:  # noqa: BLE001
            pass
        _heartbeat_timer = None


# ── start/stop (called from 456.py or the shelf) — signature stable ───────

def start(port: int = _DEFAULT_PORT) -> bool:
    global _server, _thread, _event_cb_installed
    if _server is not None and _server.running:
        print("%s already running on %s:%d" % (_TAG, _server.host, _server.port))
        return True
    if hou.isUIAvailable() and not _event_cb_installed:
        hou.ui.addEventLoopCallback(_event_loop_callback)
        _event_cb_installed = True
    _server = MCPSocketServer(port=port)
    if not _server.start():
        _server = None
        return False
    _thread = threading.Thread(target=_server.accept_loop, daemon=True)
    _thread.start()
    _install_log_tee()
    _start_heartbeat()
    _write_registry()
    return True


def stop() -> None:
    global _server, _thread, _event_cb_installed
    if _server:
        _server.stop()
        _server = None
    _thread = None
    if _event_cb_installed:
        try:
            hou.ui.removeEventLoopCallback(_event_loop_callback)
        except Exception:  # noqa: BLE001
            pass
        _event_cb_installed = False
    _stop_heartbeat()
    _remove_registry()
    _restore_streams()
    print("%s stopped" % _TAG)
