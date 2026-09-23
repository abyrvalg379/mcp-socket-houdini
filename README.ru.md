# MCP Socket for Houdini

Локальный MCP-мост для Side Effects Houdini — houdini-ветка семейства [mcp-socket](https://github.com/abyrvalg379/mcp-socket). TCP-листенер живёт внутри Houdini и говорит на том же wire-протоколе, что Blender- и Maya-мосты (blender-mcp 1.6.x совместимый) — любой MCP-клиент управляет Houdini через типизированные тулы.

Часть набора **STUKACH — Pipeline Asset Validation System**.

**Автор:** Maksim Kovalev · **Версия:** 0.1.0 · **Лицензия:** GPL-3.0

*English documentation: [README.md](README.md)*

## Как это работает

```
MCP-клиент → houdini_mcp.py (stdio) → TCP 127.0.0.1:9877 → mcp_socket_houdini.server (внутри Houdini)
```

Каждая команда маршаллится в главный поток Houdini через очередь задач и
`hou.ui.addEventLoopCallback` (`queueToMainThread` в Houdini 20.5 не
существует); сокет живёт в демоне-потоке. Протокол — один JSON-документ на
запрос, без фрейминга:

```
→ {"type": "ping", "params": {}}
← {"status": "success", "result": {...}}
```

## Тулы (13)

| Тул | Назначение |
|-----|------------|
| `ping_houdini` | версия Houdini, pid, порт, hip-файл, fps, счётчики объектов |
| `execute_houdini_code` | Python внутри Houdini — `hou` прединжектирован, stdout/stderr ловятся, опциональная переменная `result` возвращается (JSON-safe); один вызов = одна undo-группа «MCP Socket» |
| `undo_agent_session` | performUndo(), пока верх undo-стека — группы «MCP Socket»; чужие записи не трогаются |
| `get_scene_info` | hip-файл, флаг изменений, fps, кадр, playback range, контексты /obj /out /stage /ch |
| `get_hierarchy` | дерево нод /obj (пути, типы, глубина, display-флаг), с капом |
| `get_screenshot` | режим `window` (дефолт): Qt-граб главного окна, работает при перекрытии; режим `flipbook`: честный рендер вьюпорта с текущими настройками флипбука |
| `get_console_log` | кольцевой буфер stdout/stderr всей сессии (глобальный tee + пер-снипетные захваты) |
| `clear_console_log` | очистка кольца |
| `list_instances` | живые инстансы Houdini из реестра в `%TEMP%` |
| `export_fbx` | PROKLADKA-нейтральный экспорт через filmboxfbx ROP: binary, `convertunits=1` (метры), трансформ объекта бейкается идемпотентной нодой `mcp_bake_xform` |
| `import_fbx` | `hou.hipFile.importFBX` под subnet-контейнер (identity-трансформ); bbox в отчёте в метрах (родные единицы Houdini), корни >50 м помечаются; никаких магических множителей |
| `replay_last_session` | повтор модифицирующих команд последней записанной сессии из JSONL-лога |
| `get_session_log_path` | путь свежайшего JSONL-лога сессий |

Лог сессий: каждая записанная команда дописывает JSON-строку в
`%TEMP%/mcp_socket_houdini/sessions/`; гэп >10 с = новый файл, хранятся 30
свежайших. `replay_last_session` скипает read-only шаги и помечает реплеиные
`replay: true`.

Кнопка **MCP Socket** на полке (ставится Houdini-пакетом, включается через
`+` в конце ряда вкладок полок) открывает окно с теми же секциями, что
N-панель Blender: шапка со статусом, Undo Agent Work, Agent Sessions,
просмотр лога консоли, Pipeline FBX.

## Установка

1. Скачайте `mcp_socket_houdini_v*.zip` из
   [последнего релиза](https://github.com/abyrvalg379/mcp-socket-houdini/releases/latest)
   и распакуйте.
2. При ЗАКРЫТОМ Houdini запустите инсталлер через hython:

   ```
   "C:\Program Files\Side Effects Software\Houdini 20.5.278\bin\hython.exe" install_mcp_socket_hou.py
   ```

   Он собирает Houdini-пакет в преф-директории (резолв через
   `hou.getenv("HOUDINI_USER_PREF_DIR")`), ставит полку и хук автостарта
   (`scripts/456.py`).
3. Перезапустите Houdini — мост слушает `127.0.0.1:9877` (занят → 9878, ...).

### Подключение любого MCP-клиента

Ноль зависимостей сверх стандартной библиотеки Python:

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

`--port 9878` или переменная `HOUDINI_MCP_SOCKET_PORT` выбирают второй
инстанс Houdini.

## Безопасность

Только localhost, без аутентификации; `execute_houdini_code` исполняет
произвольный Python в вашей Houdini — это инструмент одной рабочей станции
для связки «художник + агент», а не сервис. Порт наружу не выставлять.

## Смежные туры

- [mcp-socket](https://github.com/abyrvalg379/mcp-socket) — Blender-ветка семейства
- [mcp-socket-maya](https://github.com/abyrvalg379/mcp-socket-maya) — Maya-ветка семейства
- [PROKLADKA](https://github.com/abyrvalg379/prokladka) — FBX-мост Blender ↔ Maya ↔ Houdini ↔ UE
- [STUKACH](https://github.com/abyrvalg379/STUKACH) / [STUKACH_Maya](https://github.com/abyrvalg379/STUKACH_Maya) — пайплайн-валидаторы ассетов
