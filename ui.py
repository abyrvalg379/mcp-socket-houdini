# -*- coding: utf-8 -*-
"""MCP Socket for Houdini — Qt window (PySide2), the analogue of the
Blender N-panel / Maya bridge window. Opened from the shelf tool:

    import mcp_socket_houdini.ui
    mcp_socket_houdini.ui.show_window()
"""

from __future__ import annotations

import json
import os
import tempfile
import time

import hou
try:
    from PySide2 import QtCore, QtGui, QtWidgets
except ImportError:  # future Houdini versions
    from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore

from . import server as _bridge

VERSION = _bridge.VERSION

_window = None               # BridgeWindow singleton


class BridgeWindow(QtWidgets.QWidget):
    def __init__(self, parent=None):
        flags = QtCore.Qt.Window if parent else None
        super(BridgeWindow, self).__init__(parent, flags)
        self.setWindowTitle("MCP Socket Houdini %s" % VERSION)
        self.resize(380, 540)

        lay = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel("<b>MCP Socket Houdini %s</b>" % VERSION)
        lay.addWidget(header)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        undo_btn = QtWidgets.QPushButton("Undo Agent Work")
        undo_btn.clicked.connect(lambda: self.run_handler("undo_agent_session"))
        lay.addWidget(undo_btn)

        box = QtWidgets.QGroupBox("Agent Sessions")
        v = QtWidgets.QVBoxLayout(box)
        row = QtWidgets.QHBoxLayout()
        replay_btn = QtWidgets.QPushButton("Replay Last Session")
        replay_btn.clicked.connect(
            lambda: self.run_handler("replay_last_session"))
        logpath_btn = QtWidgets.QPushButton("Copy Log Path")
        logpath_btn.clicked.connect(
            lambda: self.run_handler("get_session_log_path"))
        row.addWidget(replay_btn)
        row.addWidget(logpath_btn)
        v.addLayout(row)
        lay.addWidget(box)

        box = QtWidgets.QGroupBox("Console Log")
        v = QtWidgets.QVBoxLayout(box)
        self.console = QtWidgets.QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(200)
        f = QtGui.QFont("Consolas")
        f.setFixedPitch(True)
        f.setStyleHint(QtGui.QFont.Monospace)
        self.console.setFont(f)
        v.addWidget(self.console)
        row = QtWidgets.QHBoxLayout()
        save_btn = QtWidgets.QPushButton("Save to File")
        save_btn.clicked.connect(self._save_console)
        clear_btn = QtWidgets.QPushButton("Clear")
        clear_btn.clicked.connect(lambda: self.run_handler("clear_console_log"))
        refresh_btn = QtWidgets.QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_all)
        for b in (save_btn, clear_btn, refresh_btn):
            row.addWidget(b)
        v.addLayout(row)
        lay.addWidget(box)

        box = QtWidgets.QGroupBox("Pipeline FBX (PROKLADKA)")
        v = QtWidgets.QVBoxLayout(box)
        row = QtWidgets.QHBoxLayout()
        self.preset_cb = QtWidgets.QComboBox()
        self.preset_cb.addItems(sorted(_bridge._FBX_PRESETS))
        self.scope_cb = QtWidgets.QComboBox()
        self.scope_cb.addItems(["selected", "scene"])
        row.addWidget(QtWidgets.QLabel("preset"))
        row.addWidget(self.preset_cb)
        row.addWidget(QtWidgets.QLabel("scope"))
        row.addWidget(self.scope_cb)
        v.addLayout(row)
        export_row = QtWidgets.QHBoxLayout()
        self.export_path = QtWidgets.QLineEdit()
        export_btn = QtWidgets.QPushButton("Export...")
        export_btn.clicked.connect(self._do_export)
        export_row.addWidget(self.export_path)
        export_row.addWidget(export_btn)
        v.addLayout(export_row)
        import_row = QtWidgets.QHBoxLayout()
        self.import_path = QtWidgets.QLineEdit()
        import_btn = QtWidgets.QPushButton("Import...")
        import_btn.clicked.connect(self._do_import)
        import_row.addWidget(self.import_path)
        import_row.addWidget(import_btn)
        v.addLayout(import_row)
        lay.addWidget(box)

        lay.addStretch(1)
        self.refresh_all()

    # ── actions ───────────────────────────────────────────────────────────

    def run_handler(self, name, params=None):
        """Run a bridge handler in-process and surface the result in the log."""
        try:
            response = _bridge._execute_command({"type": name,
                                                 "params": params or {}})
        except Exception as exc:  # noqa: BLE001
            response = {"status": "error", "message": repr(exc)}
        if response.get("status") == "success":
            res = response.get("result")
            text = res if isinstance(res, str) else json.dumps(
                res, ensure_ascii=False)
            _bridge._log_append("stdout", "%s %s" % (name, text or "ok"))
        else:
            _bridge._log_append("stderr", "%s FAILED: %s"
                                % (name, response.get("message", "")))
        self.refresh_all()

    def _default_dir(self):
        try:
            return os.path.dirname(hou.hipFile.path() or "") or os.path.expanduser("~")
        except Exception:  # noqa: BLE001
            return os.path.expanduser("~")

    def _do_export(self):
        path = self.export_path.text().strip()
        if not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Export FBX", self._default_dir() + "/untitled.fbx",
                "FBX (*.fbx)")
            if not path:
                return
            self.export_path.setText(path)
        self.run_handler("export_fbx", {
            "path": path,
            "preset": self.preset_cb.currentText(),
            "scope": self.scope_cb.currentText()})

    def _do_import(self):
        path = self.import_path.text().strip()
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Import FBX", self._default_dir(), "FBX (*.fbx)")
            if not path:
                return
            self.import_path.setText(path)
        self.run_handler("import_fbx", {"path": path})

    def _save_console(self):
        with _bridge._LOG_LOCK:
            entries = list(_bridge._LOG_RING)
        outdir = os.path.join(tempfile.gettempdir(), "mcp_socket_houdini")
        os.makedirs(outdir, exist_ok=True)
        path = os.path.join(
            outdir, "console_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
        with open(path, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write("%s [%s] %s\n" % (e["ts"], e["stream"], e["text"]))
        clipboard = QtGui.QGuiApplication.clipboard()
        clipboard.setText(path)
        _bridge._log_append("stdout",
                            "console saved: %s (path in clipboard)" % path)
        self.refresh_all()

    def refresh_all(self):
        srv = _bridge._server
        port = srv.port if srv and srv.running else None
        state = "running" if port else "STOPPED"
        try:
            hip = os.path.basename(hou.hipFile.path() or "(untitled)")
        except Exception:  # noqa: BLE001
            hip = "?"
        self.status.setText("port %s | pid %d | %s<br>hip: %s"
                            % (port or "-", os.getpid(), state, hip))
        with _bridge._LOG_LOCK:
            entries = list(_bridge._LOG_RING)[-12:]
        self.console.setPlainText("\n".join(
            "%s [%s] %s" % (e["ts"], e["stream"],
                            e["text"].replace("\n", " | ")[:220])
            for e in entries))
        bar = self.console.verticalScrollBar()
        bar.setValue(bar.maximum())


def show_window():
    """Singleton window opener (shelf entry point)."""
    global _window
    if _window is not None:
        try:
            _window.close()
            _window.deleteLater()
        except RuntimeError:
            pass
        _window = None
    try:
        parent = hou.qt.mainWindow()
    except Exception:  # noqa: BLE001 — headless / odd layouts
        parent = None
    win = BridgeWindow(parent)
    win.show()
    win.raise_()
    _window = win
    return win
