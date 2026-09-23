# -*- coding: utf-8 -*-
"""MCP Socket for Houdini — installer (run with hython, Houdini closed).

    "C:\\Program Files\\Side Effects Software\\Houdini 20.5.278\\bin\\hython.exe" install_mcp_socket_hou.py

Assembles the Houdini package in the user pref dir:

    <pref>/mcp_socket_houdini/python3.11libs/mcp_socket_houdini/{__init__,server,ui}.py
    <pref>/mcp_socket_houdini/toolbar/mcp_socket_houdini.shelf
    <pref>/mcp_socket_houdini/config/Icons/mcp_socket_houdini.svg
    <pref>/packages/mcp_socket_houdini.json
    <pref>/scripts/456.py                      (autostart; overwritten)

Payload files are taken from the directory this installer lives in (unpack
the release zip first). The pref dir comes from hou.getenv — the
authoritative source (Houdini recomputes it at startup; the OS env lies).

Author: Maksim Kovalev. mcp-socket family. License: GPL-3.0.
"""

import json
import os
import shutil
import sys

try:
    import hou
    PREF = hou.getenv("HOUDINI_USER_PREF_DIR")
except ImportError:                      # hython: hou exists; plain python: no
    PREF = None

if not PREF:
    PREF = os.environ.get("HOUDINI_USER_PREF_DIR") or os.path.join(
        os.path.expanduser("~"), "houdini20.5")

SRC = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() \
    else os.getcwd()

FILES = {
    "houdini_mcp_server.py": "python3.11libs/mcp_socket_houdini/server.py",
    "ui.py": "python3.11libs/mcp_socket_houdini/ui.py",
    "__init__.py": "python3.11libs/mcp_socket_houdini/__init__.py",
    "mcp_socket_houdini.shelf": "toolbar/mcp_socket_houdini.shelf",
    "mcp_socket_houdini.svg": "config/Icons/mcp_socket_houdini.svg",
}

PACKAGE_ROOT = os.path.join(PREF, "mcp_socket_houdini")


def main():
    missing = [f for f in FILES if not os.path.exists(os.path.join(SRC, f))]
    if missing:
        print("[mcp_socket_houdini] payload files missing in %s: %s"
              % (SRC, ", ".join(missing)))
        sys.exit(1)

    for src_name, rel_dst in FILES.items():
        dst = os.path.join(PACKAGE_ROOT, rel_dst.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(SRC, src_name), dst)
        print("  installed %s" % dst)

    pkg_dir = os.path.join(PREF, "packages")
    os.makedirs(pkg_dir, exist_ok=True)
    pkg = {"env": [{"MCPSOCKETHOUDINI": PACKAGE_ROOT.replace("\\", "/")}],
           "path": ["$MCPSOCKETHOUDINI"]}
    with open(os.path.join(pkg_dir, "mcp_socket_houdini.json"), "w") as fh:
        json.dump(pkg, fh, indent=2)
    print("  installed %s" % os.path.join(pkg_dir, "mcp_socket_houdini.json"))

    stdio_src = os.path.join(SRC, "houdini_mcp.py")
    if os.path.exists(stdio_src):
        dst = os.path.join(PREF, "scripts", "houdini_mcp.py")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(stdio_src, dst)
        print("  installed %s (stdio MCP server for the client config)" % dst)

    old_456 = os.path.join(PREF, "scripts", "456.py")
    if os.path.exists(old_456):
        with open(old_456, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        if "mcp_socket_houdini" not in content and content.strip():
            print("  NOTE: existing scripts/456.py replaced "
                  "(previous autostart content: %d bytes)" % len(content))

    dst456 = os.path.join(PREF, "scripts", "456.py")
    os.makedirs(os.path.dirname(dst456), exist_ok=True)
    shutil.copyfile(os.path.join(SRC, "456.py"), dst456)
    print("  installed %s" % dst456)

    print("[mcp_socket_houdini] install complete. Restart Houdini — the "
          "bridge autostarts on 127.0.0.1:9877; the MCP Socket shelf "
          "appears via the + button in the shelf tab bar (or is already "
          "visible).")


if __name__ == "__main__":
    main()
