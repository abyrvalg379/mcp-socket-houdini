# -*- coding: utf-8 -*-
# Autostart MCP Socket for Houdini (mcp-socket family) in every Houdini
# session. Author: Maksim Kovalev. Replaces the previous PROKLADKA-era
# bridge autostart. If the module is missing (package not installed),
# prints a notice and keeps Houdini startup safe.
try:
    from mcp_socket_houdini import server
    server.start()
except Exception as _e:
    print("[mcp_socket_houdini] autostart failed:", _e)
    import traceback
    traceback.print_exc()
