# MCP Socket for Houdini — v0.1.1

Houdini-ветка семейства mcp-socket. TCP-листенер внутри Houdini с общим
протоколом mcp-socket / blender-mcp 1.6.x. Заменяет старый HTTP-эпохи мост
PROKLADKA (:9877) — порт и место в конфиге сохранены.

## Архитектура

```
MCP-клиент (ZCode) → houdini_mcp.py (stdio) → TCP 127.0.0.1:9877 → mcp_socket_houdini.server (внутри Houdini)
```

- **server.py** — TCP-листенер в демоне-потоке; исполнение в главном потоке
  через `hou.ui.addEventLoopCallback` (очередь задач разбирается колбеком
  event loop'а). В headless (hython) хендлеры исполняются напрямую.
- **houdini_mcp.py** — stdio MCP-сервер (newline-delimited JSON-RPC, stdlib).
- Протокол: `{"type", "params"}` → `{"status": "success", "result"}` /
  `{"status": "error", "message"}`, без фрейминга.

## Установка

Инсталлер собирает Houdini-пакет в преф-директории (при ЗАКРЫТОМ Houdini):

```
"C:\Program Files\Side Effects Software\Houdini 20.5.278\bin\hython.exe" install_mcp_socket_hou.py
```

Что ставится (преф берётся из `hou.getenv("HOUDINI_USER_PREF_DIR")` —
авторитетный источник; env-переменная врёт):

```
<pref>/mcp_socket_houdini/python3.11libs/mcp_socket_houdini/{__init__,server,ui}.py
<pref>/mcp_socket_houdini/toolbar/mcp_socket_houdini.shelf
<pref>/mcp_socket_houdini/config/Icons/mcp_socket_houdini.svg
<pref>/packages/mcp_socket_houdini.json
<pref>/scripts/456.py                        ← автостарт (перезаписывается)
<pref>/scripts/houdini_mcp.py                ← stdio-сервер для конфига клиента
```

Перезапуск Houdini поднимает мост на 127.0.0.1:9877 (занят → 9878, ...).
Полка **MCP Socket** включается через `+` в конце ряда вкладок полок; кнопка
открывает окно моста (аналог N-панели Blender / окна Maya).

## Тулы (13)

| Тул | Что делает |
|-----|------------|
| `ping_houdini` | версии, pid, порт, hip, fps, счётчики /obj, top-undo |
| `execute_houdini_code` | Python с прединжектированным `hou`; stdout/stderr ловятся, `result` возвращается (JSON-safe); один вызов = одна undo-группа «MCP Socket: code» |
| `undo_agent_session` | performUndo(), пока верх стека — группы «MCP Socket…»; кап 200 шагов; чужие записи не трогает |
| `get_scene_info` | hip, modified, fps, кадр, playback range, контексты /obj /out /stage /ch |
| `get_hierarchy` | дерево /obj (пути/типы/глубина/display-флаг), SOP-сети внутри geo — только с `include_sops`, кап 800 |
| `get_screenshot` | `mode="window"` (дефолт) — Qt-граб главного окна (работает при перекрытии); `mode="flipbook"` — честный рендер вьюпорта с ТЕКУЩИМИ настройками флипбука |
| `get_console_log` | ринг stdout/stderr всей сессии (глобальный tee + пер-снипетные захваты) |
| `clear_console_log` | очистка ринга |
| `list_instances` | живые инстансы Houdini из реестра %TEMP% |
| `export_fbx` | filmboxfbx ROP: binary (exportkind=0), convertunits=1 (метры); трансформ объекта бейкается идемпотентной нодой `mcp_bake_xform`; после рендера ROP удаляется |
| `import_fbx` | `hou.hipFile.importFBX` под subnet-контейнер (identity); bbox в метрах (родные единицы Houdini), корни >50 м — флаг |
| `replay_last_session` | повтор модифицирующих команд из JSONL-лога |
| `get_session_log_path` | путь свежайшего JSONL-лога |

JSONL-лог: `%TEMP%\mcp_socket_houdini\sessions\` (гэп 10 с = новый файл,
хранятся 30; ping/консоль/реестр/пути не пишутся; реплей скипает read-only).

## Скрытые грабли (выловлено на 20.5.278)

1. `hou.ui.queueToMainThread` / `scheduleOnMainThread` в 20.5 НЕ существуют.
   Маршаллинг = `hou.ui.addEventLoopCallback` + очередь. Хендлерам ЗАПРЕЩЁН
   вложенный `_run_on_main` — подзадача будет ждать следующей итерации event
   loop, которая не наступит, пока текущая не вернётся = дедлок до таймаута.
2. `hou.setSelectedNodes` НЕ существует; `node.setSelected(True)` без
   `clear_all`-кварга; очистка выделения — `hou.clearAllSelected()`.
3. `hou.applicationBuildVersion` НЕ существует (есть `applicationVersionString`).
4. `hou.playbar.frame_range()` НЕ существует — `hou.playbar.playbackRange()`.
5. `SceneViewer.flipbook(viewport, settings, open_dialog)` — НЕТ параметров
   output_dir/frame_range; `hou.ui.flipbookSettings` тоже нет; plain
   `viewer.flipbook()` рендерит с ТЕКУЩИМИ настройками (запазданный диапазон
   = долго) и кладёт файл по настройкам.
   **Скриншот окна: `QWidget.grab()` главного окна даёт ПОЛНОСТЬЮ ЧЁРНЫЙ
   кадр** (Houdini рисует нативно, Qt backing store пуст) — проверено живьём
   2026-09-23. Рабочий путь: физический захват `QScreen.grabWindow(winId)`
   (окно активно — контент есть); минус — перекрытая часть покажет то, что
   поверх.
6. `hou.FlipbookSettings` абстрактный — конструктора нет.
7. `hou.undos`: `UserUndoBlock` НЕ существует, но `hou.undos.group(label)` —
   контекст-менеджер «один экшен на undo-стеке»; `undoLabels()` — новейшая
   ПЕРВАЯ (`labels[0]`; вживую доказано 2026-09-23: последний вызов моста
   стоял первым, самый старый шаг сессии юзера — последним. Ранняя пометка
   «новейшая последняя» была ошибочной верификацией и делала
   `undo_agent_session` бесполезным — тул сверялся с самым СТАРЫМ шагом);
   `performUndo()` откатывает шаг.
8. filmboxfbx — ROP-категория (не SOP): пармы `startnode`/`sopoutput`
   (20.5; старые: `soppath`/`file`), `exportkind=0` (binary — ДЕФОЛТ ROP =
   ASCII, Blender его не читает!), `convertunits=1` (метры; =0 даёт ×0.01).
9. `hou.hipFile.importFBX` заворачивает импорт в OBJ-subnet — bbox искать
   ВНУТРИ (мерить по всем новым нодам, не только корням).
10. В headless hython OBJ-примитивы (torus/sphere/box) НЕ зарегистрированы —
    null/geo/cam работают. `createNode` из ЧУЖОГО потока = «Invalid node
    type name» (реестр HOM нитевой) — наш dispatch главный поток даёт
    автоматически.
11. hython из Git Bash: HOME указывает на /c/... — префы уезжают в
    `~/houdini20.5`; переопределять `HOME="C:\Users\mkova\Documents"`.
    456.py подхватывается и из CWD — не держать 456.py в рабочей папке.
12. PySide2 (НЕ PySide6; `hutil.Qt` сам выбирает биндинг); parent окна —
    `hou.qt.mainWindow()`.
13. Полка — ТОЛЬКО XML-пакетом: `hou.shelves.newShelf/newTool` живёт в памяти
    сессии, при выходе пишется в default.shelf, тула-сироты остаются в DB
    (инцидент PROKLADKA 2026-08-30). Скрипты тулов — сырой текст с CDATA.
14. `hou.BBox`: `minvec()`/`maxvec()`/`sizevec()`; `centervec()`/`xmin` НЕТ.
15. Старый мост-совместимость: команда `execute_code` — алиас
    `execute_houdini_code`.
16. **ИНЦИДЕНТ 2026-09-23: `replay_last_session` вешает мост насмерть** в
    живом GUI. Симптомы: тулов отвечает таймаутом, дальше все коннекты —
    10061 (refused) при ЖИВОМ слушателе; `list_instances`-реестр (пишется
    QTimer'ом главного потока) замер полупустым; Houdini-процесс жив, окна
    «отвечают», но питон-мир главного потока стоит. py-spy dump: MainThread
    застрял в кадре `json.dump → _write_registry` (heartbeat), accept-нить
    формально в `accept()`, но ядро шлёт RST на новые SYN — прижизненный
    GIL-затык питон-мира Houdini. Лечение ТОЛЬКО рестарт Houdini.
    Предположительная цепочка: replay — длинная команда на главном потоке,
    event-loop callback не тикает на idle-Houdini (юзер не шевелит мышь) →
    очередь стоит → TCP-нить в ev.wait(180). Фикс-попытка: `_kick_event_loop`
    (no-op Qt-событие после постановки в очередь) + backlog 1→4 —
    ТРЕБУЕТ ВЕРИФИКАЦИИ на рестарте (повторить idle-пинг и осторожный replay
    на сохранённой сцене). Диагностика на будущее: `py-spy dump --pid
    <pid>` (есть в системе, 0.4.2).
17. **NumPy 2.x из user-site → СЕГФОЛТ Houdini (инцидент 2026-09-24).**
    `AppData\Roaming\Python\Python311\site-packages` — ОБЩИЙ user-site всех
    питонов 3.11 на машине: Maya 2025 держит там numpy 2.4.6/scipy/websockets
    (mayapy подтверждает: берёт numpy из Roaming — удалять НЕЛЬЗЯ), а
    Houdini-питон 3.11 подхватывает тот же каталог → numpy 2.x затирает
    родной 1.24.4 → при старте warning «compiled using NumPy 1.x», в работе
    Segmentation fault (дамп crash.<hip>.hip в %TEMP%\houdini_temp).
    Решение: пакет
    `<pref>/packages/zpython_no_user_site.json` = `{"env":
    [{"PYTHONNOUSERSITE": "1"}]}` — Houdini-only, Maya не затронута.
    Грабля проверки: hython из Git Bash с HOME=/c/Users/mkova ищет префы в
    `~/houdini20.5` и пакеты НЕ видит — тестировать с
    `HOME="C:\Users\mkova\Documents"`.

## Отключение старого моста PROKLADKA

Старый мост жил в `PROKLADKA/work/houdini_mcp_server.py` и стартовался
строкой в `scripts/456.py`. Инсталлер перезаписывает 456.py — старый мост
больше не стартует (файл PROKLADKA не трогается; откат = вернуть старую
строку exec в 456.py). Порт 9877 общий — второй мост на нём не поднимется,
пока живёт первый (в живой сессии новый мост возьмёт 9878 до рестарта).
