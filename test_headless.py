# -*- coding: utf-8 -*-
"""Headless smoke test for houdini_mcp_server.py (run with hython).

Starts the bridge on port 9990 inside this hython process, then talks to it
over raw TCP like the stdio server would. Exits non-zero on failure.
"""

import glob
import json
import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import houdini_mcp_server as m  # noqa: E402

PORT = 9990
OUT = {"pass": [], "fail": []}


def call(ctype, params=None, timeout=60):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=timeout)
    s.sendall(json.dumps({"type": ctype, "params": params or {}}).encode())
    buf = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
        try:
            return json.loads(buf.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    raise RuntimeError("no reply for %r" % ctype)


def check(name, cond, detail=""):
    (OUT["pass"] if cond else OUT["fail"]).append("%s %s" % (name, detail))
    print(("PASS " if cond else "FAIL ") + name + (" | " + detail if detail else ""))


def main():
    assert m.start(PORT), "server failed to start"

    r = call("ping")
    check("ping", r.get("status") == "success"
          and r["result"]["bridge_version"] == m.VERSION
          and r["result"]["port"] == PORT, json.dumps(r.get("result", {}))[:120])

    r = call("get_scene_info")
    check("get_scene_info", r.get("status") == "success"
          and "contexts" in r["result"], "")

    r = call("get_hierarchy")
    check("get_hierarchy", r.get("status") == "success", "")

    r = call("execute_houdini_code", {"code": (
        "geo = hou.node('/obj').createNode('geo', 'mcp_test_geo')\n"
        "box = geo.createNode('box')\n"
        "box.setDisplayFlag(True)\n"
        "box.setRenderFlag(True)\n"
        "result = geo.path()")})
    geo_path = (r.get("result", {}) or {}).get("returned")
    check("execute createNode", r.get("status") == "success"
          and geo_path == "/obj/mcp_test_geo", str(geo_path))

    # old-bridge alias
    r = call("execute_code", {"code": "result = 40 + 2"})
    check("execute_code alias", r.get("status") == "success"
          and r["result"]["returned"] == 42, "")

    # error path is in-band
    r = call("execute_houdini_code", {"code": "1/0"})
    check("execute error in-band", r.get("status") == "success"
          and r["result"]["error"] is True, "")

    tmp = os.path.join(os.environ.get("TEMP", "/tmp"),
                       "mcp_socket_houdini", "hython_roundtrip.fbx")
    if os.path.exists(tmp):
        os.remove(tmp)
    r = call("execute_houdini_code", {"code": (
        "hou.clearAllSelected()\n"
        "hou.node('/obj/mcp_test_geo').setSelected(True)\n"
        "result = [n.path() for n in hou.selectedNodes()]")})
    r = call("export_fbx", {"path": tmp, "preset": "neutral", "scope": "selected"})
    ok = r.get("status") == "success" and os.path.exists(tmp)
    binary = r.get("result", {}).get("binary", False) if ok else False
    check("export_fbx", ok and binary,
          "" if not ok else json.dumps(r["result"])[:140])

    r = call("import_fbx", {"path": tmp, "container": True})
    imp = r.get("result", {}) or {}
    check("import_fbx", r.get("status") == "success"
          and imp.get("container"), json.dumps(imp)[:160])

    r = call("undo_agent_session", {})
    check("undo_agent_session headless refuses",
          r.get("status") == "error", str(r.get("message", ""))[:80])

    r = call("get_console_log", {"last_n": 5})
    check("get_console_log", r.get("status") == "success", "")

    r = call("list_instances")
    inst = (r.get("result", {}) or {}).get("instances", [])
    check("list_instances", r.get("status") == "success"
          and any(i.get("port") == PORT for i in inst), json.dumps(inst)[:120])

    r = call("get_session_log_path")
    last = (r.get("result", {}) or {}).get("last_file")
    check("session log exists", bool(last and os.path.exists(last)), str(last))

    r = call("unknown_command")
    check("unknown command error", r.get("status") == "error", "")

    # replay must not crash on read-only content
    r = call("replay_last_session", {})
    check("replay_last_session", r.get("status") == "success",
          json.dumps(r.get("result", {}))[:100])

    m.stop()

    print("\nRESULT: %d pass, %d fail" % (len(OUT["pass"]), len(OUT["fail"])))
    sys.exit(1 if OUT["fail"] else 0)


if __name__ == "__main__":
    main()
