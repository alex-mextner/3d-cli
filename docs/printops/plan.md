# PrintOps: самокорректирующаяся система печати для парка принтеров — исследование и план

Срез на 2026-10-14. Только чтение: код не менялся, принтерам ничего не отправлялось, HA только читался.
Сопутствующие отчёты: **[oss.md](oss.md)** (разбор OSS/SaaS, ~45 КБ, со ссылками) и **[rfid.md](rfid.md)** (RFID/NFC и станция на ESPHome).
Непроверенное помечено **[INFERENCE]**.

---

## 0. Коротко

- В репозиториях нет ни учёта катушек, ни очереди, ни сушки, ни `3d-bot`/`3d-lib`/`3d-api`. Ближе всего к этому: `3d inventory` (плоский JSON без катушек и адресов), `3d procurement` (офлайн-план закупок по BOM), `3d printer` + `print-preflight` (U1 и K1 SE; гейт сверяет цвет и материал слотов с G-code) и ручной `gen/queue.json` в babushkin-dom.
- **`projects/3d-bot` нигде не найден**: ни в `~/xp`, ни на Odroid, ни в бэкапе MSI, ни на GitHub. Это решение **D1**.
- Всё 3D-шное в HA уже версионируется, но в **чужом публичном** репозитории `alex-mextner/ha-configs-rs-bg-stanka_vraza`. На Odroid лежат незакоммиченные правки `packages/3d_printers.yaml` от 2026-10-14 и три `.bak`. Перенос в 3d-cli нужно делать с сохранением истории.
- Рекомендуемая архитектура: **Spoolman** хранит vendor/filament/spool, **PrintOps DB** (SQLite в `3d-api`) хранит всё остальное: адреса склада, ledger расхода, сушки, инциденты, принтеры и их компоненты, калибровки, ТО, очередь и историю, закупки. Пишет в Spoolman один писатель — `3d-api`. Решения D2 и D3.
- Гейты: существующий `print-preflight` расширяется проверками «материал против учёта»: температура, тип/цвет/партия в голове, сушка, остаток, сопло, калибровки, ТО. Они срабатывают на трёх рубежах: при слайсе (Orca post-processing), при отправке (`3d printer start` и `3d-api`) и при старте не через нас (watch → уведомление или пауза по политике).
- Интерфейсы (§19, эпик #356): база знаний ТО «симптом → причина → проверка → исправление → профилактика», связанная с ТО, инцидентами и журналом настроек; TUI при запуске `3d` в терминале; тот же набор функций в HA; все операции работают без LLM, локальная Ollama — необязательное дополнение.
- Цвет (§20, эпик #410): тестер смесей из загруженных катушек печатается за минуты, фото даёт измеренные цвета катушек и смесей, они хранятся как калибровки; смешанная деталь обязана показывать цвет на стенках — `print-preflight` даёт FAIL плоской смешанной детали.

---

## 1. Что уже есть (prior work)

### 1.1 GitHub, alex-mextner/*
Поиск `gh search issues` по ключам inventory, spool, filament, queue, history, drying, 3d-bot, 3d-lib, 3d-api, spoolman, smart speaker, home assistant, склад, катушк, сушк, очеред, колонк. По русским ключам ничего нет. Spoolman упоминается только в #71 (архитектурный зонтик) и косвенно.

| Где | Что | Статус |
|---|---|---|
| 3d-cli [#90](https://github.com/alex-mextner/3d-cli/issues/90) | Epic: операции с принтерами (status/upload/start/control/preflight) | open |
| 3d-cli [#91](https://github.com/alex-mextner/3d-cli/issues/91), [#93](https://github.com/alex-mextner/3d-cli/issues/93), [#94](https://github.com/alex-mextner/3d-cli/issues/94), [#95](https://github.com/alex-mextner/3d-cli/issues/95), [#96](https://github.com/alex-mextner/3d-cli/issues/96), [#101](https://github.com/alex-mextner/3d-cli/issues/101) | printer-devices.yaml, Creality stock, v1 commands, safety, preflight (слоты против `filament_colour`), gcode-bounds | closed (PR [#132](https://github.com/alex-mextner/3d-cli/pull/132) merged) |
| 3d-cli [#92](https://github.com/alex-mextner/3d-cli/issues/92) | Moonraker-адаптер U1 (слоты из `print_task_config`) | open (основное влито в #132) |
| 3d-cli [#97](https://github.com/alex-mextner/3d-cli/issues/97) | printer v2: pause/resume/**history**/**slots**/snapshot | open |
| 3d-cli [#98](https://github.com/alex-mextner/3d-cli/issues/98) | адаптеры Bambu LAN/MQTT, PrusaLink, OctoPrint | open |
| 3d-cli [#100](https://github.com/alex-mextner/3d-cli/issues/100) | start ждёт реального старта | open |
| 3d-cli [#68](https://github.com/alex-mextner/3d-cli/issues/68) | durable intent + сверка с живым состоянием при upload/start (**ledger заданий**) | open |
| 3d-cli [#142](https://github.com/alex-mextner/3d-cli/issues/142) | offline-записи в printer-devices (принтеры, под которые только слайсим) | open |
| 3d-cli [#145](https://github.com/alex-mextner/3d-cli/issues/145) / PR [#151](https://github.com/alex-mextner/3d-cli/pull/151) | `3d printer watch`: события аномалий → stdout/exec/tg/omp | PR open (ветка feat/printer-watch, +2.5k строк, `lib/printer_ops/watch*.py`) |
| 3d-cli [#150](https://github.com/alex-mextner/3d-cli/issues/150) | printer vision: спагетти, залипшая камера, NPU-вердикты U1 → watch | open (ветка feat/printer-vision) |
| 3d-cli [#138](https://github.com/alex-mextner/3d-cli/issues/138) / PR [#144](https://github.com/alex-mextner/3d-cli/pull/144) | gcode-resume | PR open |
| 3d-cli [#102](https://github.com/alex-mextner/3d-cli/issues/102), [#112](https://github.com/alex-mextner/3d-cli/issues/112), [#114](https://github.com/alex-mextner/3d-cli/issues/114), [#116](https://github.com/alex-mextner/3d-cli/issues/116) | print-pipeline из babushkin-dom; цвета столов → рабочие головы; plate-view с таблицей заправки; colour-mix | open |
| 3d-cli [#152](https://github.com/alex-mextner/3d-cli/issues/152) | bridge_max_mm на принтер | open |
| garage-band [#23](https://github.com/alex-mextner/garage-band/issues/23) | babushkin-dom v5 (3 филамента на U1) | open |
| aisis [#3](https://github.com/alex-mextner/aisis/issues/3) | эпик «локальная голосовая колонка» | open |
| aisis [#8](https://github.com/alex-mextner/aisis/issues/8) | голосовые аккаунты, привязанные к пользователям HA | open |
| esphome-ir [#14](https://github.com/alex-mextner/esphome-ir/issues/14), [#15](https://github.com/alex-mextner/esphome-ir/issues/15) | своё wake-word «эй Милош» | open |
| ha-configs-rs-bg-stanka_vraza | живой HA-репозиторий Odroid (PUBLIC); 3D-коммиты: f4e5132 «add 3d printer monitoring and ingress proxy» … 51485cf «K1 SE online sensor…» | см. §3 |
| ultra-3d-printer | KlipperScreen-тикеты (#7, #19 closed) | не про учёт |

### 1.2 ROADMAP 3d-cli
- §31 `3d print`: в планах «Multi-printer / queue … assign by capability» и «Material check against registry + inventory (§32) before starting».
- §32 «Material procurement & inventory»: учёт катушек (material/color/brand/remaining g/location), списание по оценке слайсера, `3d materials inventory list|add|use`, ссылки на закупку. **Не реализовано**: есть только `3d inventory` (materials|parts, плоский `inventory.json`) и `3d procurement` (BOM → shortage, без сети).
- §29: перевести `lib/` в импортируемый пакет `threed` с console-script. Не сделано (`[tool.uv] package = false`, `bin/3d` добавляет `lib/` в `sys.path`).

### 1.3 Тикеты `task` (HYP)
По spool/катушк/склад/филамент/очеред/сушк/3d-bot/3d-lib/3d-api/spoolman/колонк/speaker тикетов нет. По printer: HYP-1679/1681/1699/1700 отменены, их перенесли в GitHub (#90, #107, #142, #145). HYP-1678 про Orca Local-Z к теме не относится.

### 1.4 Архив omp (prompts/recaps)
В истории промптов про учёт, очередь, сушку, бота, API и колонку ничего нет. Есть только контекст babushkin-dom («докупить три катушки», «не нити, а филамент» → garage-band AGENTS.md) и переезд HA/ботов на Odroid (2026-10-07…10).

### 1.5 Связанная прошлая проектная работа (в HA-репозитории)
`docs/3d-printing-product-vision-and-journey.md` v0.1 от 2026-09-19 описывает цепочку `Request → Spec → Model → SliceArtifact → Quote → Order → ProductionAttempt → QCReport → Fulfilment`. Там уже есть принцип «G-code hash ≠ attempt id» и вывод, что HA — это адаптер, а не БД заказов. Этот документ стоит перенести в 3d-cli, а PrintOps job/attempt должен совпадать с его `ProductionAttempt`.

---

## 2. Состояние репозитория 3d-cli

| Вопрос | Факт |
|---|---|
| 3d-lib отделён? | **Нет.** `lib/` — плоский набор модулей в `sys.path` (`from registries.inventory import …`), его кладёт туда `bin/3d`. Пакета, `pyproject` и build backend нет (ROADMAP §29: «activate after lib/ → threed»). `libs/` — это BOSL2 и NopSCADlib (OpenSCAD), к Python-либе не относится. |
| projects/3d-bot | **Не найден.** В `3d-cli/projects/` лежит только `low-poly-cat`. `find` по `~` (до глубины 6–7), Odroid `/home/ultra`, `/mnt/{wd500,storage,msi-backup-ro}` и `gh search code` ничего не дали. Боты на Odroid: только `hypercal-bot`. |
| Принтеры: устройства | `~/.config/3d-cli/printer-devices.yaml` — **вне репозитория**: u1 (moonraker 192.168.0.26:7125, axis_limits, 30 trusted_macros, bridge_max_mm 25) и k1se (creality-stock 192.168.0.27). Модели — `lib/data/printers.yaml` (Bambu A1/A1 mini, Prusa…; U1 и K1 SE нет, см. [#84](https://github.com/alex-mextner/3d-cli/issues/84)). |
| printer_ops | `base.py` (PrinterStatus, `Slot{index,label,present,color,material,vendor,sub_type}`, safety-состояния), `moonraker.py` (U1 `print_task_config` → слоты; `filament_detect` **не читается**), `creality.py`/`ws.py` (K1 stock), `preflight.py`: thumbnails, printer_free, **slots** (цвет/материал против G-code), halves, overhangs, slice_warnings, head_sequence, visible, macros, moves. |
| watch / vision / gcode-resume | Ветки feat/printer-watch (PR #151), feat/printer-vision и feat/gcode-resume (PR #144) не влиты. watch даёт event-bus аномалий: это готовый источник событий для PrintOps. |
| Материалы | `lib/data/materials.yaml` + user/project-слои: свойства для прочности и массы, без температурных окон, абразивности и гигроскопичности. |
| inventory/procurement | `3d inventory` (materials/parts, name/quantity/unit/location/notes); `3d procurement` (BOM + inventory → план покупок; без цен и сети). |
| Очередь и заправка (babushkin-dom) | `gen/queue.json`: printers[] → entries{file, status printed/now/queued/waiting/tester, load {T1:"синий",…}, what}. `gen/plates.json`: heads, owned (катушки на руках с hex), plates (recipes Full Spectrum), bought. `gen/queue_report.py` берёт время и граммы из футера G-code и генерирует QUEUE.md, README и shopping_list.md. `docs/full-spectrum/tools/load.json` — заправка для инструмента смешивания. **Это рабочий прототип очереди и закупок; его модель стоит перенести в PrintOps как есть.** |
| Web | `lib/web/server.py`: FastAPI `3d web`, 18 эндпоинтов, SSE. Стек тот же, что для `3d-api`. |
| lint | `3d lint` (repo-правила и правила моделей .scad). Отдельный dev-gate: ruff, mypy, pytest, shellcheck (CI/pre-commit). |

---

## 3. Home Assistant (Odroid 192.168.0.39, `/home/ultra/homeassistant`): 3D-артефакты

Это git-репозиторий `origin = github.com/alex-mextner/ha-configs-rs-bg-stanka_vraza` (**PUBLIC**). `main` = `origin/main`, но рабочее дерево грязное.

| Артефакт | Что | Версионирование |
|---|---|---|
| `packages/3d_printers.yaml` (507 строк) | REST-сенсор `printer_u1_raw` (Moonraker), `rest_command` pause/resume/cancel/estop U1, скрипты K1/U1 (pause/resume/cancel/estop/fans_off), `script.printer_send_notification`, ~30 template-сенсоров (status/progress/remaining/file/layer/temps/filament_used/z/fan/active_tool) и binary (online/printing/paused/problem) для K1 SE и U1, 6 автоматизаций (уведомления о состоянии, вентиляторы K1 после печати, offline, перегрев K1/U1) | tracked; **M** (незакоммиченные правки 2026-10-14: slicer-ETA для U1, `completed`/`stopped` для ha_creality_ws, `not_from unavailable`), плюс 3 untracked `.bak-20261014-*` |
| `packages/u1_print_inspector.yaml` | `rest_command` u1_inspector_status / u1_inspect_layer → `u1-camera-proxy:8090` | tracked |
| `packages/printer_vision.yaml` + регионы `go2rtc.yaml`, `3d.docker-compose.yaml`, `ui-3d-printers.yaml` | **генерируются** `3d ha vision` из `fleet/printers.yaml` для каждого принтера с камерой (`watch.vision.camera`): сенсоры vision (p/q, события, printing/paused/alive из `GET /v1/printers/NAME/vision`), кадр для phone-алертов, поток go2rtc, прокси камеры, карточки; «призрак» остатка печати на камере (`u1_camera_ghost`, `lib/vision_ghost.py`) — только у принтеров с калибровкой камеры (сейчас U1, у K1 SE калибровки нет → без призрака). См. docs/commands/ha.md | tracked (генерируется) |
| `ui-3d-printers.yaml` (232 строки) | YAML-дашборд «3D Printers» (`/3d-printers/overview`): gauge, entities K1/U1, кнопки, `custom:webrtc-camera` u1_camera, `custom:u1-inspector-card`, `custom:u1-share-card` (public_url dataplicity), history-graph, ссылки на ingress | tracked |
| `configuration.yaml` (фрагменты) | `ingress:` k1_se_printer → `printer-proxy:8080/k1se`, u1_printer → `/u1`; `lovelace.dashboards.3d-printers` (mode yaml); `lovelace.resources` u1-inspector-card.js, u1-share-card.js | tracked (файл общий) |
| `scripts/u1_mjpeg_proxy.py` (332) | MJPEG-прокси камеры U1, поллинг monitor.jpg, «будильник» монитора через Moonraker MQTT publish, оверлей | tracked |
| `scripts/u1_print_inspector.py` (372), `scripts/u1_gcode_index.py` (256) | read-only инспектор: телеметрия Moonraker, индекс G-code по слоям, `/status.json`, `/layers/N` | tracked |
| `scripts/u1_share.py` (362), `requirements/u1-share.txt` | подписанные ссылки на камеру с истечением | tracked |
| `www/u1-share/*` (6 файлов) | карточки Lovelace и viewer для публичных ссылок | tracked |
| `tests/u1_print_inspector/*` | pytest + browser checks | tracked |
| `docs/u1-camera-sharing.md`, `docs/u1-print-inspector*.md`, `docs/3d-printing-product-vision-and-journey.md` | документация | tracked |
| `printer-proxy.nginx.conf` (98) | nginx для ingress: K1 web + ws 9999 (sub_filter), U1 Fluidd, `/u1/go2rtc`, `/u1/u1-camera` | tracked |
| `go2rtc.yaml` | потоки `k1se_camera` (mjpeg :8080), `u1_camera` (ffmpeg h264 из u1-camera-proxy); целиком 3D-шный | tracked |
| `ha.docker-compose.yaml` (сервисы) | `go2rtc` (alexxit/go2rtc:1.9.9), `printer-proxy` (nginx:1.27-alpine), `u1-camera-proxy` (образ HA, `python /config/scripts/u1_mjpeg_proxy.py`, env U1_CAMERA_*) | tracked (файл общий) |
| `custom_components/moonraker` | копия [marcolivierarsenault/moonraker-home-assistant](https://github.com/marcolivierarsenault/moonraker-home-assistant) 1.13.4 (submodule `submodules/moonraker-home-assistant`); config entry «Snapmaker U1» | tracked (копия) и submodule |
| `custom_components/ha_creality_ws` | [3dg1luk43/ha_creality_ws](https://github.com/3dg1luk43/ha_creality_ws) v0.9.7 (submodule); config entry «Creality K1 SE»; `www/k_printer_card.js` | **IGNORED** в custom_components, pin только через submodule |
| config entries (`.storage`) | `go2rtc`, `generic` «u1_camera_proxy», `moonraker` «Snapmaker U1», `ha_creality_ws` «Creality K1 SE» | UI-конфиг, не в git (`.storage`) |
| automations.yaml, scripts.yaml | 3D-шного нет (все автоматизации в пакете) | — |
| cron, systemd на хосте | 3D-шного нет | — |

### 3.1 Предлагаемая раскладка в монорепо

```
3d-cli/
  packages/3d-lib/            # python-пакет `threed` (бывший lib/): geometry, gcode, printer_ops, printops-domain
  apps/cli/                   # bin/3d + lib/commands (или оставить в корне до §29)
  services/3d-api/            # FastAPI + SQLite (PrintOps), Dockerfile, compose-фрагмент
  projects/3d-bot/            # Telegram-бот (D1: откуда брать / писать заново)
  integrations/
    homeassistant/
      packages/3d_printers.yaml, u1_print_inspector.yaml, printops.yaml (новый)
      dashboards/ui-3d-printers.yaml
      scripts/u1_mjpeg_proxy.py, u1_print_inspector.py, u1_gcode_index.py, u1_share.py
      www/u1-share/*, www/printops/* (новые карточки)
      custom_sentences/ru/printops.yaml   # голосовые интенты Assist
      custom_components/printops/         # фаза 4: своя интеграция
      proxy/printer-proxy.nginx.conf
      go2rtc/go2rtc.yaml
      compose/3d.docker-compose.yaml      # go2rtc, printer-proxy, u1-camera-proxy, 3d-api, spoolman
      snippets/configuration.3d.yaml      # ingress + lovelace dashboard/resources (проверяются линтером)
      vendor.lock                         # пины: moonraker-home-assistant 1.13.4, ha_creality_ws v0.9.7, webrtc 3.6.1
      tests/u1_print_inspector/*
      docs/*.md
      deploy.py                           # `3d ha deploy`
    esphome/spool-station.yaml            # станция NFC/весы (§10)
    orca/post-process.sh                  # хук гейта при экспорте G-code (§9)
  fleet/printers.yaml                     # бывший ~/.config/3d-cli/printer-devices.yaml + головы/сопла/AMS (§7)
```

**Перенос с историей:** `git filter-repo --paths-from-file 3d-paths.txt` по клону HA-репозитория, затем `git merge --allow-unrelated-histories` в 3d-cli под `integrations/homeassistant/`. **До этого** нужно закоммитить текущие правки на Odroid (`3d_printers.yaml` M) и убрать `.bak`.

**Деплой `3d ha deploy [--dry-run] [--host odroid]`:**
1. `rsync --checksum` файлов из манифеста в `/home/ultra/homeassistant/…` (те же пути, что сейчас).
2. Cutover в HA-репозитории (по D9 — submodule): добавить 3d-cli submodule'ом, старые tracked-пути убрать `git rm --cached` и закоммитить (сам `.gitignore` уже tracked-файлы не игнорирует), затем добавить их в `.gitignore` с комментарием «source of truth: 3d-cli integrations/homeassistant». Проверка: после деплоя `git status` HA-репо чистый. В `/config/.3d-cli-deployed` пишется SHA коммита 3d-cli и время деплоя.
3. Сниппеты `configuration.yaml` не копируются, только сверяются: деплой падает, если в живом конфиге нет ingress, dashboard или resources (`lovelace` и `ingress` в packages не выносятся [INFERENCE]).
4. Compose: в главный `ha.docker-compose.yaml` добавляется `include: [3d.docker-compose.yaml]` (Compose ≥ 2.20) или `-f`. Проверить версию docker compose на Odroid.
5. `docker exec homeassistant-homeassistant-1 python -m homeassistant --script check_config -c /config`, затем `homeassistant.reload_all` через API. `docker compose up -d` перезапускает только изменённые 3D-сервисы.
6. Проверки после деплоя: сущности `sensor.printer_*` не `unavailable`, `/3d-printers/overview` отдаётся, go2rtc отдаёт `u1_camera`.

---

## 4. OSS: выводы (подробности и ссылки — [oss.md](oss.md))

- **Spoolman** ([Donkie/Spoolman](https://github.com/Donkie/Spoolman), MIT, ★2.9k, v0.27.0 от 2026-09-25): vendor/filament/spool, REST + WS, `PUT /spool/{id}/use` и `/measure`, extra-поля, `lot_nr`, Prometheus, с v0.27 реестр NFC по UID. **Пробелы:** `location` — плоская строка ≤64 символов (вложенные локации — open [#1202](https://github.com/Donkie/Spoolman/issues/1202)); сушки нет (PR #711 в draft); закупок нет (#207); принтеров, заданий и аудита нет; **авторизации нет**.
- **Moonraker `[spoolman]`** держит одну активную катушку и каждые 5 с делает `/use`. Мульти-тул — через `SET_ACTIVE_SPOOL` в T-макросах. history хранит `spool_ids` и `filament_weights` по инструментам.
- **U1**: сток читает только проприетарные метки Snapmaker (`filament_detect`, 4 канала FM175xx). [Extended firmware paxx12](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware) (GPL-3.0, ★1.05k) даёт Spoolman SpoolLink (`SET_SPOOL_ID LANE=E0..E3`), OpenRFID (Bambu/Creality/OpenSpool/TigerTag…) и `POST /printer/filament_detect/set`. → решение **D4**.
- **K1 SE stock**: ws:9999 (`usedMaterialLength`, `materialStatus`), без Moonraker. Без root Spoolman на принтере не поставить, только серверный адаптер. CFS — только с апгрейд-китом.
- **Bambu**: [ha-bambulab](https://github.com/greghesp/ha-bambulab) (MIT) отдаёт по треям AMS тип, цвет, remain, tag_uid, tray_uuid. Влажности у AMS lite нет. Метки подписаны RSA. Мосты в Spoolman: OpenSpoolMan, bambu-spoolman, SpoolmanSync. Принтеры чужие (сосед, Борис), нужно их согласие и LAN-доступ или облачный read-only.
- **OctoPrint SpoolManager/FilamentManager** заброшены. **Bambuddy** (AGPL) — только Bambu. **FDM Monster** — без филамента. **Printago, Filarack, SimplyPrint** покрывают больше, но проприетарные. **Готового OSS на весь наш парк нет.**
- **Метки**: для своих меток — NTAG215 + OpenSpool JSON (читают U1-extended, телефоны, PN532). OpenPrintTag (Prusa, NFC-V) сильнее по модели, но нужен ридер ISO15693 (подробнее в §10).

---

## 5. Архитектура и модель данных PrintOps

### 5.1 Хранилища
- **Spoolman** (docker на Odroid, в LAN, без внешнего доступа) — источник правды для vendor, filament и spool (identity, `remaining`, `lot_nr`, `price`, `location`-строка).
- **PrintOps DB** (SQLite WAL на HDD Odroid, владелец — `3d-api`). Ссылается на `spoolman_spool_id` и `spoolman_filament_id`. Все изменения идут через события (append-only `events`) и проекции.
- **Правило одного писателя:** в Spoolman пишет только `3d-api`, кроме Moonraker `[spoolman]` на принтерах, где он включён: K1 SE (после root 2026-10-15, `docs/printers/k1se-root-upgrade.md`) и U1, если его включат (D4). Тогда `3d-api` по заданиям такого принтера не делает `/use`, а только сверяет; `spool load` лишь ставит active spool Moonraker (одноголовые принтеры, §9.6 в `oss.md`).
- **Альтернатива (D2):** своё хранилище с экспортом и импортом в формате Spoolman. Плюс — одна БД и транзакции. Минус — теряем Fluidd, SpoolLink, Disane87-HA и Bambu-мосты.

### 5.2 Сущности (PrintOps)
```
location(id, path "WH:A/S2/B3/04", kind warehouse|shelf|box|slot|printer_slot|dryer|dry_box, parent_id, tag_uid?, qr?, capacity, humidity_sensor_entity?)
spool_state(spool_id, location_id, bound_printer_slot?, opened_at, last_dried_at, dry_state, confidence, updated_at)   # проекция
ledger(id, at, spool_id, delta_g, source gcode_estimate|moonraker_live|printer_reported|weigh|voice|manual|purge, confidence, job_id?, note)
drying_session(id, dryer_id, started_at, ended_at, temp_c, target_h, rh_before, rh_after, source) + drying_spool(session_id, spool_id, weight_before, weight_after)
purchase(id, shop, ordered_at, received_at, total, currency) + purchase_line(purchase_id, filament_id, qty, unit_price, url) → received spools
printer(id, model, adapter, host, …) + head/nozzle/ams-slot components (§7)
job(id, printer_id, artifact_sha256, file, source 3d|slicer|touchscreen|cloud, queued_at, started_at, ended_at, status, attempt_of?, order_ref?) + job_tool(job_id, tool, slot, spool_id, g_est, g_reported, mm_reported)
incident(…) (§6), calibration(…) (§7–8), maintenance(…) (§7)
anomaly(id, at, kind, subject, evidence, status open|ack|resolved)
```

### 5.3 Самокоррекция (reconcile)
Каждые N минут и на каждое событие `3d-api` сводит независимые сигналы по каждой катушке и слоту:
1. **Идентичность в слоте:** RFID принтера (U1 `filament_detect`/`print_task_config`, Bambu `tag_uid`/`tray_uuid`, CFS `rfid`=material id) ↔ привязка в учёте ↔ голос, станция, скан. Расхождение → `anomaly(slot_mismatch)` и вопрос пользователю. RFID побеждает в идентичности, только если метка уникальна (Bambu tray_uuid — да, CFS — нет).
2. **Цвет и тип** в слоте (`print_task_config.filament_color_rgba`, AMS `color`) ↔ filament катушки: ΔE2000 > порога → anomaly.
3. **Расход:** оценка из G-code по инструменту (до печати; Moonraker history `filament_weights` и Bambu 3MF — тоже **оценки** слайсера из метаданных файла, а не факт) → наблюдаемый расход после печати: `filament_used`/`usedMaterialLength`/Bambu remain с учётом отмены или сбоя (отменённое на первом слое задание не списывает полную оценку). Взвешивание (`/measure`) — абсолютная привязка, сбрасывает накопленную ошибку. Каждый ledger-ряд несёт `source` и `confidence`, остаток показывается как интервал. Тесты учёта покрывают отменённые и упавшие задания.
4. **Аномалии:** расход ≫ оценки (сопли, пробка, недоэкструзия по факту), остаток < 0, катушка одновременно в двух местах, «печать на неизвестной катушке», катушка в сушилке дольше X, принтер печатает материалом, которого по учёту нет.
5. **Учёт ошибок пользователя:** голосовые поправки — тоже события. Повторяющееся расхождение источника X → понижение его доверия (простая байесовская оценка по каждому источнику).

### 5.4 Адаптеры парка
`klipper-moonraker` (U1, хакспейс-Klipper), `creality-ws` (K1 SE stock), `bambu-mqtt` (P1S, A1 mini; LAN developer mode или чтение через ha-bambulab), `ha-bridge` (любой принтер, который уже есть в HA), `offline` (принтеры хакспейса без сети: задания и расход вручную или по G-code). Общий интерфейс — существующий `printer_ops.base.Adapter`, расширенный на history/slots/events.

---

## 6. Структурированные заметки и инциденты по филаментам и катушкам

**Цель:** систематически анализировать проблемы по филаменту, производителю, партии, принтеру и голове.

```
incident(
  id, at, reported_by (voice|telegram|cli|station|watch|vision), confidence,
  kind ∈ {clog, stringing, oozing_blobs, warping, delamination/layer_split, under_extrusion, over_extrusion,
          moisture_popping, brittleness/snap, poor_bed_adhesion, layer_shift, colour_off, runout, tangle,
          heat_creep, nozzle_drag, other},
  severity ∈ {cosmetic, part_lost, print_lost, hardware_risk},
  printer_id, head/slot, spool_id, filament_id, vendor_id, lot_nr,      # снимок на момент инцидента
  job_id, layer, z_mm, feature (wall/infill/bridge/support…),
  nozzle_temp, bed_temp, chamber_temp, speed_mm_s, volumetric_mm3_s, fan_pct,   # из G-code/телеметрии на момент
  days_since_dried, dry_box_rh, room_rh,
  resolution_kind ∈ {dried, temp_change, speed_change, retraction_change, cleaned_nozzle, replaced_nozzle,
                     glue/plate, recalibrated, discarded_spool, none},
  resolution_text, resolved_at, photos[], linked_calibration_id?, tags[]
)
```
- **Автозаполнение контекста:** при создании (голосом «на U1 голова 3 пошли сопли») `3d-api` сам подтягивает текущий job, слой, температуры и скорость по G-code-индексу (переиспользовать `u1_gcode_index.py`), катушку и партию из привязки, часы с последней сушки и влажность бокса.
- **Автоинциденты:** из watch (runout, ошибки экструдера, перегрев) и vision (спагетти) → `incident(draft)` с подтверждением.
- **Аналитика:** `3d printops incidents stats --by vendor|filament|lot|printer|head --since 90d` — частоты на кг или час печати, а не абсолютные числа. «Партия X: 3 пробки на 1,2 кг против 0,2 в среднем у вендора». Карточка в HA, оценка вендоров (vendor scorecard) идёт в подбор при закупке (§13).
- **Свободные заметки** к катушке и филаменту (`note`) остаются отдельно. Инцидент — это структура, заметка — просто текст.

---

## 7. Модель и БД принтеров (парк): компоненты, калибровки, поломки, ТО

### 7.1 Статика (git, `fleet/printers.yaml`) и динамика (DB)
- **YAML (переносится из `~/.config/3d-cli/printer-devices.yaml` и закрывает #142, #84):** id, model, adapter/host/ports, owner (свой / сосед / Борис / хакспейс), access (full | read-only | offline), axis_limits, trusted_macros, bridge_max_mm, **topology**: heads[] (label T1..T4 ↔ index 0..3, extruder name), ams[] (unit → slots), enclosure, chamber_heater, bed plates.
- **DB, события компонентов:** nozzle installs (head, diameter, material brass|hardened|ruby|tungsten, type std|HF, installed_at, removed_at, reason), PTFE/hotend/extruder gear, plates; счётчики (часы печати, метры филамента, количество смен голов — по истории заданий).

### 7.2 Каталог калибровок (декларативно, `fleet/calibrations.yaml`)
| Калибровка | Скоуп | Триггеры инвалидации | Как получить | Куда применять |
|---|---|---|---|---|
| Flow ratio | head × filament (× lot по политике) × nozzle | новый filament, новая партия (policy), замена сопла, смена диаметра | Orca-style flow-тест (модель + per-object flow), U1 `SM_PRINT_FLOW_CALIBRATE` (авто) | профиль филамента Orca (`filament_flow_ratio`) |
| Pressure advance / K | head × filament × nozzle | то же + замена экструдера/PTFE | PA pattern / tower (Klipper `TUNING_TOWER`), K1 — K-factor | Orca `pressure_advance` и/или Klipper `SET_PRESSURE_ADVANCE` + сохранение |
| Temp tower | filament (× lot) | новый filament | башня + `M104` по высоте (пост-процесс) | диапазон в filament (min/max/opt) |
| Max volumetric speed | filament × hotend | новый filament, HF-сопло | тест расхода | Orca `filament_max_volumetric_speed` |
| Retraction | head × filament | замена PTFE/hotend | тест ретракта | профиль |
| Input shaper | printer | ремонт кинематики, ремни, перенос | Klipper `SHAPER_CALIBRATE` / K1 auto | `SAVE_CONFIG` (с подтверждением) |
| Z-offset / bed mesh | printer × plate | замена стола/сопла, ремонт | Klipper, U1 auto | Klipper |
| Смещения голов U1 (XY/Z) | U1 | снятие/замена любой головы, удар | калибровка Snapmaker | прошивка (D: только по процедуре вендора) |
| Rotation distance / e-steps | head | замена шестерни или мотора экструдера | отметка 100 мм | Klipper cfg |
| Ремни, натяжение | printer | ТО, шум | частотомер или приложение | журнал |
| Цвет филамента (`filament_color`, §20.4) | filament (× lot) | новая партия | пластинка-эталон тестера смесей + фото (§20.5), колориметр | hex для модели смесей вместо hex Spoolman |
| Цвет смеси (`mix_color`, §20.4) | printer × смесь (`mix`) | новая запись `filament_color` её филаментов | тестер смесей + фото (§20.5) | журнал; строка `mix_calibration` (§20.7) |

Запись: `calibration(id, kind, scope{printer,head,nozzle_install_id,filament_id,lot_nr}, at, method, gcode_sha, values{…}, evidence[photos, VLM-оценка], applied_to[{target, before, after, at, confirmed_by}], valid_until?, superseded_by)`.

### 7.3 Поломки и ТО
- `maintenance(id, printer_id, component, kind clean|lube|replace|tighten|repair|firmware_update, at, by, parts_used[spare_part_id], notes, triggers_calibrations[])` — после записи сами создаются требования калибровок (§8).
- `fault(id, printer_id, component, at, symptom, root_cause, status, fixed_by_maintenance_id)`. Пример: «U1 T2 не работает» сейчас живёт текстом в `queue.json`/`plates.json`. Нужно перенести в fault + `head.usable=false`, это нужно планировщику и #112.
- **Регламент ТО** (`fleet/maintenance-rules.yaml`): каждые N часов, M метров или K смен голов → задача ТО (смазка направляющих, чистка сопел, проверка ремней, PTFE). Счётчики считаются по истории заданий.

### 7.4 Журнал изменений настроек принтеров (settings history, #264)
Требование: append-only история **всех** изменений калибровок и настроек принтера — что, старое → новое значение, когда, кто и какой командой, источник, привязка к принтеру/голове/столу/катушке, откат.

**Чего не хватает в #167/#227 и #166.** `calibration` (#227) хранит измерение, но строка мутабельна (`status`, `superseded_by`); «старое значение» есть только в `applied_to`, а его сейчас никто не пишет (apply не реализован); нет PID, firmware/конфигов, `trusted_macros`/fleet, профилей слайсера; нет источника `backup`; нет команды-инициатора; нет отката. `maintenance.kind = firmware_update` (#166) — только заметка, без diff и хешей. События `events` append-only, но вида `settings.changed` нет, и нет запроса «история принтера».

**Схема** (миграция `settings_change`, триггеры запрещают UPDATE/DELETE, как у `events`):
```
settings_change(id, uid UNIQUE,                     -- uid (UUIDv7) = ключ идемпотентного импорта
  at, recorded_at, printer, head?, plate?, nozzle_install_id?, spool_id?, filament_id?, lot_nr?,
  kind,        -- flow_ratio|pressure_advance|input_shaper|z_offset|bed_mesh|pid_hotend|pid_bed|… (каталог §7.2)
               -- | firmware | config_file | trusted_macros | fleet | slicer_profile | other
  key,         -- input_shaper.shaper_freq_x, printer.cfg, fleet/printers.yaml#k1se.adapter, …
  old, new,    -- JSON; для config_file — {sha256, bytes}
  status,      -- measured | measured_not_applied | applied | restored | observed (дрейф) | rolled_back
  source,      -- accelerometer | photo | vlm | manual | backup | firmware_auto | drift_scan | import
  actor, command,                  -- токен/пользователь и argv команды
  evidence[],                      -- фото, логи, gcode path + sha256
  calibration_id?, maintenance_id?, reverts?)        -- reverts = id отменяемой записи
config_blob(sha256 PK, content)                      -- содержимое конфигов, content-addressed; путь — в settings_change.key
```
Одно действие может дать несколько строк (например, `shaper_freq_x` и `shaper_freq_y`), их связывает общий `command` и `at`.

**Откат** — это не правка истории, а новая запись: `3d settings rollback <id>` берёт `old` записи `<id>`, применяет его тем же путём с подтверждением (§8.2 п.4: diff профиля, `SAVE_CONFIG` только idle + `--yes`, конфиг — запись блоба по sha256) и пишет строку `status=applied, source=manual, reverts=<id>`. Перед применением живое значение (или sha256 файла) сверяется с `new` записи `<id>`: если после неё было другое изменение, откат отказывает с diff (409), перезапись только с явным `--force`.

**Кто пишет:** `3d calibrate record|apply` (#227, #168/#249, #257) — measured / applied со ссылкой на `calibration_id`; `3d printops maintenance` (#166) с `firmware_update` — снимок конфигов до/после; `drift_scan` в `3d-api` (раз в час, только чтение: Moonraker `/server/files/config/*`, K1 stock — по SSH/HTTP при доступе) — изменённый sha256 даёт `observed` с diff; правки `fleet/*.yaml` и `printer-devices.yaml` (`trusted_macros`, адаптер) — через `3d fleet`/`3d printer` или по git-хуку; применение профиля Orca — diff JSON.

**Чтение:** `3d settings history --printer P [--kind K] [--since D]`, `GET /v1/settings/history?printer=&kind=&since=&cursor=` (новые сверху, курсор по `id`, scope `read`); событие `settings.changed` (§11.2). Ответ по умолчанию — метаданные и sha256 без содержимого конфигов; содержимое и diff — `GET /v1/settings/blobs/{sha256}` со scope `printer-control`. Секреты (`api_key`, `token`, `password`, `[secrets]`-ссылки Moonraker) вырезаются до сохранения блоба.

**Без 3d-api:** CLI дописывает те же записи в `cli.paths.data_dir()/printer-history/<printer>.jsonl` (`~/.local/share/3d-cli/printer-history/`; одна строка — одна запись, `uid` обязателен) под `flock` на файл, одним `write` с `O_APPEND` + `fsync`. Буфер, начатый до #264 в `~/.local/share/3d/printer-history/`, переносится туда один раз (`rename` под замком и `flock` на каждый файл); если есть оба каталога, команда останавливается и печатает команды импорта обоих. `3d settings import [<file>…]` идемпотентно переносит их в БД по `uid` (тот же `uid` с другим содержимым отвергается); битая строка (обрыв записи) не пропускается молча, а останавливает импорт с номером строки. Когда появится `config_blob`, импорт будет пересчитывать sha256 приложенного содержимого конфигов и отвергать несовпадающие (пока строки буфера содержимого конфигов не несут).

**Импорт → записи калибровок (решение #264).** Строка становится записью `calibration` (#167), если её `kind` — вид каталога §7.2, или это `slicer_profile` с блоком `measured` и ключом `… / <orca key>`, который применяет вид каталога (`filament_max_volumetric_speed` → `max_volumetric_speed`). `applied`/`measured` — запись; `measured_not_applied` — запись, только если измеренное совпадает с действующим (`new` = `old` по значениям каталога): измерение подтвердило то, на чём печатает принтер. Иначе строка остаётся только в журнале: принтер работает на `old`, а запись `new` заставила бы гейт `calibration_current` требовать значение, которого нет ни в одном профиле. Катушка для видов со scope filament: явный `--spool UID=ID`, затем `spool_id`/`filament_id` строки, затем цвет `spool.color_hex` против строк `spool_binding` той же головы (ровно одна катушка); по одному цвету в Spoolman не связываем — у K1 оранжевая #13 и у Бориса оранжевая #9. Метод: `--method UID=M`, затем метод строки, если он есть в каталоге, иначе `manual` (исходный — в `params.buffer_method`); у вида без `manual` (input_shaper) строка отвергается с перечнем методов — метод не угадываем. Строку без привязки или метода импорт отвергает целиком (ничего не пишет), чтобы повторный импорт с `--spool`/`--method` её принял. Время записи — время измерения (`at`; голая дата — 00:00Z), вытеснение записей идёт по нему: старый буфер не перекрывает более позднюю калибровку, а событие инвалидации после измерения делает её устаревшей.

**Где лежат буферы.** `data_dir()` — это `cli.paths.data_dir()`: единый каталог данных 3d-cli, по умолчанию `~/.local/share/3d-cli/`, с учётом `XDG_DATA_HOME`. Имя каталога — `3d-cli`, как у `config_dir()`; старое имя `3d` — legacy. Буфер журнала настроек переезжает по правилу выше (#264). Буфер задач `printer-todo/` вместе с журналами уведомлений `*.notices.jsonl` переезжает через `printer_task_buffer.migrate_legacy()` (#365). Полное поведение, включая текст при конфликте, описано в `docs/commands/printer.md`; здесь — только правила. Отличия от журнала настроек: решение принимается по любой записи каталога, а не только по `*.jsonl`; замок один (`.printer-todo.migrate.lock`), без `flock` на каждый файл.
- Переезд делает первая команда `3d printer todo`, которая трогает буфер. Исключение — `import FILE` с явным файлом: он переезд не запускает, поэтому команды импорта из сообщения о конфликте выполняются.
- Записи только в старом каталоге → `rename` целиком, вместе с журналами уведомлений. Старый пуст → ничего. Записи в обоих → команда останавливается (exit 1) и печатает, как свести каталоги.
- Будущий писатель этого каталога (журнал уведомлений `watch`, #283) тоже сначала вызывает `migrate_legacy()`.
- Известные пробелы при конфликте отслеживает [#394](https://github.com/alex-mextner/3d-cli/issues/394):
  - старый журнал уведомлений для `watch` надо сливать с новым по периодам, а не просто дописывать;
  - фиксированная цель `mv` (`*.imported`);
  - файл на месте старого каталога.

  До исправления эти случаи разбираются руками.

### 7.5 Задачи принтера (printer todo, #281–#285)
Требование (tg#22149): у каждого принтера один список дел — какие калибровки нужны (никогда не делались, устарели, сброшены заменой сопла или ремонтом), механические действия после калибровки, ремонт и ручные задачи. По каждой задаче: что, зачем, блокирует ли печать, когда удобно (простой или перед печатью), сколько займёт, нужен ли человек, источник, статус. Каналы: CLI, бот, HA (сенсор и `todo`-сущность), `3d-api`.

**Решение: расширяем модель #166, а не заводим параллельную.** В #166 уже есть `fault` («что сломано»), `maintenance` («что сделано»), `maintenance_due` (напоминания по регламенту), у #167 (PR #227) — движок требований калибровок. Не хватает одного *списка действий* со сроком, длительностью, «нужен человек», областью блокировки и зависимостями, плюс ручных задач. Поэтому добавляем одну таблицу `printer_task` в домен #166 (миграция рядом с `0070_maintenance`, модуль `lib/printops/printer_tasks.py`) с тем же словарём printer / head / component, теми же событиями и идемпотентностью по внешнему ключу (как `fault.external_key`). Источники правды остаются прежними: задача ссылается на них и закрывается записью, которая её выполнила.
```
printer_task(id, uid UNIQUE, printer, head?, slot?, component?,
  type        calibration | inspect | maintenance | repair | setup | other,
  title, why,                          -- что и зачем
  blocks      none | printer | head | slot   (head/slot — из полей задачи),
  when        idle | before_print | any,   when_note?,
  duration_min?, needs_human,
  source      manual | calibration | maintenance_rule | fault | calibration_followup | import,
  source_key?,                         -- всегда с принтером: 'calib:k1se/T1' (одна калибровочная задача на голову, '-' — без головы),
                                       -- 'rule:u1/<rule_id>/<head|->/<baseline_id>', 'fault:<external_key|fault_id>'; UNIQUE среди открытых (closed_at IS NULL)
  depends_on[task_id], refs[], calibration_kinds[], spools[], spool_id?,
  requirements[{key, kind, title, reason, spool_id, spool, minutes}],   -- что покрывает калибровочная задача (0072, #387)
  status      open | in_progress | snoozed | done | dismissed,  snoozed_until?,
  closed_at?, closed_by?, note?, calibration_id?, maintenance_id?, fault_id?,
  created_at, updated_at)
printer_idle_notice(printer, idle_since, state pending|sent|failed, claimed_at, sent_at?, tg_message_id?, task_uids[], task_set_sha)  PK (printer, idle_since)   -- ровно одна строка на период простоя
                                     -- task_set_sha = sha256 строки из task uid, отсортированных по возрастанию и соединённых '\n'
```

**Откуда берутся задачи (материализуются, идемпотентно по `source_key`):**
- `calibration` — движок требований #167: каждый вид из каталога, чей статус не `current`/`assumed` (`new_*`, `invalidated` после замены сопла или ремонта, `expired`). **Одна задача на голову** (`calib:<printer>/<head|->`, #387): первая версия (#281) открывала задачу на каждый вид × голову × сопло × катушку × партию, и на U1 из трёх голов с новыми катушками вышло 15 строк «Calibrate <вид> on Tn for <катушка>» с одной и той же причиной — список дел и HA тонули в них. Теперь в задаче поле `requirements`: по записи на требование, с ключом полного scope записи калибровки (§7.2: `calib:k1se/T1/flow_ratio/nozzle:<install_id>/filament:<id>[/lot:<lot>]`), видом, катушкой, причиной и оценкой времени; заголовок («Calibrate 5 kinds on T1 for чёрный»), `why` (виды, сгруппированные по причине), `calibration_kinds`, `spools` и `duration_min` выводятся из него. Записанный вид выпадает из задачи (`printer_task.updated`); задача закрывается `done`, когда у головы ничего не осталось и запись удовлетворила что-то из покрытого ею (если ни одной — например, катушку сняли, — `dismissed` «no longer needed»). Вид, который движок не может решить (катушка неизвестна: Spoolman лежит, ничего не привязано), сохраняет свою запись как была. Почему голова, а не голова × катушка: в голове одна загруженная катушка, так что сегодня это одно и то же, но при смене катушки задача головы переписывается, а не закрывается и открывается заново, и виды без катушки (на голову) не уходят в отдельную задачу. `dismiss` человеком действует на ключи требований, которые задача покрывала: новое сопло, катушка или партия, или истечение после записи этого же scope (запись новее отказа) дают новую задачу только с новыми ключами. История задач читается под write-lock прохода, так что `dismiss`, пришедший во время прохода, не отменяется. Открытые задачи по видам от #281 (любой источник с таким ключом) следующий проход закрывает: удовлетворённые — `done`, остальные — `dismissed` с заметкой `superseded: one calibration task per head (task #N)`; их отказы человеком по-прежнему действуют на свои ключи, зависимости других задач от них переходят на задачу головы, отложенность (snooze) не переносится. Ключ уникален только среди открытых задач: та же потребность после закрытия открывает новую строку, история остаётся. Дата «когда делали» — из журнала настроек (§7.4, #264).
- `maintenance_rule` — `maintenance.due` (#166); закрывается `maintenance.logged` с этим `rule_id`.
- `fault` — открытый fault → задача `repair` с областью блокировки (U1 T2 → `blocks=head`); закрывается `fault.closed`. Доступность головы определяет открытый fault, а не fleet: `usable: false` в `fleet/printers.yaml` только засевает fault (#166, `fleet:u1/T2`). После `fault.closed` (ремонт записан через maintenance) голова снова печатает. Устаревший `usable: false` в fleet показывает `3d lint --printops`, его правят в том же PR, что и ремонт.
- `calibration_followup` — механика после калибровки: `3d calibrate record --followup "…"` или правило `followups:` в `fleet/calibrations.yaml` (например, пики шейпера 13–17 Гц → проверка стола и ремней).
- `manual` — CLI, бот, HA, `3d-api`; `import` — из буфера JSONL.

Производную задачу нельзя закрыть через `done`: CLI отказывает и называет команду, которая записывает работу (`3d calibrate record`, `3d printops maint log --rule`, `3d printops maint fault close`). Иначе следующий проход движка открыл бы её снова. Закрыть без работы — только `dismiss --reason`. Задача с открытой зависимостью показывается как «ждёт» (шейпер K1 ждёт проверки механики X). События: `printer_task.opened|updated|closed|accepted|idle_window`.

**Каналы.** CLI `3d printer todo list [PRINTER] [--all] [--json] | add PRINTER --title … [--why --type --blocks head:T2 --when idle --duration 20 --human --depends-on ID] | done ID [--note] | dismiss ID --reason | snooze ID --until | import FILE`. Команда живёт под `3d printer` (так попросил пользователь, рядом со `status`/`start`/`watch`, где список и нужен), а журналы #166 остаются под `3d printops maint`; `3d printops maint --help` ссылается на `3d printer todo`. REST — §11.1. HA — сенсор `sensor.printops_<printer>_tasks` и `todo.printops_<printer>` (§12, #285). Бот — `/todo` и кнопки «готово / отложить» (#284).

**Preflight (#282).** Проверка `maintenance_due` из #164 (PR #226) обобщается в `printer_tasks` — одна проверка, без второй рядом: каждая открытая задача принтера — WARN (id, длительность, нужен ли человек); задача с `blocks`, совпадающим с заданием, — FAIL (`printer` — всегда, `head`/`slot` — только если G-code их использует). Без `3d-api` (буфер JSONL) голова с `usable: false` в fleet — FAIL, как раньше; с `3d-api` её блокирует открытый fault. Калибровочные задачи сюда не попадают: их уже отражает `calibration_current` со своей политикой. Обход: `3d printer start --force` снимает все FAIL, как сейчас. Точечный обход — новый флаг `--accept printer_task:<id>[,…] --reason …` у `3d printer start` и `print-preflight` (синтаксис как у `3d calibrate check --accept calibration:<kind>` в #227) — снимает только названные задачи, остальные FAIL продолжают блокировать. Каждое принятие пишется событием `printer_task.accepted` с причиной и argv.

**Уведомление о простое (#283).** Если принтер простаивает дольше `idle_notify_after_min` (по умолчанию 20; `fleet/policy.yaml`, секция `printer_tasks:`), отправитель шлёт в Telegram одно сообщение «Сейчас удобно: …» со списком открытых задач с `when ∈ {idle, any}`, без отложенных и без открытых зависимостей; ниже — что откроется следом. **Отправитель один и задан явно:** `printer_tasks.notifier: {kind: watch, host: <hostname>} | {kind: api}` (по умолчанию `watch` на рабочей станции, пока `3d-api` не развёрнут). `watch` на любом другом хосте не шлёт: сверяет `socket.gethostname()` и пишет в лог, почему молчит. Автоматического переключения нет: API на Odroid и `watch` на рабочей станции не делят ни замок, ни БД. Если выбранный отправитель лежит, сообщений нет, а задачи по-прежнему видны в preflight, `todo list`, HA и боте. Переключение на `api` — правка политики, после неё `3d printer todo import` переносит журнал уведомлений `watch` в `printer_idle_notice`. **Не больше одного сообщения на период простоя (at-most-once):** перед отправкой в одной транзакции вставляется строка `(printer, idle_since)` в состоянии `pending` — это захват периода; после ответа Telegram она становится `sent` с `tg_message_id`, после ошибки — `failed`. Строку `pending` после рестарта не отправляют повторно: сбой между захватом и ответом стоит одного пропущенного сообщения, а не дубля. Переподключения и рестарты повтора не дают. Простой дольше суток и изменение списка задач внутри того же периода второго сообщения не дают. Между периодами: новый период с тем же `task_set_sha`, что и в последнем `sent`, в течение `renotify_after_h` (24) пропускается и строку не пишет. Хеш считается по `uid` задач (они одинаковы в буфере и в БД), а не по порядку выборки. Тихие часы (`quiet_hours`, по умолчанию 22:00–09:00 Europe/Belgrade) откладывают захват до их конца, если простой ещё идёт. `watch` ведёт журнал уведомлений в `data_dir()/printer-todo/<printer>.notices.jsonl` (захват под `flock` на файл, `O_APPEND` + `fsync`), поэтому два процесса `watch` на одном хосте тоже не дублируют. Схема строки — `printops.idle_notice/1`: `uid`, `schema`, `printer`, `idle_since`, `state` (`pending | sent | failed`), `claimed_at`, `sent_at`, `tg_message_id`, `task_uids`, `task_set_sha`, `host`. Состояние периода — последняя строка с этим `(printer, idle_since)`.

**Буфер без 3d-api.** Пока нет `3d-api`, задачи пишутся в `data_dir()/printer-todo/<printer>.jsonl` (путь и переезд с legacy — «Где лежат буферы», §7.4; `flock`, `O_APPEND` + `fsync` — как у буфера журнала настроек): одна строка — одна операция, схема `printops.printer_task/1`. Поля строки: `uid` (UUIDv7, ключ идемпотентного импорта), `schema`, `op` (`open | update | done | dismiss | snooze`), `task_uid` (у `open` совпадает с `uid`), `printer`, `printer_model`, `head`, `slot`, `component`, `type`, `title`, `why`, `calibration_kinds`, `spools[{name, spool_id}]`, `blocks{scope, head, slot}`, `when`, `when_note`, `duration_min`, `needs_human`, `source`, `source_key`, `depends_on[task_uid]`, `refs`, `status`, `at`, `recorded_at`, `actor`, `note`. `3d printer todo import FILE` выбирает разбор по полю `schema` каждой строки: `printops.printer_task/1` проигрывает операции по порядку, `printops.idle_notice/1` переносит последнее состояние периода в `printer_idle_notice`; незнакомая схема — ошибка с номером строки. Повторный импорт добавляет 0 строк. Задача `calibration` из буфера сливается с производной задачей с тем же scope (`uid` из буфера остаётся в `refs`); катушка, известная только по имени, привязывается к `spool_id` из Spoolman при импорте. Буфер от 2026-10-15 (legacy-каталог `~/.local/share/3d/printer-todo/`, переезжает по правилу «Где лежат буферы», §7.4): `k1se.jsonl` (6 задач: проверка механики X → холодный `SHAPER_CALIBRATE AXIS=X`, PA и flow для bordeaux / Snapdragon / Ultramarine, верхняя камера #263) и `u1.jsonl` (4 задачи: первые калибровки после очереди дома, T2 неисправна (`blocks=head`, `source_key fault:fleet:u1/T2`), этикетка слота T3 `#FFFFFF NONE` вместо `#1B5E20` (`blocks=slot`), высота камеры для призрака #261).

---

## 8. Защита от забытых калибровок + streamline

### 8.1 Правило
Для задания (G-code × printer × slot → spool → filament/lot × nozzle install) движок берёт из каталога нужные калибровки по каждой используемой голове. Затем ищет действующую запись: тот же scope, не инвалидированную maintenance или nozzle swap, в пределах `valid_until`. Политика (`fleet/policy.yaml`) для каждого вида калибровки и принтера: `block | warn | off`. Дефолты: новый **тип** материала на голове → block; новый **вендор/филамент** → block для flow/PA; новая **партия** → warn; после ремонта или замены сопла → block для затронутых калибровок; input shaper после ремонта кинематики → block.
Обход: `--accept calibration:<kind> --reason "…"` попадает в журнал и в паспорт детали.

### 8.2 Streamline (`3d calibrate`)
1. `3d calibrate plan --printer u1 --head T3 [--spool 42]` → список нужного с оценкой времени и грамм.
2. `3d calibrate gen <kind> …` → G-code. Orca CLI не умеет калибровочные мастера из GUI [INFERENCE], поэтому свои генераторы: PA pattern (своя реализация по мотивам [Ellis PA pattern](https://github.com/AndrewEllis93/Pressure_Linear_Advance_Tool); тот под GPL, поэтому код не копируем, только идею паттерна), flow-плитки (модели + `3d slice` с per-object `filament_flow_ratio`), temp tower (модель + пост-процесс `M104` по Z). Каждый G-code проходит `print-preflight` и привязывается к `calibration_run`.
3. Проведение: CLI, Telegram или голос («напечатал, выбирай»). Камера U1 и vision-ветка делают снимок результата, VLM предлагает лучшую полосу или плитку, пользователь подтверждает. Варианты: «3», «чуть выше 3».
4. Применение — **только с подтверждением**: (a) профиль Orca (user filament JSON в репо или в `~/.config/OrcaSlicer`, diff показывается), (b) Klipper: `SET_PRESSURE_ADVANCE` на сессию или `SAVE_CONFIG` через Moonraker `printer.gcode.script` (перезапуск Klipper; только когда принтер idle и с явным `--yes`), (c) U1 stock — только профиль слайсера.
5. Запись результата закрывает требование. Следующий `print-preflight` видит запись и проверяет, что значения в G-code **совпадают** с калибровкой (Orca пишет `; pressure_advance = …`, `; filament_flow_ratio = …` в футер).

Экспресс-калибровка новой катушки (PA-линии + flow-плитки на одном столе, ≈30 мин, `3d calibrate gen combo`) — **[express-calibration.md](express-calibration.md)**.

### 8.3 Калибровочные столы v2: печатаем только недостающее, плотно, с машинным чтением фото
Эпик [#268](https://github.com/alex-mextner/3d-cli/issues/268) (дочерний к [#154](https://github.com/alex-mextner/3d-cli/issues/154)). Приоритет — фаза 2, сразу после текущей очереди мержа: каталог калибровок [#227](https://github.com/alex-mextner/3d-cli/pull/227), `3d calibrate gen` [#249](https://github.com/alex-mextner/3d-cli/pull/249), комбо-стол и экспресс-калибровка [#257](https://github.com/alex-mextner/3d-cli/pull/257) ([#254](https://github.com/alex-mextner/3d-cli/issues/254)), фреймворк столов (по слоям + последовательная фаза, хуки объектов, зазор портала K1, карта стола NAME.json), журнал настроек [#264](https://github.com/alex-mextner/3d-cli/issues/264).

Обзор для пользователя — [calibration-workflows.md](calibration-workflows.md) ([#329](https://github.com/alex-mextner/3d-cli/issues/329)): общий конвейер (сетка стола перед каждым столом → план → печать → фото → apply → история → задачи), варианты (новый принтер, новая катушка, замена сопла, после ТО и прошивки, проверка дрейфа, многоголовые принтеры со сменой голов, смешение цветов, глажка), плоскостность стола и подкладки из каптона, каналы.

**Критерий приёмки эпика.** Новая катушка PLA на K1 SE: `3d calibrate plan` даёт **≤ 2 стола на катушку, каждый заполнен полезными тестами на ≥ 70 % пригодной площади (цель 80 %)**. Покрыты **все** филаментные калибровки: PA, flow, температура, ретракт, MVS, мосты и нависания, допуски, стринги, первый слой, верхняя поверхность. Время и граммы показываются по каждому столу (оценка слайсера/генератора от первой экструзии до `END_PRINT`, нагрев отдельно); ограничивает их только явный `--max-time` (решение пользователя tg#22176: плотный стол заполняет всё место, время — настраиваемый бюджет, а не потолок). Подтверждается реальной печатью обоих столов. Второй критерий — **сквозная сессия по каналам**: начать в CLI → прислать фото из бота → применить в HA, всё в одной калибровочной сессии (§8.3.6).

**Правила.**
1. **Не перепечатывать откалиброванное.** План строится из того, что для этой катушки, головы и принтера ещё не откалибровано: движок требований #227 (записи, политика D6, инвалидации) плюс журнал #264 (что применено, что откатили). Тест, который читает только действующие калибровки, в план не попадает. Перепечатка — только явным `--only`.
2. **`--skip` / `--only`** принимают короткие id тестов каталога, те же, что у фреймворка столов (`--skip pa,flow,bridge`, `--only temp,retraction`); неизвестный id — ошибка со списком допустимых. В HA-карточке те же id — галочками. Исключённый тест не печатается и не занимает места: план и раскладка пересобираются. Этим же механизмом разово исключаются дубли.
3. **Минимальный тест.** Каждый тест по высоте и площади ровно такой, какой нужен для чтения, и не больше: место на столе уходит на большее число тестов, а не на крупные тесты. У каждого в каталоге есть стоимость (мин, г) и письменное обоснование размера и числа слоёв. CI падает, если оценка слайсера больше стоимости из каталога на 25 % и более.
4. **Температура глобальна.** Тест, который меняет по Z значение, действующее на всю печать (температура сопла; любая ступень по высоте через глобальный M220), печатается в последовательной фазе, один, последним. Низкие тесты печатаются по слоям вместе; их параметры (M221, M220, PA, ACCEL, вентилятор, ретракт) ставятся на входе в объект и снимаются на выходе. MVS-полоса меняет скорость по длине своими G-code-движениями, поэтому идёт в фазе по слоям.
5. **Зазор портала K1.** Последовательные объекты, кроме последнего, ниже `extruder_clearance_height_to_rod`, между объектами не меньше `extruder_clearance_radius`. Объекты фазы по слоям стоят на этот радиус (по Y) от ряда последовательных, чтобы балка портала не прошла над готовой башней. Числа берутся из Orca-профиля принтера ([#266](https://github.com/alex-mextner/3d-cli/issues/266)/[#267](https://github.com/alex-mextner/3d-cli/pull/267) для K1 SE).
6. **Машина предлагает, человек решает.** Vision даёт первую разметку. Значение применяется только после подтверждения или правки пользователем; при низкой уверенности — `needs_manual`.
7. **Стол заполняется целиком.** После обязательного набора планировщик добирает стол полезными тестами, которые ещё не действуют, в порядке приоритета каталога: допуски (вал/отверстие 0.1–0.5, пазы, петля), мелкие отверстия, тонкие стенки, стринги, куб размеров и перекоса, первый слой, верхняя поверхность, VFA/ringing (только если механика принтера не откалибрована). Когда тесты каталога кончились, планировщик расширяет наборы вариантов у тестов на столе (больше зазоров и диаметров отверстий, мельче шаг) до предела, заданного в каталоге. Цель — 80 % пригодной зоны (стол минус зоны зазоров), минимум — 70 %, пока есть кандидаты. Уже действующие тесты добором не печатаются (правило 1). Перед нарезкой пишется PNG-превью раскладки: контуры, id тестов, фидуциалы, последовательный ряд, зоны зазоров.
8. **Время — настраиваемый бюджет.** `--max-time` (в HA-карточке — поле «Не дольше») задаёт бюджет времени на стол. Формат в CLI — длительность `45m`, `1h30m` или целые минуты `45`; в CLI и в API (`max_time_min`) одни и те же пределы: целое 1–1440 минут после нормализации. Пустое значение равно отсутствию (карточка не передаёт пустое поле, API трактует `max_time_min=` как «не задано»). Ноль, отрицательное, больше 1440 (`1441`, `24h1m`) или нечитаемое значение — ошибка (CLI — exit 2, API — 422).
   - **Без `--max-time`** заполнение не ограничивается; план только предупреждает, если стол длиннее `calibrations.plate_warn_minutes` из `fleet/policy.yaml` (по умолчанию 120, если ключа нет; не целое положительное — ошибка `3d lint`). Критерий заполнения ≥ 70 % относится к этому режиму. Если добор остановился раньше 70 % (кандидатов нет — `no_candidates`, или они не помещаются — `no_fit`), рабочий план не падает, но предупреждает «стол заполнен на N %» с причиной. Для приёмки эпика это провал: тест-фикстура новой катушки (#271/#272) и реальная печать требуют ≥ 70 % на каждом столе; такой исход значит, что каталогу (#269) не хватает тестов или вариантов, или раскладке (#272) — упаковки.
   - **С `--max-time`** добор останавливается на этой оценке. Заполнение тогда может быть ниже 70 %; это не ошибка, а осознанный выбор пользователя. План и манифест показывают долю заполнения и причину (`stop_reason: max_time`).
   - Обязательные тесты не выбрасываются никогда. На шаге plan каждый стол проверяется по стоимости из каталога с запасом 25 % (тот же допуск, что в правиле 3): стол 1 — по его обязательному набору, стол 2 — по худшему случаю (обязательный набор плюс условные перепроверки, таблица ниже). Не помещается — plan выдаёт ошибку с величиной превышения, а не после печати стола 1. На шаге gen стол нарезается, и бюджет сверяется с оценкой Orca: сначала урезается добор. Если и без добора оценка Orca больше `--max-time`, gen отказывает (G-code не выдаётся, CLI — exit 2, API — 409) с величиной превышения; сессия остаётся на шаге gen с ожидаемым действием «увеличить --max-time или снять тесты через skip». Граница включительна: стол ровно в `--max-time` проходит.
   - Обязательный набор — это то, что нужно по правилу 1. Явный `--only` задаёт этот набор сам: `--only temp --max-time 5m` проверяется только по башне температуры, а добор с `--only` выключен (`stop_reason: only`).
9. **Стол 2 строится после стола 1.** Стол 2 зависит от значений стола 1 (температура, flow, PA, follow-up), поэтому его G-code генерируется только после подтверждения результата стола 1. Заранее выдаётся только план стола 2 с оценкой времени и граммов.

#### 8.3.1 Поток данных
```
calibrations.yaml (виды, #227) + calibration-tests.yaml (тесты, #269)
      │                     записи #227 + settings_change #264 (что уже откалибровано)
      ▼                                   ▼
3d calibrate plan / GET /v1/calibration/plan ── --skip/--only, --max-time, follow-up ──► план: тесты → столы, заполнение, время, граммы  (#271)
      ▼
OpenSCAD-модули тестов (-D из каталога) → STL; PA-линии и MVS — G-code генератора  (#270)
      ▼
раскладка: упаковка, фазы по слоям/последовательно, зазоры, фидуциалы  (#272)
      ▼                                   ▼
G-code (print-preflight) → печать         манифест calib-plate/1 (где какой тест и вариант)
      ▼
фото: телефон через HA-карточку «Калибровка» | Telegram | камера принтера сверху (#275, #276, #279; свет и контраст — #263)
      ▼
3d calibrate analyze / POST …/photos: ArUco → гомография → анализаторы → calib-assessment/1 + размеченный JPEG  (#273)
      ▼
пользователь подтверждает или правит → apply: записи #227 + settings_change #264 + профиль (D10) / Klipper; откат  (#274)

всё это — шаги одной калибровочной сессии в 3d-api (#277); CLI (#278), бот (#279) и HA (#280) — равноправные каналы
```

#### 8.3.2 Каталог тестов (`fleet/calibration-tests.yaml`, #269)
Тест (то, что **печатается**) отделён от вида калибровки (то, что **записывается**, `fleet/calibrations.yaml`). Один тест может читать несколько видов, например стринги читают ретракт и температуру.
```yaml
tests:
  pa:
    kinds: [pressure_advance]            # какие виды калибровок читает (ключи calibrations.yaml)
    phase: layer                         # layer | sequential (sequential — если меняет глобальное значение по Z)
    generator: gcode:pa_lines            # gcode:<функция> | openscad:models/calibration/<file>.scad
    params:
      pressure_advance:
        range: {default: [0, 0.10], PETG: [0, 0.15], TPU: [0, 0.30]}   # по классу материала
        step: 0.01                       # 11 линий для PLA
    size: {footprint_mm: [30, 40], height_mm: 0.2, layers: 1}
    budget: {minutes: 2, grams: 0.1}     # K1 SE, PLA, сопло 0.4, слой 0.2
    rationale: >-
      Линия 7 + 14 + 7 мм (медленно 20 / быстро 120 / медленно 20 мм/с). Разгон до 120 мм/с при
      5000 мм/с² ~1.5 мм, давление устанавливается за несколько мм, поэтому быстрый участок
      10–15 мм, медленные 5–8 мм; каждый участок длиной в несколько мм, чтобы vision мерил
      ширину. Один слой: PA читается на первом слое, следующие слои усредняют переход.
    readout: {view: top, method: line_width_profile, manual: "самая ровная ширина на обоих переходах"}
    depends_on: []                       # предпосылка ПЕЧАТИ: виды, которые должны уже действовать (у tolerances — flow)
    revalidate_on: {temp: 5}             # условие ДЕЙСТВИТЕЛЬНОСТИ: результат предварительный, если выбранная
                                         # температура ушла от температуры печати на ≥ 5 °C (шаг башни)
    follow_up:                           # что планировщик ставит на СЛЕДУЮЩИЙ плотный стол при неоднозначности
      - {test: pa_fine, when: ambiguous}  # 4–5 линий шагом 0.0025 вокруг лучшего
```
Обязательные поля: `kinds`, `phase`, `generator`, `params`, `size`, `budget`, `rationale`, `readout`. Без них — FAIL в `3d lint` и pytest. `depends_on` — предпосылка печати: тест не планируется раньше, чем вид действует (или чем его тест стоит раньше на этом же столе). `revalidate_on` — условие действительности: тест печатается сразу, а результат становится предварительным и перепроверяется на следующем столе, если условие сработало.

Новые виды в `fleet/calibrations.yaml`: `bridge` (макс. пролёт, bridge flow/speed; `bridge_max_mm` из [#152](https://github.com/alex-mextner/3d-cli/issues/152)), `overhang` (макс. угол), `fit_clearance` (вал/отверстие, паз), `hole_compensation`, `xy_dimensions` (масштаб и перекос), `first_layer`, `thin_wall`, `top_surface`. Стринги — тест, а не вид.

#### 8.3.3 Стоимость обязательных тестов (целевая, K1 SE, PLA, 0.4/0.2)
Числа — целевая стоимость тестов в каталоге [INFERENCE: из размеров и скоростей; #269/#272 сверяют её с оценкой Orca, расхождение > 25 % — FAIL]. Таблицы — только обязательный набор новой катушки; свободное место на обоих столах добирается по правилу 7, поэтому реальные столы длиннее.

**Стол 1 «филамент»** (при температуре пресета; башня температуры последней, одна). Здесь только тесты, которые либо выбирают температуру, либо дают результат, годный при отклонении температуры меньше шага башни (5 °C):

|Тест|Геометрия|Почему так|мин|г|
|---|---|---|---|---|
|`pa`|11 линий (PA 0…0.10 шаг 0.01) по ~28 мм, 1 слой, ширина 0.5|см. rationale выше; диапазон не сужается, уточнение — `pa_fine` на следующем столе|2|0.1|
|`first_layer`|20×20 мм, 1 слой|видны щели и гребни первого слоя; больше площади ничего не добавляет|1|0.2|
|`flow`|5 плиток 15×15 мм, 5 слоёв × 0.2 (1.0 мм), 100 % сплошные, M221 на всех слоях|37 линий верха хватает, чтобы найти периодичность щелей; без разреженного заполнения верх не «подушкует»; сейчас 20×20×3 (15 слоёв) — перерасход|6|1.4|
|`bridge`|пролёты 8/11/14 мм на опорах 3 мм|провис и разрыв нитей читаются на 3 пролётах; опоры минимальной высоты|2|0.5|
|`overhang`|45/50/55/60°, 3–5 мм|подгиб кромки виден за 3–5 мм|2|0.4|
|`retraction`|башня: блоки с растущей длиной ретракта, по слоям|стринги между двумя стойками на каждом блоке|5|1.0|
|`temp`|башня: 7 блоков 230→200 °C, 2.4 мм (8 × 0.3) каждый, последовательно, последней|за блок температура успевает установиться; на блоке видны поверхность, мост и нити|10|2.5|
|фидуциалы, QR|4 × ArUco 10 мм + QR 21 мм, 1 слой|см. 8.3.4|1|0.3|
|**итого**|||**29**|**6.4**|

**Стол 2 «посадки и геометрия»** (печатается с уже выбранными температурой, flow и PA; MVS всегда здесь, потому что зависит от температуры):

|Тест|Геометрия|Почему так|мин|г|
|---|---|---|---|---|
|`mvs`|одна стенка, скорость ступенями по длине, ~8 мм|первая ступень с недоэкструзией видна по стенке; выше не нужно|3|0.6|
|`tolerances`|5 валов Ø4, h 3 мм, в отверстиях с зазором 0.1/0.2/0.3/0.4/0.5 (печать на месте) + 5 пазов 0.1–0.5|функциональная проверка «крутится или нет»; 3 мм — минимум, при котором вал не ломается при проверке|7|2.0|
|`hinge`|3 петли с зазором 0.2/0.3/0.4, ширина 8 мм|печать на месте: подвижна ли петля|4|1.0|
|`geometry`|рамка 30×30×1.2 мм с диагоналями, отверстия Ø2/3/4/5, стенки 0.4/0.6/0.8/1.2|размер и перекос по диагоналям, мелкие отверстия и тонкие стенки на одном купоне; 1.2 мм хватает для штангенциркуля|5|1.5|
|`stringing`|2 шпильки Ø3 × 8 мм, зазор 20 мм|проверка выбранных температуры и ретракта|2|0.2|
|`bridge_warm_cold`|2 моста по 15 мм: вентилятор 100 % и быстро / 40 % и медленно|сравнить «холодный» и «тёплый» мост при выбранной температуре|3|0.6|
|`top`|плитка 15×15 мм, 5 × 0.2, выбранный flow|контроль верхней поверхности после выбора flow|1.5|0.3|
|фидуциалы, QR|как на столе 1||1|0.3|
|**итого**|||**26.5**|**6.5**|

Условные тесты стола 2 (в итог выше не входят):

|Условие|Тесты|мин|г|
|---|---|---|---|
|PA со стола 1 неоднозначен|`pa_fine`: 5 линий шагом 0.0025 вокруг лучшего|+1|+0.05|
|выбранная температура отличается от температуры стола 1 на ≥ 5 °C (шаг башни)|`flow` из 3 плиток вокруг выбранного; заменяет `top` (верх плиток и есть проверка поверхности) и добавляет `pa_fine`|+3.6 − 1.5 + 1 = +3.1|+0.84 − 0.3 + 0.05 = +0.59|

**Зависимость от температуры.** В каталоге у тестов, чей результат зависит от температуры (`pa`, `flow`, `mvs`, `retraction`, `bridge`), стоит `revalidate_on: {temp: 5}`; это условие действительности, а не предпосылка печати, поэтому они печатаются на столе 1 рядом с башней. Если выбранная температура отличается от температуры печати стола 1 меньше чем на шаг башни (5 °C), результаты стола 1 остаются в силе. Если на шаг и больше — результаты `pa` и `flow` записываются как предварительные, и стол 2 их перепроверяет (строка выше). `retraction` перепроверяется `stringing`, `bridge` — `bridge_warm_cold`, `mvs` и так печатается на столе 2. Тест планировщика покрывает обе стороны порога (4 и 5 °C).

Обязательный набор без условных тестов: ≈ 55.5 мин и ≈ 12.9 г на оба стола; худший случай (смена температуры и неоднозначный PA — `pa_fine` один раз): ≈ 58.6 мин и ≈ 13.5 г. Добор до 80 % площади добавляет к этому время и граммы; план показывает итог по каждому столу. Тест планировщика проверяет, что во всех случаях обязательный набор помещается на 2 стола. Если часть калибровок уже действует (правило 1), тесты выпадают, и обязательное обычно помещается на один стол; освободившееся место добирается по правилу 7.

#### 8.3.4 Фидуциалы и подписи
- **4 маркера ArUco** `DICT_4X4_50`, 10 × 10 мм (6 модулей по ~1.67 мм — это больше 4 ширин линии), 1 слой пластиком катушки по углам занятой области. id 0–3 задают роль угла (ЛП, ПП, ПЗ, ЛЗ). Трёх найденных хватает для гомографии в мм стола.
- **QR** 1 слой, полезная нагрузка — короткий токен `C<сессия>P<n>G<ревизия>` в base36, в верхнем регистре (алфавитно-цифровой режим QR; сессия 17, стол 1, ревизия 1 → `CHP1G1`). id сессии — целое из SQLite, ревизия растёт при каждом `gen` стола, токен ≤ 16 символов. Версия QR и размер модуля (≥ 1 мм) выбираются по длине токена; тест на самый длинный токен. По QR фото из Telegram без контекста привязывается к сессии, столу и ревизии (§8.3.6).
- **Подписи** значений (цифры генератора, как в комбо-столе) — для человека. Машина берёт положение вариантов из манифеста, OCR не нужен.
- Контраст пластика и стола проверяется до печати ([#263](https://github.com/alex-mextner/3d-cli/issues/263)): чёрное на чёрном не читается ни маркерами, ни глазом.

**Манифест стола** (`calib-plate/1`, пишет раскладка #272; развитие NAME.json фреймворка):
```json
{"schema": "calib-plate/1", "session_id": 17, "plate": 1, "revision": 1, "printer": "k1se", "head": "T1", "spool_id": 42,
 "gcode_sha256": "…", "print": {"nozzle_temp": 220, "flow_ratio": 0.98, "pressure_advance": 0.04},
 "fill": {"usable_mm2": 48400, "occupied_mm2": 39200, "ratio": 0.81, "minutes": 64, "grams": 14.2,
          "max_time_min": null, "stop_reason": "fill_target"},
 "fiducials": [{"id": 0, "center_mm": [12, 12], "size_mm": 10}, {"id": 1, "center_mm": [208, 12], "size_mm": 10},
               {"id": 2, "center_mm": [208, 208], "size_mm": 10}, {"id": 3, "center_mm": [12, 208], "size_mm": 10}],
 "qr": {"text": "CHP1G1", "center_mm": [110, 12]},
 "tests": [{"test": "pa", "kinds": ["pressure_advance"], "phase": "layer", "views": ["top"],
   "variants": [{"label": "40", "params": {"pressure_advance": 0.04},
                 "bbox_mm": [20, 30, 48, 31], "z_mm": [0, 0.2],
                 "features": {"slow_in": [20, 27], "fast": [27, 41], "slow_out": [41, 48]}}]}]}
```
`fill.stop_reason` — почему добор остановился: `fill_target` (достигнута цель 80 %), `max_time` (упёрлись в `--max-time`), `no_candidates` (кандидатов больше нет), `no_fit` (кандидаты есть, но ни один не помещается в оставшиеся свободные области с учётом зазоров; манифест перечисляет их и размер самой большой свободной области), `only` (явный `--only`, добор выключен). По нему потребители отличают осознанно неполный стол от сбоя упаковки.

#### 8.3.5 Vision и результат (`calib-assessment/1`, #273)
Конвейер: ориентация по EXIF → ArUco (≥ 3 из 4) → гомография → выпрямленный кадр ~10 px/мм → анализатор по `readout.method` каталога:
- `line_width_profile` — PA: ширина на переходах скорости;
- `top_texture` — flow и верх: щели по периодичности линий, гребни по локальной дисперсии при боковом свете;
- `first_layer_coverage`;
- `hole_diameter` — субпиксельная аппроксимация эллипсом;
- `frame_diagonals` — размер и перекос;
- `gap_visibility` — пазы;
- `strand_count` — стринги;
- `band_defects` — башни, по боковому кадру.

Мосты и нависания сверху читаются плохо. Для них манифест просит боковой кадр, иначе — `needs_manual`. Допуски и петли проверяются руками («крутится/нет»), vision только отмечает видимость зазора.
```json
{"schema": "calib-assessment/1", "session_id": 17, "plate": 1, "photo_id": "…",
 "photo": {"sha256": "…", "source": "ha|telegram|camera", "taken_at": "…", "view": "top",
           "fiducials_found": 4, "px_per_mm": 10.2, "homography": [[…]], "contrast": {"delta_e": 41}},
 "tests": [{"test": "pa", "kind": "pressure_advance", "status": "proposed",
   "variants": [{"label": "40", "metrics": {"width_cv": 0.04, "entry_dip_mm": 0.01, "exit_bulge_mm": 0.02},
                 "score": 0.91, "bbox_px": [412, 300, 286, 14]}],
   "proposal": {"values": {"pressure_advance": 0.04}, "confidence": 0.82, "edge": false,
                "follow_up": null},
   "reason": null}],
 "annotated_image": {"sha256": "…", "url": "/v1/calibration/photos/…/annotated.jpg"},
 "analyzer": {"version": "…", "opencv": "4.x"}}
```
`status`: `proposed | needs_manual | failed`. `edge: true` — лучший вариант крайний, значение вне диапазона, и планировщик ставит тест с расширенным диапазоном. `follow_up` (например `pa_fine`) ставится на следующий стол. На размеченном изображении — рамка и подпись у каждого варианта, предложенный выделен зелёным. Оценка хранится отдельно для каждого фото (`photo_id`): переснятое фото даёт новую оценку, а поздно завершившийся анализ старого фото её не перезаписывает. Первый тестовый набор — размеченный датасет `~/.cache/k1-calib/photos/dataset.jsonl`: два комбо-стола K1 SE, чёрный PLA на чёрном PEI (низкий контраст) и оранжевый на чёрном (эталон).

#### 8.3.6 Калибровочная сессия и каналы (#277–#280, #275)
Калибровка — встроенный пошаговый процесс. Его состояние — **калибровочная сессия** в 3d-api (SQLite, события): id, принтер, голова, катушка, план тестов со skip/only, нормализованный `max_time_min` (или «не задано»), столы (G-code, манифест, PNG-превью), статус печати каждого стола (job из журнала заданий и watch), фото, результаты vision по каждому фото, выбранные значения, что применено и что откатили, ссылки на записи #227 и строки `settings_change` #264. `gen` стола 2, повторный `gen` и перезапуск 3d-api берут ограничение времени из сессии, а не заново из запроса.

```
plan → gen (превью) → print → photo → analyze → review/choose → apply → done
                 ▲                                                  │
                 └───────── стол 2: gen … apply по значениям стола 1 ┘        abort — с любого шага
```
- Каждый переход — один вызов API с проверкой условия. Переход не по порядку — 409 с ожидаемым действием. Повтор с тем же client request id идемпотентен (повторное нажатие кнопки в боте не применит значение дважды). Смена шага — compare-and-swap, поэтому два параллельных `gen` не создадут два стола 2.
- Шаги, которые трогают железо или профиль, — `print` и `apply` — выполняются только явной командой с подтверждением (`--yes` в CLI, кнопка с подтверждением в боте и HA). Общий `next` делает ожидаемое действие только для остальных шагов; на `print` и `apply` он отвечает «нужно подтверждение».
- У сессии есть «ожидаемое действие» (например «нужно фото стола 1»). Все каналы показывают одно и то же.
- События `calibration.session.*` (id, шаг, ожидаемое действие) идут в SSE/WS, в HA и в tg-sink.
- **Каналы равноправны.** Сессию можно начать в одном канале и продолжить в другом:
  - **CLI** (#278): `3d calibrate start|status|next|gen|print|skip|photo|analyze|choose|apply|abort`, везде `--session ID`. `3d calibrate plan` остаётся (только чтение, без сессии; `start` = plan + новая сессия). `3d calibrate gen <kind>` из #249 остаётся для ручной печати одного теста вне сессии;
  - **бот** (#279): кнопки шагов, приём фото, размеченная картинка, выбор значения; фото без контекста и без QR — бот спрашивает, к какой сессии;
  - **HA** (#280): services `printops.calibrate_start/next/gen/print/skip/photo/choose/apply/abort`, сенсоры (сессия, шаг, ожидаемое действие, результат), карточка «Калибровка» (#275) — загрузка фото с телефона, галочки skip, выбор значения, «Применить».

```
GET  /v1/calibration/plan?printer=&head=&spool=&skip=&only=&max_time_min=   план обоих столов: заполнение, время, граммы, предупреждения
POST /v1/calibration/sessions                  {printer, head, spool, skip[], only[], max_time_min?} → session_id, шаг, ожидаемое действие
GET  /v1/calibration/sessions/{id}             состояние сессии
POST /v1/calibration/sessions/{id}/next        ожидаемое действие, кроме print и apply
POST /v1/calibration/sessions/{id}/gen         стол 1 или стол 2 (из подтверждённых значений стола 1) → G-code, манифест, PNG-превью, preflight
POST /v1/calibration/sessions/{id}/print       {confirm: true} → старт через гейт старта (`3d printer start`), только idle
POST /v1/calibration/sessions/{id}/skip        {tests[]} → план и раскладка пересобираются (только до gen)
POST /v1/calibration/sessions/{id}/photos      multipart, source=ha|telegram|camera → 202 {photo_id}
GET  /v1/calibration/photos/{photo_id}/assessment                    calib-assessment/1 этого фото + URL разметки
POST /v1/calibration/sessions/{id}/choose      {photo_id, values}
POST /v1/calibration/sessions/{id}/apply       {photo_id, targets: [profile|klipper]} → calibration ids + settings_change ids
POST /v1/calibration/sessions/{id}/abort
```
- **Лимиты загрузки фото.** Тело ≤ 25 МБ; JPEG или PNG (иначе 415); размер в пикселях проверяется по заголовку до декодирования, ≤ 50 Мпикс; декодирование и анализ — с таймаутом 30 с; ≤ 20 фото на сессию. Хранение: фото завершённой или отменённой сессии — 90 дней, потом остаются sha256, размеченная миниатюра и JSON оценки. Тесты на слишком большой файл, битый файл и «пиксельную бомбу».
- **Привязка фото.** `photo_id` должен принадлежать этой сессии и текущему столу. QR несёт ревизию генерации (`C<сессия>P<n>G<ревизия>`, ревизия растёт при каждом `gen` этого стола; манифест хранит ревизию и `gcode_sha256`). QR на фото должен совпасть с сессией, столом и текущей ревизией: фото старого или перегенерированного стола — 409. Без читаемого QR фото принимается только явно в контексте сессии (CLI `--session`, ответ в боте, карточка). Подпись QR не нужна: загружают только авторизованные клиенты в LAN, а ревизия защищает от ошибки, а не от злоумышленника.
- **Цели apply** — явный набор, сверяется с `apply` вида в каталоге (у PA и профиль, и `SET_PRESSURE_ADVANCE`). Права: `read` и `write` — план, сессии, фото, apply только в профиль; `printer-control` обязателен, если в `targets` есть `klipper`.
- **Профиль (D10).** Apply пишет значения в профиль в `profiles/` и показывает diff до подтверждения. Слайсер стоит на машине пользователя, поэтому синхронизация зависит от того, где сделан apply:
  - apply из CLI на машине со слайсером — сразу `3d profiles sync --push --yes` (подтверждение уже получено на diff);
  - apply из HA или бота — строка `settings_change` остаётся `measured_not_applied`, ожидаемое действие сессии — «синхронизировать профиль»; следующий `3d profiles sync` на машине со слайсером забирает ожидающие изменения из 3d-api и переводит строку в `applied`.
  - Если синхронизация упала, правка в `profiles/` остаётся, сессия ждёт повтора, откат — `3d settings rollback`.
  - До синхронизации гейт `calibration_current` ловит G-code со старыми значениями в футере.
- Klipper — только когда принтер idle и с подтверждением.
- HA-карточка ходит в 3d-api через существующий ingress-прокси с авторизацией HA; прокси подставляет токен 3d-api.

Шаги в карточке «Калибровка»:
1. Принтер, голова и катушка (по привязке слота) → список нужных тестов с галочками, поле «Не дольше» (`--max-time`, по умолчанию пусто) и по каждому столу заполнение, время и граммы.
2. «Сгенерировать» → PNG-превью стола 1; при снятой галочке стол пересобирается.
3. «Отправить на принтер» (через гейт старта).
4. После печати — «Загрузить фото» (`<input capture=environment>`, работает в HA Companion).
5. Карточка показывает размеченное фото и таблицу предложенных значений; значения можно править.
6. «Применить» (цели — галочками «профиль» и «принтер») → записи #227, строки `settings_change` #264, профиль с синхронизацией (D10) или Klipper; рядом ссылка «Откатить».
7. Если в плане есть стол 2 — «Сгенерировать стол 2» из применённых значений стола 1, дальше шаги 3–6.

Telegram и камера (#276) работают с той же сессией. Фото в бот привязывается по QR или по ответу на сообщение «стол напечатан»; ответ — разметка и кнопки «Применить / Изменить / Переснять». Камера: на `job.finished` стола калибровочной сессии включается свет (#263), делается снимок сверху, запускается анализ, свет возвращается как был. Тесты, которым мало разрешения или нужен боковой кадр, получают `needs_manual` с просьбой прислать фото с телефона.

#### 8.3.7 Порядок реализации
0. Влить очередь: #227 → #249 → #257, фреймворк столов, #265 (журнал #264).
1. [#269](https://github.com/alex-mextner/3d-cli/issues/269) каталог тестов, новые виды, стоимость и обоснования.
2. Параллельно: [#270](https://github.com/alex-mextner/3d-cli/issues/270) генераторы OpenSCAD и фидуциалы; [#271](https://github.com/alex-mextner/3d-cli/issues/271) планировщик от состояния, `--skip/--only`, `--max-time`, follow-up, заполнение стола.
3. [#272](https://github.com/alex-mextner/3d-cli/issues/272) раскладка: упаковка, фазы, зазоры, манифест, PNG-превью. На этом шаге проверяется критерий «≤ 2 стола на катушку, заполнение ≥ 70 % (цель 80 %)».
4. [#277](https://github.com/alex-mextner/3d-cli/issues/277) калибровочная сессия и её API (нужен план #271).
5. [#273](https://github.com/alex-mextner/3d-cli/issues/273) vision. Начать можно раньше — на датасете и манифесте NAME.json фреймворка.
6. [#274](https://github.com/alex-mextner/3d-cli/issues/274) apply через журнал с откатом и синхронизацией профиля (расширяет apply из [#168](https://github.com/alex-mextner/3d-cli/issues/168)).
7. Каналы, параллельно: [#278](https://github.com/alex-mextner/3d-cli/issues/278) CLI; [#279](https://github.com/alex-mextner/3d-cli/issues/279) бот; [#280](https://github.com/alex-mextner/3d-cli/issues/280) HA services и сенсоры + [#275](https://github.com/alex-mextner/3d-cli/issues/275) карточка; [#276](https://github.com/alex-mextner/3d-cli/issues/276) фото из Telegram и с камеры принтера.
8. Приёмка эпика: печать обоих столов на новой катушке на K1 SE; сессия «CLI → фото из бота → apply в HA»; `docs/printops/express-calibration.md` (#257) переписывается под v2.

---

## 9. Гейты, линтеры, хуки против типичных ошибок

### 9.1 Новые проверки `print-preflight` (lib/printer_ops/preflight.py → модуль `printops_checks`)
Каждая проверка возвращает `Check(name, status PASS|WARN|FAIL|UNVERIFIED, detail)` — поля как у существующего `Check` в `lib/printer_ops/preflight.py`. Уровни задаются политикой. Без данных учёта — `UNVERIFIED`, а не PASS (как в #66). **Миграция:** сейчас `Report.overall` знает только FAIL и WARN и всё остальное сводит к PASS, поэтому UNVERIFIED дал бы PASS. Нужно добавить UNVERIFIED в агрегацию (не ниже WARN; блокирует ли старт — решает политика) и регрессионный тест «UNVERIFIED не даёт overall PASS».

| Проверка | Вход | FAIL, если |
|---|---|---|
| `filament_temp_range` | G-code: `M104/M109 S` по T, `M140/M190`, футер `nozzle_temperature`, `bed_temperature` ↔ filament (min/max из temp tower или вендора; U1 tag `HOTEND_MIN/MAX_TEMP`) | сопло или стол вне окна (по каждой голове, с учётом first-layer) |
| `loaded_filament_match` (расширяет `check_slots`) | футер `filament_type`, `filament_colour`, `filament_vendor`, `filament_settings_id` по экструдеру ↔ привязанная катушка в учёте ↔ RFID и цвет принтера | тип не совпадает (FAIL); цвет ΔE > порога (FAIL или WARN по проекту); вендор или партия другие (WARN и проверка калибровок) |
| `slot_binding_known` | каждый используемый T | на слоте нет привязанной катушки → WARN «расход не учтётся» |
| `filament_sufficient` | граммы по T из футера + prime tower/purge + запас ↔ остаток из ledger (интервал) | остаток < нужного (FAIL); в зоне неопределённости — WARN «взвесить» |
| `drying_due` | класс гигроскопичности материала (PLA мягко; PETG, TPU, PA, PC, PVA строго) ↔ last_dried_at, открыта с…, RH бокса | просрочено по политике |
| `nozzle_compat` | abrasive-флаг филамента (CF/GF/glow/wood/metal) ↔ материал сопла на голове; `nozzle_diameter` в G-code ↔ установленное | CF на латуни; диаметр не совпадает |
| `calibration_current` | §8 | нет действующей калибровки или значения в G-code ≠ записанным |
| `printer_tasks` (было `maintenance_due`, §7.5, #282) | открытые задачи принтера: fault → repair, просроченное ТО, ручные и после калибровки; `usable: false` в fleet | голова неисправна; задача с `blocks` = printer или с используемой головой/слотом (остальные открытые — WARN) |
| `enclosure_material` | ABS/ASA/PC ↔ enclosure и chamber принтера | WARN |
| `plate_material` | тип стола ↔ материал (PETG на гладком PEI без клея и т.п.) | WARN |
| `preset_match` (#293) | `printer_settings_id`/`print_settings_id` ↔ `slicer:` принтера во fleet; ускорения ↔ процесс-пресет | FAIL (стоковые пресеты Orca, несохранённая правка) |
| `printer_profile_match` | `printer_model`/`printer_settings_id` в G-code ↔ целевой принтер | FAIL (G-code K1 на U1 и т.п.) |
| `mix_calibration` (§20.7, #418) | смеси стола (проект 3MF) → катушки на их головах → ключ смеси ↔ записи `mix_color` | WARN, если смесь не измерена или измерение устарело; FAIL по политике, если измеренный цвет далеко от цвета детали |

Плюс существующие: thumbnails, printer_free, overhangs, slice_warnings, head_sequence, mix face (§20.6, #411: смешанная деталь показывает цвет на стенках, по проекту 3MF), visible, macros, moves. Строки ядра `print-preflight` пишутся словами (`head seq`, `mix face`), проверки PrintOps — snake_case, как в этой таблице.

### 9.2 Три рубежа
1. **При слайсе:** скрипт Orca post-processing (`integrations/orca/post-process.sh` → `3d print-preflight --printer <из футера> --offline`). Ошибка видна прямо в Orca при экспорте [INFERENCE: Orca показывает stderr и ненулевой код скрипта].
2. **При отправке:** `3d printer start` и `POST /v1/jobs/{id}/start` в `3d-api`. FAIL блокирует, обход через `--accept … --reason`.
3. **Старт не через нас** (Orca «Print», тачскрин, Bambu Handy). watch видит `printing` → `3d-api` скачивает файл (Moonraker `/server/files/gcodes`, K1 HTTP) → preflight → уведомление в HA, Telegram и колонку. Если FAIL, по политике (D5) `PAUSE` в первые N слоёв — только для своих принтеров и только для проверок `nozzle_compat`/`filament_temp_range`/`loaded_filament_match(type)`.

### 9.3 Линтеры данных (`3d lint --printops`, CI и cron в `3d-api`)
Катушка в двух местах; адрес не существует; слот принтера по RFID ≠ учёту; filament без температурного окна или абразивности; катушка без `spool_weight` (тара); остаток < 0; давно не взвешивали (дрейф ledger); `printers.yaml` ↔ живой topology (число голов, сопла); сниппеты HA ↔ живой `configuration.yaml`; `vendor.lock` ↔ версии submodule на Odroid.

### 9.4 Репо-гейт
dev-gate (ruff/mypy/pytest/shellcheck) расширяется на `services/3d-api`, `integrations/homeassistant` (yamllint + `check_config` в docker в CI [INFERENCE: образ HA тяжёлый, только в CI, не pre-commit]) и `integrations/esphome` (`esphome config`, ср. esphome-light [#12](https://github.com/alex-mextner/esphome-light/issues/12)).

---

## 10. RFID/NFC катушек и станция на ESPHome

Подробности — в **[rfid.md](rfid.md)**: матрица совместимости, BOM с ценами, скетч ESPHome YAML (не компилировался), альтернативы.

### 10.1 Совместимость меток (выводы)
- **U1, судя по всему, на стоковой прошивке** [INFERENCE: в живом `filament_detect` нет полей extended-прошивки; все 4 канала сейчас пусты]. Ридер — 2× FM17580, по 2 антенны на чип, всего 4 канала e0..e3. Работает только ISO14443A/B; NFC-V (OpenPrintTag) аппаратно не поддерживается.
- Сток U1 понимает только **MIFARE Classic 1K Snapmaker с подписью RSA-2048** (`OFFICIAL=true`). Ключи секторов выводятся из UID через HKDF, соли лежат в открытом коде [u1-klipper](https://github.com/Snapmaker/u1-klipper). Чужие и неподписанные метки он игнорирует целиком, не отдаёт даже `CARD_UID`; NTAG тоже игнорирует.
- На **paxx12 extended FW** U1 читает NTAG213/215/216 в формате OpenSpool, понимает чужие метки через OpenRFID и связывается со Spoolman.
- **K1 SE:** ридера нет, он появляется только с CFS-китом. Метки CFS не подписаны, поэтому свои метки там можно писать.
- **Bambu AMS / AMS lite:** принимают только подписанные метки Bambu. Слот под нашу катушку назначается по MQTT, для этого нужны LAN-only и Developer Mode.
- **OpenPrintTag** (Prusa, ISO15693, MIT) не читает ни один принтер из парка.
- **Spoolman v0.27** умеет нативно привязывать метку по аппаратному UID: `POST /api/v1/tag/scan`, привязка к spool и filament, есть гайд по ESPHome. Содержимое меток он не разбирает.

### 10.2 Своя маркировка
- Катушки без меток: **2× NTAG215 (25 мм, по одной на каждую щёчку)**. Сначала запись URI HA-tag `sp-<spoolman_id>`, затем OpenSpool JSON (парсер paxx12 такой порядок выдерживает, проверено по коду). Плюс бумажный QR как дубль.
- Вендорские метки (Bambu, Creality, Snapmaker, Prusament) не трогаем и привязываем **по UID** в Spoolman.
- Ячейки склада, сушилки и слоты принтеров: **NTAG213 + QR** с ID `cell:A-2-03`, `dryer:1`, `slot:u1/e2`. Таблица ячеек живёт в 3d-api, в `Spoolman.location` пишется строка-путь.
- Принтер без ридера (K1 SE; U1 на стоке для наших катушек; Bambu для чужих катушек) — схема «скан-скан»: метка катушки, затем метка слота (или выбор энкодером), затем `POST /v1/tags/scan op=load`. Для U1 3d-api может дополнительно записать слот в принтер через `SET_PRINT_FILAMENT_CONFIG`. Это запись в принтер, она пока не тестировалась и пойдёт отдельным тикетом с подтверждением.

### 10.3 Станция ESPHome
- ESP32-S3 + **PN532** (официальный `pn532_spi`, чтение и запись NDEF; RC522 отдаёт только UID; PN7160 не умеет NFC-V; у PN5180 нет официального компонента) + OLED SSD1306 или TFT ST7789 + энкодер EC11 + 2–3 кнопки + HX711 с тензобалкой на 5 кг.
- Операции: load, unload, to_dryer, from_dryer, store (скан ячейки), weigh, incident, info.
- События уходят в `3d-api` (`/v1/tags/scan`), а в HA — как `esphome.spool_station` и `tag_scanned`. Вес отправляется в Spoolman через `/measure`, тару тот вычитает сам.
- **Цена:** ≈ **6–7 тыс. RSD (50–60 €)** в сербских магазинах (Elektro Modul, Elektroleum — проверено) или ≈ **25–35 $** на AliExpress [INFERENCE]. Метки: ≈0,1 $/шт на Ali, 70–100 RSD/шт в Сербии.
- **Альтернативы:** телефон с HA Companion (касание метки → actionable notification «в U1/eN · в сушку · в ячейку»; бесплатно, сразу, но без весов и медленнее), QR + камера телефона (бесплатно, но ещё медленнее). Встроенные ридеры принтеров для наших меток бесполезны без paxx12.
- **Рекомендация:** сначала телефон + NTAG и QR. Тикет 21 (схема меток) можно поднять в Фазу 1, сразу за тикетом 6: это почти бесплатно. Станцию (тикет 22) строить, когда заработает ledger: её главная ценность — весы и одно касание вместо 5 действий. PN5180 и OpenPrintTag отложить.

---

## 11. 3d-api: REST, события, голос

Сервис FastAPI (стек `3d web`) в контейнере на Odroid рядом с HA. Хранилище — SQLite и Spoolman. Авторизация — bearer-токены на клиента (ha, station, bot, cli, voice) со scope: read, write, printer-control, camera (узел камеры: только `PUT /v1/camera-nodes/{name}`, #341). Наружу сервис не публикуется; удалённый доступ — через Tailscale.

### 11.1 REST (v1)
```
GET  /v1/printers                          парк + состояние + слоты (адаптеры)
GET  /v1/printers/{p}/slots                слот → катушка (учёт) + RFID/цвет (принтер) + mismatch
POST /v1/printers/{p}/slots/{s}/load       {spool_id | query:{material,color,vendor}} → bind (или кандидаты для уточнения)
POST /v1/printers/{p}/slots/{s}/unload     {to_location?, weight_g?}
GET/POST /v1/printers/{p}/maintenance | faults | calibrations
GET  /v1/tasks?printer=&status=   GET/POST /v1/printers/{p}/tasks   POST /v1/tasks/{id}/done|dismiss|snooze   # §7.5
GET  /v1/spools?material=&color=&location= (Spoolman + PrintOps-состояние)
POST /v1/spools/{id}/move {location}       POST /v1/spools/{id}/measure {gross_g}
POST /v1/spools/{id}/notes                 POST /v1/incidents   GET /v1/incidents/stats?by=
GET  /v1/locations (дерево)   GET /v1/locations/{path}/contents
POST /v1/drying/sessions {dryer, spools[], temp, hours}   POST /v1/drying/sessions/{id}/finish
GET/POST /v1/jobs (очередь)  PATCH /v1/jobs/{id} (порядок/принтер)  POST /v1/jobs/{id}/preflight  POST /v1/jobs/{id}/start (confirm token)
GET  /v1/history?printer=&since=          история заданий (§15 п.16)
GET  /v1/settings/history?printer=&kind=&since=&cursor=   GET /v1/settings/blobs/{sha256}   POST /v1/settings/{id}/rollback   # §7.4
POST /v1/preflight (gcode + printer → checks; stateless)
GET  /v1/calibrations/required?printer=&slot=&spool=   POST /v1/calibrations/runs   POST /v1/calibrations/{id}/result|apply
GET/POST /v1/purchases       GET /v1/shops/search?q=&material=&color=
POST /v1/tags/scan {uid, payload?, op, printer?, slot?, location?, weight_g?}   # станция/телефон
POST /v1/intents {text, speaker?} → {intent, slots, resolution, confirm_prompt, confirm_id}
POST /v1/intents/{confirm_id}/confirm
GET  /v1/events (SSE) | WS /v1/ws
```

### 11.2 События
`printer.state_changed`, `job.queued|started|finished|failed`, `slot.loaded|unloaded|mismatch`, `spool.low|moved|measured|empty`, `drying.started|finished|due`, `calibration.required|recorded|applied`, `settings.changed` (§7.4), `maintenance.due`, `printer_task.opened|updated|closed|accepted|idle_window` (§7.5), `incident.created`, `preflight.failed`, `anomaly.opened|resolved`, `shop.stock_changed|price_changed`. Доставка: SSE и WS, пуш в HA (`POST /api/events/printops_*` с long-lived token) и watch-sinks (tg, exec).

### 11.3 Голосовые интенты (RU)
| Фраза | Интент | Разрешение |
|---|---|---|
| «Загрузил белый PLA в U1 голову 4» | `LoadSpool(printer=u1, slot=T4, color=white, material=PLA)` | кандидаты со склада. Если один — bind и «Записал: Elegoo PLA+ белый, партия …», если несколько — уточнение. Через 10 с сверка с RFID T4 (index 3!) |
| «Снял катушку с K1, положи на полку B3» | `UnloadSpool` + `MoveSpool` | |
| «Поставил зелёный PETG сушиться на 6 часов» | `StartDrying` | таймер, событие `drying.finished` |
| «На U1 голова 3 пробка» | `ReportIncident(kind=clog)` | контекст подтягивается сам |
| «Сколько осталось белого PLA?» | `QueryStock` | сумма по катушкам с интервалом |
| «Что печатает U1 / когда закончит?» | `QueryPrinter` | |
| «Откалибровал поток, лучшая 3» | `CalibrationResult` | |
Канал: HA Assist (whisper и openwakeword уже стоят) → `custom_sentences/ru/printops.yaml` + `intent_script` → `rest_command` → `/v1/intents`. Вариант: YandexDialogs (уже в submodules) для Яндекс Станции. Свободная речь → LLM-парсер в `3d-api` (Ollama на Odroid) со строгой JSON-схемой. **Мутации — только после подтверждения** («да»), кроме записей в журнал.

---

## 12. Home Assistant: вкладка «3D Printers»

- **Фаза 1 (без кода в HA):** `packages/printops.yaml`: REST-сенсоры из `3d-api` (`/v1/printers`, `/v1/spools/summary`, `/v1/anomalies`) и `rest_command` (load, unload, move, dry, incident, measure).
- **Фаза 4:** своя интеграция `custom_components/printops` (WS-подписка на `/v1/ws`, без поллинга):
  - `sensor.printops_<printer>_<slot>_spool` (материал, цвет, остаток г ± интервал, партия, дни с сушки), `binary_sensor.printops_<printer>_<slot>_mismatch`;
  - `sensor.printops_stock_<material>_<color>`, `sensor.printops_low_stock_count`, `sensor.printops_drying_active`;
  - `sensor.printops_queue_<printer>` (следующее задание, ETA), `sensor.printops_<printer>_tasks` (открытые, блокирующие, следующая на простой; §7.5), `calibration_required`;
  - `todo.printops_<printer>` (`TodoListEntity`: отметка = `POST /v1/tasks/{id}/done`, новый пункт = ручная задача; #285);
  - сервисы `printops.load_spool|unload_spool|move_spool|start_drying|report_incident|measure`.
- **Карточки в существующей вкладке** (`ui-3d-printers.yaml`, секция Overview + новый view «Склад»):
  1. «Заправка» на каждый принтер: головы/AMS-слоты с цветом, материалом, остатком, mismatch-бейджем и кнопками (снять, взвесить, проблема).
  2. «Очередь» — список заданий с принтером, ETA и граммами, кнопка «preflight».
  3. «Склад» — дерево адресов (WH:A/S2/B3) с катушками и фильтром, low-stock.
  4. «Сушка» — активные сессии с таймером, просроченные катушки, RH боксов.
  5. «Калибровки и ТО» — задачи принтера (§7.5): что требуется до следующей печати и что удобно сделать в простой.
  6. «Инциденты» — последние и топ по вендорам и партиям.
  7. Существующие камера, u1-inspector и share-карточки остаются.
  8. «База знаний» — поиск, статья, связанные записи; подсказка KB на карточках задач и инцидентов (§19.2, §19.4, #362).
- Карточки — Lit/vanilla JS в `www/printops/`, по образцу `u1-share` (тот же resource_mode yaml).
- **Уведомления:** существующий `script.printer_send_notification` получает события `preflight.failed`, `slot.mismatch`, `drying.finished`, `spool.low`, `calibration.required`, `printer_task.idle_window`.

---

## 13. Поиск в магазинах (Белград)

| Магазин | Платформа (проверено 2026-10-14) | Способ |
|---|---|---|
| 3dfilamenti.rs | WordPress 6.8.2 + **WooCommerce 9.9.5**; **Store API открыт**: `GET /wp-json/wc/store/v1/products?search=pla` отдаёт JSON (товары, вариации, цены, stock) | HTTP-клиент, без браузера |
| 3dmarket.rs (Voxellab) | HTTP 403, **Cloudflare challenge** | headless-браузер (agent-browser/Playwright) раз в N часов; или попросить у магазина фид или прайс; [INFERENCE] платформа за CF не определена |
| gamecentar.rs (Game Centar) | HTTP 403, **Cloudflare** | то же; Elegoo RAPID PLA+ там уже покупали (queue_report.py CHECKED) |

- `shops/<shop>.py`: адаптер `search(query) → [Offer{shop, title, vendor, material, color_name, color_hex?, diameter, weight_g, price_rsd, in_stock, url, fetched_at}]` + нормализатор (материал, цвет, вес из названия и вариаций).
- Кэш `shop_offer` в PrintOps DB с TTL. Изменения цены или наличия → события `shop.*`. Сопоставление с `filament` (vendor + material + color) для «где купить ещё такую же». Для CF-магазинов — бюджет запросов и робот-этикет (robots.txt, 1 запрос в несколько секунд).
- Связь с закупками: low-stock или прогноз (§17) → `3d procurement --shops` → корзина по магазинам с ценами и наличием → `purchase` (ordered) → приёмка (received) → катушки в Spoolman и адрес на складе.
- Правило проекта «реальный магазин, артикул, URL, цена, без заглушек» (ROADMAP §32) соблюдается автоматически.

---

## 14. Перенос: 3d-lib, 3d-bot, HA

1. **3d-lib (§29):** `lib/` → `packages/3d-lib/src/threed/` (uv workspace, hatchling). Импорты `from registries.x` → `from threed.registries.x` (codemod через ast-grep). `bin/3d` → console-script `3d = threed.cli.dispatch:console_main`: новая обёртка без аргументов, которая вызывает `main(sys.argv[1:])` (нынешний `main(argv)` требует аргумент, его передаёт `bin/3d`). Тест: установленный `3d --version` из каталога вне checkout. `3d-api` и `3d-bot` зависят от `threed` как от workspace-пакета. Внешних потребителей нет, поэтому переходим чисто, без шимов.
2. **3d-bot (D1):** исходник не найден. Варианты: (a) пользователь укажет, где он (другая машина или приватный репо под иным именем); (b) пишем новый `projects/3d-bot` (Telegram на базе tg-cli / watch-sinks: кнопки «загрузил / снял / сушка / инцидент», уведомления и подтверждения из `3d-api`).
3. **HA:** §3.1. Порядок: коммит грязных правок на Odroid → `filter-repo` → merge в 3d-cli → `.gitignore` в HA-репо → первый `3d ha deploy --dry-run` (diff должен быть пустым) → настоящий деплой.
4. **Babushkin-dom queue:** формат `queue.json`/`plates.json` импортируется в PrintOps jobs и owned spools (`3d printops import-queue`). `queue_report.py` начинает читать из `3d-api`.

---

## 15. Фазы и GitHub-issues (3d-cli)

**Фаза 0 — фундамент (без функциональности для пользователя)**
1. *Monorepo: lib/ → importable `threed` package (uv workspace), console-script `3d`* — `uv run 3d --version` работает из любого cwd; `bin/3d`-симлинк убран; весь dev-gate зелёный.
2. *HA 3D artifacts move into 3d-cli with history (integrations/homeassistant) + `3d ha deploy`* — `git log` в 3d-cli показывает историю f4e5132…51485cf; `deploy --dry-run` на Odroid даёт 0 изменений; после деплоя `check_config` OK, сущности `sensor.printer_*` не unavailable.
3. *fleet/printers.yaml: printer topology (heads/nozzles/AMS), owners/access, offline entries (#142, #84)* — `3d printer list` показывает 5+ принтеров, в том числе offline; U1 T2 помечен неисправным.

**Фаза 1 — учёт катушек и склад**

4. *Spoolman on Odroid (compose) + 3d-api skeleton (FastAPI, SQLite, tokens, /v1/events)* — `docker compose up` поднимает оба сервиса; healthcheck; бэкап SQLite в существующий бэкап-контур.
5. *Locations tree + addressing labels (WH:zone/shelf/box/slot), print QR/NFC labels* — `3d printops loc add|tree|label`; путь ≤ 64 символов синхронизирован в Spoolman.location.
6. *Spool lifecycle: receive → store → load into slot → unload → empty; ledger with confidence* — CLI и REST; остаток показывается интервалом; импорт текущих катушек (owned из babushkin-dom).
7. *Slot reconciliation: U1 print_task_config/filament_detect, K1 ws, Bambu tray → mismatch anomalies* — подмена катушки в тесте с фейковым адаптером → `slot.mismatch`.
8. *Drying sessions + policy (hygroscopic classes) + dry-box RH sensor link* — `drying.due` при просрочке.
9. *Weighing: /measure with vendor tare DB* — после взвешивания ledger сбрасывается на абсолют.

**Фаза 2 — гейты, калибровки, ТО, инциденты**

10. *print-preflight: material gates (temp range, loaded match, sufficient, drying, nozzle compat, profile match)* — тест-G-code на каждое FAIL-условие; без данных учёта — UNVERIFIED.
11. *Incidents: model, auto-context, stats by vendor/lot/printer* — `incident stats --by lot` на фикстурах.
12. *Fleet components & maintenance log + rules → maintenance.due* — замена сопла инвалидирует flow/PA этой головы.
13. *Calibration catalog + requirement engine + `calibration_current` gate* — новая партия → WARN, новый материал → FAIL до записи калибровки.
14. *`3d calibrate`: PA pattern / flow / temp tower generators, guided run, apply with confirmation* — сгенерированный G-code проходит preflight; apply в Orca-профиль показывает diff; `SAVE_CONFIG` только при idle и `--yes`.
15. *Three-tier gating: Orca post-processing hook + start gate + post-start watch check (policy-driven pause)* — старт с тачскрина с CF-на-латуни → уведомление, пауза — по политике.

15a. *Printer tasks: `printer_task` + derivation, `3d printer todo`, REST, JSONL import (#281); `printer_tasks` preflight check (#282); idle-window notifier (#283); HA sensor + todo (#285); 3d-bot `/todo` (#284)* — импорт буфера 2026-10-15 даёт 10 открытых задач; печать U1 с T2 → FAIL; одно сообщение на период простоя.

15b. *База знаний ТО, TUI, паритет HA, работа без LLM, провайдер Ollama (эпик #356, §19): #357–#364* — инцидент «X тугой» в TUI подсказывает первую статью, связь видна в HA; весь сценарий проходит при выключенном ИИ.

15c. *Подбор цвета и калибровка смесей (эпик #410, §20): #116, #411–#413, #415–#420* — цвет плитки PTA/PTB подбирается тестером за меньше 30 мин, фото даёт рецепт в пределах ΔE 3; плоская смешанная деталь не проходит `print-preflight`.

**Приоритет фазы 2 после текущей очереди мержа:** *calibration plates v2* (§8.3, эпик [#268](https://github.com/alex-mextner/3d-cli/issues/268)) — новая катушка: ≤ 2 стола, каждый заполнен на ≥ 70–80 %, все филаментные калибровки; время — настраиваемый `--max-time`; печатается только недостающее; `--skip/--only`; фото → vision → подтверждение → apply с откатом.

**Фаза 3 — очередь, история, голос, HA**

16. *Fleet job ledger (#68) + history ingestion (Moonraker history, K1 ws, Bambu MQTT) + job_tool usage*.
17. *Queue: enqueue/reorder/assign + babushkin-dom queue.json import; queue_report.py reads from 3d-api*.
18. *Voice intents: HA custom_sentences + /v1/intents (rule-based + LLM fallback) + confirmations* — 7 фраз из §11.3 проходят e2e в HA Assist.
19. *HA: packages/printops.yaml (REST) + dashboard cards (slots, queue, stock, drying, calibrations, incidents)*.
20. *HA custom_component printops (WS push, entities, services)*.

**Фаза 4 — RFID/NFC, станция, магазины, бот**

21. *NFC tag scheme for own spools + location tags; `3d printops tag write|read`*.
22. *ESPHome spool station (reader + buttons/encoder + display + HX711) → /v1/tags/scan*.
23. *Shop adapters: 3dfilamenti (Woo Store API), 3dmarket & gamecentar (browser, CF) + offer cache + procurement --shops*.
24. *3d-bot: Telegram buttons and confirmations over 3d-api* (D1).
25. *Bambu adapters for P1S / A1 mini (read-only by default, with owner consent)*, плюс #98.

---

## 16. Lint wiring (сводно)
- `3d lint --printops` (данные, §9.3) — локально, в cron `3d-api` (раз в час → `anomaly`) и в CI на фикстурах.
- dev-gate: `services/3d-api` (ruff, mypy, pytest), `integrations/homeassistant` (yamllint, сверка сниппетов, тесты u1_inspector), `integrations/esphome` (`esphome config`).
- Pre-push/CI: тест «деплой-манифест HA ↔ файлы в репо» (никаких сирот).
- Политики гейтов (`fleet/policy.yaml`) проверяются схемой.

---

## 17. Дальнейшие возможности (фаза 5: все F1–F16, решение D11)

| # | Возможность | Зависит от | Оценка сложности |
|---|---|---|---|
| F1 | **Планировщик парка**: стол → принтер по заправке (минимум перезаправок; переиспользует #112 и recipes Full Spectrum), времени, ночным окнам (тихие часы, без присмотра — только «надёжные» столы), занятости | 16, 17, 7 | высокая |
| F2 | **Себестоимость детали**: г × цена катушки (из purchase) + электричество (кВт·ч из HA power-сенсора розетки или номинал × время × тариф) + износ (амортизация на час и сопла на кг) + доля неудач; отчёт по проекту | 6, 16 | средняя |
| F3 | **Паспорт детали**: G-code sha, принтер, головы → катушки и партии, калибровки, preflight-результаты, фото первого и последнего слоя, инциденты; QR на изделии | 16, 13 | средняя |
| F4 | **Учёт перепечаток**: attempt → attempt с причиной (связь с incident), статистика first-attempt yield по принтеру, материалу и проекту | 11, 16 | низкая |
| F5 | **Прогноз расхода и заблаговременная закупка**: расход по скользящему окну + очередь → дата исчерпания по цвету и материалу; заказ с учётом сроков доставки магазина | 6, 17, 23 | средняя |
| F6 | **Остаток по весу**: база тар катушек (вендор и тип катушки, картон или пластик), станция-весы | 9, 22 | низкая |
| F7 | **Датчик влажности в сухом боксе** (ESPHome SHT4x/BME280) + правила сушки (RH > X% → в сушку) | 8 | низкая |
| F8 | **Каталог измеренных цветов**: колориметр (например TCS34725/AS7341 на станции) или фото с эталонной картой (ColorChecker) → `filament.measured_hex` с доверием; используют colour-mix #116 и подбор | 22 | средняя |
| F9 | **Партии и сроки**: lot, дата производства и вскрытия, деградация (PVA/PA), FIFO-подсказки | 6 | низкая |
| F10 | **Счётчики пробега → регламент ТО** | 12, 16 | низкая |
| F11 | **Склад запчастей** (сопла, PTFE, ремни, стол): parts в той же адресации, списание по maintenance, low-stock | 5, 12 | низкая |
| F12 | **Сравнение принтеров на одинаковых тестерах**: эталонные тестеры (bridge, overhang, tolerances, ΔE) → результаты по принтеру и голове, регресс после ТО | 13, 14 | средняя |
| F13 | **Проверки до, во время и после печати**: чистый стол по камере перед стартом (vision #150, U1 DETECT_BED_PLATE), первый слой (#135 по G-code + по камере), вес детали после печати против ожидаемого (станция-весы) → incident | 22, #150 | средняя |
| F14 | **Интерфейсы**: голос (§11.3), HA-вкладка (§12), Telegram-кнопки (3d-bot) — для всех операций | 18–20, 24 | средняя |
| F15 | Метрики Prometheus/Grafana (Spoolman уже отдаёт), годовой отчёт по пластику | 4 | низкая |
| F16 | Связь с product vision (§1.5): Order → ProductionAttempt = PrintOps job | 16 | — |

---

## 18. Решения пользователя

Принятые решения (tg#21166, 2026-10-14; D11 — tg#21173) — в колонке «Решение»; они имеют приоритет над рекомендациями и текстом выше.

| # | Вопрос | Рекомендация | Решение |
|---|---|---|---|
| **D1** | `projects/3d-bot` не найден. Где исходник, или пишем новый? | Если исходника нет — новый тонкий бот поверх `3d-api` (Фаза 4) | новый бот поверх 3d-api |
| **D2** | Spoolman + своя DB или только своя DB | Spoolman + PrintOps DB (одна точка записи — `3d-api`) | Spoolman без форка: всё про катушку — в Spoolman. Адрес — в каноническом `Spool.location` (путь, oss.md §9.2; не дублировать в extra), сушка `last_dried` и заметки — extra fields; остальное — небольшая своя БД 3d-api |
| **D3** | Где живёт `3d-api` и данные | Odroid (compose рядом с HA, SQLite на HDD, бэкап) | Odroid |
| **D4** | Ставить ли на U1 extended firmware paxx12 (Spoolman SpoolLink, OpenRFID, запись `filament_detect`)? Риск для гарантии и стабильности | Сначала прочитать `filament_detect` на стоке (фаза 1). Решать после, отдельным тикетом | paxx12 — отдельно, позже, после текущего проекта (на стоке только читаем filament_detect) |
| **D5** | Пауза принтера при FAIL-гейте, если старт был не через нас | Только уведомление; авто-пауза — opt-in для 3 «опасных» проверок и только на своих принтерах | серьёзные FAIL при старте мимо нас → пауза + звонок через call-cli + tg |
| **D6** | Политика калибровок (новая партия: block или warn) | Тип и вендор — block; партия — warn | тип/вендор — block, партия — warn |
| **D7** | Bambu P1S (сосед) и A1 mini (Борис): какой доступ (LAN dev mode / облако read-only / вручную)? Нужно согласие владельцев | Read-only через ha-bambulab или ручной режим | доступа к чужим принтерам нет — только ручной режим |
| **D8** | Метки и станция: NTAG215 (OpenSpool + URI HA-tag) на свои катушки, NTAG213 + QR на ячейки; станцию ESP32 + PN532 + весы (~6–7 тыс. RSD) строить сразу или после телефонного варианта? | Сначала телефон (HA Companion) + метки; станцию — после того, как заработает ledger (Фаза 4) | метки NTAG215 (катушки) и NTAG213+QR (ячейки); телефон через HA Companion (URI HA-tag) и бот; станция позже |
| **D9** | HA-репо публичный: переносить 3D-историю в 3d-cli (тоже public) — ок? Есть ли в ней секреты (dataplicity URL, IP)? | Перед переносом прогнать gitleaks по отфильтрованной истории | источник всего 3D — 3d-cli (integrations/homeassistant); HA-репо подключает его submodule'ом |
| **D10** | Применение калибровок: писать прямо в Orca-профили пользователя или в профили в репо | Профили в репо + синк в Orca | профили в 3d-cli `profiles/` + `3d profiles sync` для Orca, Snapmaker Orca и Bambu Studio (двусторонний, с diff) |
| **D11** | Приоритеты «дальнейших возможностей» F1–F16 | — | все F1–F16 — фаза 5, после фаз 0–4; тикеты #193–#211 (метка `phase:5`, эпик #154) |

---

## 19. База знаний ТО, TUI, паритет HA, работа без ИИ, локальная LLM (эпик [#356](https://github.com/alex-mextner/3d-cli/issues/356))

Требование (tg#22455, 2026-10-16, сразу после случая «тугой X на K1 SE»): база знаний, связанная с отчётами ТО; удобный TUI, если `3d` запущен в интерактивной сессии; то же в HA; тулкит удобен без ИИ-агентов; в будущем — локальный агент на Ollama, если подписки станут дорогими.

| Тикет | Что |
|---|---|
| [#357](https://github.com/alex-mextner/3d-cli/issues/357) | KB: модель статей, `3d kb`, индекс и связи в 3d-api, поиск (§19.2) |
| [#358](https://github.com/alex-mextner/3d-cli/issues/358) | Первая статья: тугой X на K1 SE, 2026-10-16 (§19.2.6) |
| [#359](https://github.com/alex-mextner/3d-cli/issues/359) | Подсказки KB при открытии задачи, поломки, инцидента (§19.2.4) |
| [#360](https://github.com/alex-mextner/3d-cli/issues/360) | TUI: оболочка, вход, меню (§19.3) |
| [#361](https://github.com/alex-mextner/3d-cli/issues/361) | TUI: семь разделов (§19.3.3) |
| [#362](https://github.com/alex-mextner/3d-cli/issues/362) | HA: карточка KB, сервисы, проверка паритета (§19.4) |
| [#363](https://github.com/alex-mextner/3d-cli/issues/363) | Аудит «всё работает без LLM» и защита от регресса (§19.5) |
| [#364](https://github.com/alex-mextner/3d-cli/issues/364) | Абстракция LLM-провайдера, Ollama, `3d kb ask`, заметки к фото (§19.6) |

### 19.1 Архитектура
Одно ядро и тонкие поверхности. Ядро — функции `lib/` (то, что сейчас вызывают команды CLI) и 3d-api. Поверхности — CLI, TUI, HA и бот. Своей логики у них нет.
```
          CLI (3d …)      TUI (3d / 3d --tui)      HA (карточки, services)      бот
               \                 |                         |                    /
                \                |                  ingress-прокси HA          /
                 \               |                         |                  /
        действия (реестр Action: id, функция, argv CLI, подтверждение, HA-аналог)
                                 |
         lib/ (printops, printer_ops, calibrate, kb)  ──  3d-api (SQLite, события)
                                 |                               |
               JSONL-буферы без 3d-api              Spoolman, принтеры, камеры
                                 |
                  llm.ask(purpose, …) — необязательно, только §19.6
```
- **Реестр действий.** Каждое действие объявлено один раз: id, заголовок, функция ядра, шаблон команды CLI, правило подтверждения (как у `--yes`: старт печати и apply калибровки подтверждаются всегда), нужен ли 3d-api, HA-аналог (service или entity) либо `ha: none` с причиной. TUI строит меню из реестра и показывает рядом с действием эквивалентную команду, поэтому выученное в TUI работает в скриптах. По реестру тест проверяет паритет с HA (§19.4).
- **Без 3d-api** поверхности CLI и TUI читают и пишут те же JSONL-буферы, что и сейчас (`printer-todo`, `printer-history`, §7.4–7.5), плюс `kb-links.jsonl`. Действия, которым нужен 3d-api, в TUI недоступны и подписаны причиной.
- **LLM — сбоку.** Ни один шаг операций не ждёт LLM, чтобы получить результат. LLM вызывается только из функций §19.6 и только через `llm.ask`.

### 19.2 База знаний ТО ([#357](https://github.com/alex-mextner/3d-cli/issues/357))

#### 19.2.1 Где что хранится
- **Содержимое статьи — markdown в репо**, `kb/<id>.md`. Статья — знание, ему нужны ревью, история и diff, как у `fleet/maintenance-rules.yaml`. Фото статьи — в `kb/assets/<id>/` с sha256 во frontmatter.
- **Пути.** `id` — только `[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?`; любой другой id (в CLI, REST, frontmatter) отвергается до обращения к файлам. Статья — обычный файл (не симлинк) прямо в `kb/`, имя файла равно `id` во frontmatter, id уникальны; несовпадение или дубль — lint FAIL, и `3d kb sync` отказывает до начала транзакции. Фото задаётся только именем файла (`[a-z0-9][a-z0-9._-]{0,127}`, без каталогов) и лежит в `kb/assets/<id>/<имя>`. Файлы статей и фото открываются без гонки: `os.open(имя, O_RDONLY | O_NOFOLLOW, dir_fd=<fd каталога>)` и `fstat` открытого дескриптора (только обычный файл), поэтому подмена файла симлинком между проверкой и чтением не срабатывает. Иначе lint `kb/photos` даёт FAIL, а `3d kb show`, 3d-api и карточка HA файл не отдают. Тесты на `../`, абсолютный путь, вложенный каталог, симлинк статьи и фото наружу, подмену файла симлинком после проверки.
- **Записи — в 3d-api.** `kb_article` — индекс файлов репо (id, sha256 файла, разобранные поля, `removed_at`). Тело статьи и фото 3d-api отдаёт из развёрнутого checkout; файл, пропавший после sync, — 410 и отметка в индексе, а не 500. `3d kb sync` и деплой сверяют индекс со снимком checkout одной транзакцией: новые и изменённые файлы — upsert по id, статьи, которых в checkout больше нет (удаление, переименование), получают `removed_at` и выпадают из поиска и подсказок; их связи остаются в истории и показываются как «статья удалена». Сбой посреди sync откатывает всю транзакцию. Тесты: удаление, переименование, сбой посреди sync.
- **Ревизия базы.** `kb_revision` = sha256 отсортированных пар `(id, sha256 файла)`. CLI офлайн считает её по checkout, 3d-api — по индексу; она есть в каждом ответе поиска и подсказок. Совпадение результатов офлайн и онлайн гарантируется только при одинаковой ревизии и одинаковом наборе отзывов. При расхождении (статья добавлена локально, sync ещё не прошёл) TUI и `3d kb search` пишут одну строку «индекс 3d-api отстаёт: `3d kb sync`».
- `kb_link` — связи «запись ↔ статья» и отзывы «помогло / не то». Они появляются во время работы из бота, HA, TUI и CLI, и коммит для них не нужен. Таблица append-only: триггеры запрещают UPDATE и DELETE, как у `events`. Каждое действие пользователя — новая строка с новым `uid` (UUIDv7). Клиент создаёт `uid` один раз на нажатие и повторяет его при ретраях, поэтому повтор того же запроса строку не дублирует. `uid` — `UNIQUE`, рядом `payload_sha256` (канонический JSON строки без `uid`). Вставка — `INSERT … ON CONFLICT(uid) DO NOTHING` в одной транзакции, затем сверка хеша: тот же `uid` с тем же хешем — повтор (no-op), с другим — конфликт (REST 409, импорт останавливается с номером строки), как у импорта журнала настроек §7.4.
- **Действующий отзыв** по паре «запись × статья» — последняя по порядку записи строка этой пары. Порядок задаёт сервер: `seq` (монотонный номер при вставке в `kb_link`); в буфере — номер строки файла, при импорте строки получают `seq` в порядке файла. `at` клиента — только для показа и на порядок не влияет, поэтому строка с `at` из будущего не перебивает последующие исправления (тест). Двойной клик «помогло» по одной записи даёт один отзыв. Передумал — новая строка с другим `feedback`; отменить — строка с `feedback: null`. Бонусы ранжирования (§19.2.3) считаются по действующим отзывам, по одному на запись. Тесты: два параллельных импорта одной строки дают одну связь; тот же `uid` с другой статьёй — 409; «помогло», потом «не то» по одной записи — действует «не то»; двойной клик — +1, а не +2.
- Словарь связей один на все поверхности: `record_kind ∈ {incident, fault, maintenance, settings, task, calibration}` (ключи frontmatter `incidents`, `faults`, `maintenance`, `settings_changes`, `printer_tasks`, `calibrations`; ссылки в тексте `kb:<record_kind>/<id>`; флаги CLI `--incident|--fault|--maintenance|--settings|--task|--calibration`), `feedback ∈ {helped, not_it, null}` (CLI `--helped|--not-it|--clear`). `maintenance_rules` и `printers` во frontmatter — ссылки на `fleet/*.yaml`, не записи, поэтому в `record_kind` не входят.
- Без 3d-api `3d kb link` дописывает строку в `data_dir()/kb-links.jsonl` (`cli.paths.data_dir()`, по умолчанию `~/.local/share/3d-cli/`, учитывает `XDG_DATA_HOME`; схема `printops.kb_link/1`, `uid`, `flock`, `O_APPEND` + `fsync`), а `3d kb import FILE` идемпотентно переносит её в 3d-api по `uid`. Порядок записи тот же, что у буферов §7.4–7.5. Офлайн `kb.rank` берёт отзывы из этого буфера (без дублей по `uid`), онлайн — из `kb_link`.

#### 19.2.2 Статья
Frontmatter (схема `printops.kb_article/1`) на примере первой статьи:
```yaml
schema: printops.kb_article/1
id: k1se-x-stiff-graphite-bushings
title: "Тугой X на K1 SE: густая смазка забила графитовые втулки"
status: verified                 # draft | verified (подтверждено на принтере)
printer_models: [Creality K1 SE] # модели из fleet/printers.yaml; принтеры — в refs
components: [kinematics]         # словарь компонентов #166
symptoms:                        # фразы, как их говорят и пишут
  - "X тугой, голова туго ходит по X рукой, Y свободно"
  - "отверстия по X меньше, чем по Y"
  - "рябь по X, input shaper X нестабилен"
tags: [x_axis, bushings, grease, input_shaper, holes]
incident_kinds: []               # словарь §6, если применимо
refs:
  printers: [k1se]
  settings_changes: [bd1be79b-d5ae-4b54-a4ec-015003182cac, c7da2a69-9186-4fc9-81c6-1bdce029f2da]   # #264
  printer_tasks: [8b5a9ff8-005c-4402-8c86-573c9a03cf07, c6e2731c-cb3c-4d4e-b984-c7b8aab43853]   # #281
  maintenance_rules: [k1se-x-rods-bushings]                              # #166
  maintenance: []                # id записей журнала ТО, когда он в 3d-api
  faults: []
  incidents: []
  calibrations: []
photos:
  - {name: grease-1.jpg, sha256: "…", caption: "густая смазка с грязью у втулки на валу X"}   # файл kb/assets/<id>/grease-1.jpg
links:
  - {url: "https://wiki.creality.com/en/k1-flagship-series/k1c/maintenance-tips", title: "Creality: K1 maintenance tips"}
  - {url: "https://www.e-lager.rs/sr/proizvod/skf-psm101416a51skf", title: "SKF PSM 101416 A51 — замена втулок, если изношены"}
created: 2026-10-16
updated: 2026-10-16
```
Тело — пять обязательных разделов в этом порядке: `## Симптом`, `## Причина`, `## Проверка`, `## Исправление`, `## Профилактика`. Первый пункт раздела «Проверка» — то, что показывает подсказка (§19.2.4), поэтому он должен быть самостоятельным действием («снять ремни с каретки и подвигать голову рукой»). Ссылки на записи в тексте пишутся как `[settings bd1be79b](kb:settings/bd1be79b-…)`. `3d kb show` разворачивает их в краткое содержимое записи.

Lint (`3d lint --printops`): `kb/schema` — поля, разделы, неизвестные ключи; `kb/refs` — каждая ссылка находится (в 3d-api или в буферах JSONL; без обоих источников ссылка получает UNVERIFIED, а не PASS); `kb/photos` — файл есть, лежит внутри `kb/assets/<id>/` и sha256 совпадает; `kb/rule-backlink` — правило из `maintenance_rules` есть в `fleet/maintenance-rules.yaml`: нет правила — FAIL, нет самого файла — UNVERIFIED.

#### 19.2.3 Поиск и ранжирование (детерминированно, без LLM)
Одна функция `kb.rank(query, context)` в `lib/` для CLI офлайн и для 3d-api: статей сотни, не миллионы, поэтому поиск идёт в памяти, и результат один и тот же с 3d-api и без него. FTS5 не нужен.
- Нормализация: нижний регистр, `ё → е`, токены по буквам и цифрам, стемминг Snowball для RU и EN (пакет `snowballstemmer`, чистый Python, детерминирован; проверено: «тугой / туго / тугая» → «туг», «смазка / смазкой» → «смазк»), стоп-слова RU и EN. Формы, которые стеммер не сводит («втулки» → «втулк», «втулок» → «втулок»), покрывают фразы в `symptoms`; тест — запросы «X туго», «X тугой», «тугая ось X» находят первую статью.
- Текстовая оценка — Okapi BM25 (`k1 = 1.2`, `b = 0.75`, IDF = `ln(1 + (N − n + 0.5)/(n + 0.5))`, не отрицательна; корпус — все статьи без `removed_at`) по `title + symptoms + tags` с весом 2 и по телу с весом 1. Запрос, пустой после стоп-слов, даёт пустой результат.
- Бонусы контекста: модель принтера +3, компонент +3, `incident_kind` +2; по действующим отзывам (§19.2.1) той же модели и компонента: «помогло» +1 за запись (не больше +3), «не то» −1 за запись (не больше −2).
- Подсказка показывается, только если выполнены оба условия: (1) в `title`, `symptoms` или `tags` статьи совпали не меньше `kb.hint_min_terms` разных основ запроса (по умолчанию 2; если в запросе меньше основ — все), совпадения только в теле не считаются; (2) сумма ≥ `kb.hint_min_score` (по умолчанию 4.0). Оба порога — в `fleet/policy.yaml`. Условие (1) не зависит от размера базы, поэтому одни бонусы или одно случайное слово в теле подсказку не дают. Порядок при равенстве — по `id`. При одной ревизии базы и одном наборе отзывов одинаковый вход даёт одинаковый выход: это проверяет тест.

#### 19.2.4 Подсказки в потоках ([#359](https://github.com/alex-mextner/3d-cli/issues/359))
Подсказки появляются при открытии задачи принтера (`3d printer todo add`, производные задачи §7.5), поломки (`3d printops maint fault open`) и инцидента (`3d printops incidents …`, черновики из watch и vision). Контекст поиска: принтер и его модель, компонент или голова, `incident_kind`, текст `title/why/symptom`. Показываются до трёх подсказок: заголовок, первый шаг проверки, `3d kb show <id>`. Поле `kb_hints[{id, title, score, check}]` и `kb_revision` есть в `--json` этих команд и в ответах `POST /v1/printers/{p}/tasks`, `POST /v1/printers/{p}/faults`, `POST /v1/incidents`. Бот и карточки HA показывают первую подсказку с кнопками «помогло / не то», и каждая кнопка пишет `kb_link` с отзывом. Подсказка никогда не блокирует и не меняет поток. Если поиск не удался, подсказок нет, запись всё равно создаётся: в CLI — одна строка в stderr и прежний код выхода, в REST — `kb_hints: []` и строка в лог 3d-api, статус ответа прежний.

#### 19.2.5 CLI, REST, события
```
3d kb search QUERY [--printer P] [--component C] [--kind K] [--json]
3d kb show ID [--json]                          статья + связанные записи
3d kb new --id ID --from-incident N | --from-fault N | --from-task UID   черновик kb/<ID>.md (status: draft); ID по правилу §19.2.1, существующий файл не перезаписывается (ошибка)
3d kb link ID --incident N | --fault N | --maintenance N | --settings UID | --task UID | --calibration N [--helped | --not-it | --clear]
3d kb sync                                      сверка индекса 3d-api со снимком checkout (§19.2.1)
3d kb import FILE                               буфер kb-links.jsonl → 3d-api по uid
3d kb ask QUESTION [--llm require | --no-llm]    §19.6: сначала поиск; ответ LLM — только если настроен

GET  /v1/kb/articles?q=&printer=&component=&kind=      поиск (scope read)
GET  /v1/kb/articles/{id}                              статья + связи + разрешённые записи (read)
GET  /v1/kb/articles/{id}/photos/{name}               фото kb/assets/<id>/<name>; name по правилу §19.2.1, без каталогов (read)
GET  /v1/kb/hints?printer=&component=&kind=&text=      то же, что видят потоки §19.2.4 (read)
POST /v1/kb/articles/{id}/links  {record_kind, record_id, feedback: helped|not_it|null, uid}   (scope write)
POST /v1/kb/sync                                       (scope write; деплой и `3d kb sync`)
GET  /v1/kb/draft?incident=N | fault=N | task=UID     текст черновика статьи для коммита, ничего не пишет (read)
```
События: `kb.linked`, `kb.synced`. Запись в `kb/` (новая статья, правка) идёт только через git (PR), а не через API: так знание проходит ревью.

#### 19.2.6 Первая статья ([#358](https://github.com/alex-mextner/3d-cli/issues/358))
`kb/k1se-x-stiff-graphite-bushings.md`, случай 2026-10-16:
- **Симптом.** X очень туго ходит рукой, Y свободно; отверстия Ø8 по X 7,6, по Y 7,8–7,9; рябь по X; input shaper X нестабилен.
- **Причина.** Густая смазка на валах X забила латунно-графитовые втулки каретки (10×14×45, валы 10 мм).
- **Проверка.** Снять ремни с каретки и подвигать голову по валам рукой: туго — каретка или валы, свободно — ремни или шкивы. Осмотреть втулки на смазку и грязь.
- **Исправление.** Промыть валы и втулки спиртом (IPA), густую смазку не наносить. Вернуть ремни с прежним натягом. Перемерить input shaper X и рамку 50 мм. Пользователь: «Оооо спирт помог» (tg#22453).
- **Профилактика.** Правило ТО `k1se-x-rods-bushings` (200 ч печати или 90 дней, IPA, без густой смазки) в `fleet/maintenance-rules.yaml` (#166).
- **Ссылки.** Записи журнала настроек `bd1be79b…` и `c7da2a69…`, задачи `8b5a9ff8…` и `c6e2731c…`, три фото смазки из Telegram (tg 22449–22451), вики Creality; втулки SKF PSM 101416 A51 — только если изношены.

### 19.3 TUI ([#360](https://github.com/alex-mextner/3d-cli/issues/360), [#361](https://github.com/alex-mextner/3d-cli/issues/361))

#### 19.3.1 Вход
- `3d` без аргументов, когда stdin и stdout — оба TTY, и `3d --tui` открывают меню. `THREED_NO_TUI=1` отключает автозапуск. `3d --tui` без TTY на stdin или stdout (`3d --tui | tee log`) выходит с UsageError «TUI нужен терминал» и кодом usage; меню не рисуется в пайп.
- Без TTY (пайп, cron, агент) `3d` без аргументов печатает ту же справку, что и сейчас, байт в байт, с тем же кодом 0 (`dispatch.main` сводит пустой argv к `help`). Это проверяет тест. `3d help`, `--help` и все подкомандные вызовы не меняются.
- Без установленного extra `tui`: `3d` без аргументов в TTY печатает справку и одну строку в stderr, как поставить extra; stdout и код 0 те же, что без TTY. `3d --tui` выходит с кодом MissingDependency и подсказкой установки.

#### 19.3.2 Библиотека: Textual
Выбор — [Textual](https://textual.textualize.io/) (MIT, на Rich), extra `tui` в `pyproject.toml`, импорт только внутри точки входа TUI. `tests/test_imports.py` по-прежнему держит `3d help` лёгким.

| Вариант | За | Против |
|---|---|---|
| **Textual** | готовые виджеты (DataTable, Tree, Input, Select, Checkbox, Markdown для статей KB, TabbedContent); асинхронный цикл (опрос 3d-api и SSE без потоков); headless-тесты через `App.run_test()` и Pilot; SVG-снимки экранов (pytest-textual-snapshot) для доказательств в PR; работает по SSH | лишняя зависимость (rich, textual), поэтому только extra |
| prompt_toolkit | лёгкий, хорош для REPL и автодополнения | полноэкранные формы и таблицы собирать вручную; нет готовых снимков для тестов |
| curses (stdlib) | без зависимостей | нет виджетов, Unicode и цвет — вручную, тесты только через pty |
| urwid | зрелый | старый API, мало виджетов для форм, без снимков и Pilot |
| внешние `fzf` и `gum` | быстро | зависимость от бинарей, нет состояния экрана, не тестируется в pytest |

Решает тестируемость: у каждого раздела должны быть headless-тесты и снимки, а у Textual они есть из коробки.

#### 19.3.3 Структура
Главное меню (стрелки, Enter, Esc — назад, `/` — поиск, `q` — выход), строка состояния: 3d-api доступен или офлайн (буферы), принтеры онлайн, LLM (`none` / `ollama:<model>` / недоступен). Ширина от 80×24.

| Раздел | Экраны и действия | Ядро и команда |
|---|---|---|
| Принтеры | список: статус, задание, ETA, открытые задачи и поломки; карточка принтера: кадр камеры, watch, гейт старта, состояние стола | `3d printer status|watch|start`, `3d printer vision` |
| Печать и очередь | очередь; задание: preflight с причинами FAIL и WARN; старт с подтверждением; порядок и назначение принтера | `print-preflight`, `3d printer start`, `/v1/jobs` (§15 п.17) |
| Катушки | слоты принтеров, mismatch; load / unload / move / measure; сушка; склад-дерево | `3d printops spool|slots|loc|dry` |
| Калибровка | сессия #277 по шагам: план (галочки skip, «не дольше»), gen с превью, print (подтверждение), путь к фото или камера, результат анализа, выбор значений, apply (подтверждение), откат; продолжение сессии из другого канала | `3d calibrate start|status|next|gen|print|skip|photo|analyze|choose|apply|abort` (#278, §8.3.6) |
| Задачи | список по принтеру: блокирует / удобно сейчас / ждёт; add, done, dismiss, snooze; подсказки KB при add | `3d printer todo` (#281) |
| ТО | due по правилам; журнал; поломки (open, close); замена сопла; инциденты (add с подсказками KB, stats) | `3d printops maint`, `3d printops incidents` |
| База знаний | поиск, статья (Markdown), связанные записи, фото (путь и открытие внешним просмотрщиком), `new --from-incident`, `ask` (§19.6) | `3d kb` |

Мутации подтверждаются диалогом с текстом эквивалентной команды. Ошибки ядра (`ThreeDError`) показываются тем же `render()`, что в CLI, с remediation.

### 19.4 Паритет с HA ([#362](https://github.com/alex-mextner/3d-cli/issues/362))
HA — та же поверхность над 3d-api, через существующий ingress-прокси с авторизацией HA, который подставляет токен 3d-api (§8.3.6). Для HA это токен клиента `ha` со scope `read` и `write` (§11); маршруты `/v1/kb/*` проверяют scope так же, как остальные (§19.2.5), и запрос без токена получает 401 — это проверяет интеграционный тест #362. Тест паритета проходит по реестру действий (§19.1): у каждого действия есть service или entity в `integrations/homeassistant` либо `ha: none` с причиной. Новое действие без аналога в HA роняет CI.

| Раздел TUI | HA | 3d-api | Тикет |
|---|---|---|---|
| Принтеры | существующие сенсоры `sensor.printer_*`, камера, u1-inspector; `sensor.printops_<printer>_tasks` | `/v1/printers` | §12, #285 |
| Печать и очередь | карточка «Очередь», кнопка preflight; `sensor.printops_queue_<printer>` | `/v1/jobs*`, `/v1/preflight` | §12, §15 п.17, 19–20 |
| Катушки | карточки «Заправка», «Склад», «Сушка»; `printops.load_spool|unload_spool|move_spool|start_drying|measure` | `/v1/printers/{p}/slots*`, `/v1/spools*`, `/v1/drying*` | §12, §15 п.19–20 |
| Калибровка | карточка «Калибровка»; `printops.calibrate_start|next|gen|print|skip|photo|choose|apply|abort`; `status` — сенсоры сессии (шаг, ожидаемое действие); `analyze` — без отдельного service: 3d-api запускает анализ сам на загрузку фото, результат — в сенсоре и карточке | `/v1/calibration/*` | #275, #280 |
| Задачи | `todo.printops_<printer>`; карточка «Калибровки и ТО» | `/v1/tasks*`, `/v1/printers/{p}/tasks` (оба — §11.1) | #285 |
| ТО и инциденты | карточки «Калибровки и ТО», «Инциденты»; `printops.report_incident`, `printops.maint_log`, `printops.fault_open|fault_close` | `/v1/printers/{p}/maintenance|faults`, `/v1/incidents*` | §12, #362 |
| База знаний | карточка «База знаний» (поиск, статья, связи, фото); «черновик из инцидента» показывает текст черновика и команду `3d kb new --id … --from-incident N` для коммита — через API статьи не создаются (§19.2.5); подсказка с кнопками «помогло / не то» на карточках задач и инцидентов; `printops.kb_search` (возвращает данные — для скриптов и Assist), `printops.kb_link` | `/v1/kb/*` | #362 |

Строки, у которых уже есть свой тикет, выполняются там. #362 делает только раздел KB, недостающие services ТО (`maint_log`, `fault_open|close`) и тест паритета.

### 19.5 Без ИИ ([#363](https://github.com/alex-mextner/3d-cli/issues/363))
Правило: **операции детерминированы.** Операции — это принтеры, очередь, катушки, калибровка, задачи, ТО, инциденты, KB, HA и TUI. Их результат (значения калибровки, FAIL гейта, задача, подсказка) получается без LLM. LLM может только добавить заметку рядом (§19.6). Моделирование (`3d generate`, `3d match`, `3d judge`, `3d bench`, `3d ai`, recover-blockout) — отдельный класс: там ИИ и есть продукт, правило на него не распространяется, но у каждой такой команды остаётся детерминированный `--dry-run` или mock, как сейчас.

Инвентаризация на 2026-10-16 (main, `grep resolve_backend|ai.backends`; полную таблицу ведёт #363):

| Где | ИИ для чего | Класс | Детерминированный путь |
|---|---|---|---|
| `lib/ai/backends.py`, `lib/ai/__init__.py` (`ai.json`) | провайдеры claude, codex, opencode, ollama, mock; автовыбор claude-first | общее | mock; провайдер `none` для операций (§19.6) |
| `lib/match_loop.py` (`3d match`), `lib/img3d_loop.py` (`recover-blockout`) | критик и veto по картинке | моделирование | `--dry-run` |
| `lib/ai/design.py` (`3d generate`), `lib/ai/bench.py` (`3d bench`), `lib/ai/judge.py` (`3d judge`), `3d ai` | генерация и оценка .scad | моделирование | mock |
| `lib/ollama.py` (`3d ollama`) | план запроса, сеть не трогает; отдельный `ollama.json` | общее | сливается в `ai.json` (#364) |
| `lib/printops/*`, `lib/printer_ops/*`, `3d calibrate`, `3d printer` | вызовов LLM нет; `3d printer vision` — ONNX-детектор на CPU (не LLM, детерминирован) | операции | уже без LLM — закрепить тестом |
| §11.3 голосовые интенты (#172) | LLM-разбор свободной речи | операции | правила покрывают фразы §11.3; LLM — запасной путь, по умолчанию выключен |
| §8.3.5 vision калибровок (#273), `calibration.evidence` «VLM notes», `settings_change.source: vlm` (#264) | чтение фото | операции | OpenCV даёт значения; VLM — только заметка, значения после подтверждения |

Защита от регресса: фикстура pytest `no_llm` (провайдер `none`, на PATH нет claude, codex и opencode, `Backend.complete` падает) для e2e операций и Pilot-тестов TUI; тест импортов — модули операций не импортируют `ai.backends` напрямую, только `llm` из §19.6.

### 19.6 LLM-провайдер и Ollama ([#364](https://github.com/alex-mextner/3d-cli/issues/364))
**Конфиг — один файл**, `ai.json`, но два раздела с разными загрузчиками. Путь файла у обоих загрузчиков один: `$THREED_AI_CONFIG`, иначе `~/.config/3d-cli/ai.json` — так, как его уже находит `ai.load_backend_config()`; тест: при `THREED_AI_CONFIG` ни один загрузчик не читает домашний файл. Верхний уровень (`backend`, `endpoint`, `model`) — моделирование, читает его `ai.load_backend_config()`, как сейчас; он не меняется. Секция `llm` — операции, читает её только `llm.load_config()`, и `OllamaBackend` для операций получает именно эту секцию. Поэтому `{"backend": "ollama", "model": "llava", "llm": {...}}` даёт модели моделирования и операций независимо (e2e-тест на обе пары):
```json
{"backend": "ollama", "endpoint": "http://127.0.0.1:11434", "model": "llava",
 "llm": {"provider": "none",
         "endpoint": "http://127.0.0.1:11434",
         "model": "qwen2.5:7b", "vision_model": "llama3.2-vision",
         "timeout_s": 60,
         "features": {"kb_qa": true, "photo_notes": false, "intent_fallback": false}}}
```
Верхние `backend/endpoint/model` — моделирование, как сейчас. `llm.provider` ∈ `none | ollama | claude | codex | opencode`. Загрузчик — строгий `json.loads`, как у `ai.load_backend_config()`, поэтому комментарии в файле недопустимы.

**Адрес Ollama.** Разрешены только loopback (`127.0.0.0/8`, `::1`), RFC1918, Tailscale `100.64.0.0/10`, IPv6 ULA `fc00::/7` (включает Tailscale `fd7a:115c:a1e0::/48`). Link-local (`169.254.0.0/16`, `fe80::/10`) и всё остальное — отказ. Имя хоста резолвится при каждом соединении (A и AAAA); соединение разрешено, только если **все** полученные адреса входят в эти диапазоны, и идёт на проверенный адрес. Редиректы HTTP запрещены (обработчик редиректов отключён), прокси из окружения для этого клиента не используются. Тесты: публичный IPv4 и IPv6; `[::1]`; ULA; link-local; имя, резолвящееся в публичный адрес; имя с одним частным и одним публичным адресом; редирект с разрешённого адреса на публичный.

**Интерфейс.** `llm.ask(purpose, prompt, images=None, schema=None) → LLMAnswer | LLMUnavailable` поверх существующих классов `Backend`. `purpose ∈ {kb_qa, photo_notes, intent_fallback}` должен быть включён в `features`. Для операций провайдер не выбирается автоматически: используется только явно настроенный, поэтому подписка не тратится незаметно. HTTP у `OllamaBackend` сейчас идёт через обычный `urllib.request.urlopen` (прокси из окружения, редиректы) и принимает любой адрес. #364 заменяет это защищённым транспортом с правилами «Адрес Ollama» выше: свой opener без `ProxyHandler` и без редиректов, соединение на проверенный IP. Транспорт общий для обоих путей Ollama (моделирование и операции), а тесты проходят через сам `OllamaBackend`.

**Кэш ответов** — на диске, `cache_dir()/llm/<key>.json` (`cli.paths.cache_dir()`, учитывает `XDG_CACHE_HOME`), TTL 30 дней. Ключ — sha256 канонического JSON `{provider, endpoint (нормализованный), effective_model, model_digest, purpose, prompt_version, prompt, schema_sha256, images_sha256[]}`. `effective_model` — `vision_model` для запросов с картинками, иначе `model`. `model_digest` — digest модели из Ollama `/api/tags` (для CLI-провайдеров — имя модели и версия CLI), поэтому повторный `ollama pull` того же тега даёт новый ключ. Кэшируются только принятые ответы: отброшенные (невалидная схема, нет ссылок) не кэшируются. Запись атомарна: временный файл, `fsync`, `rename`. Параллельные одинаковые запросы сериализуются `flock` на файл блокировки ключа. Получив блокировку, запрос заново проверяет кэш: есть валидная запись — читает её; нет (первый получил таймаут или отказ) — делает свой запрос, и его неудача деградирует по правилам ниже. Тесты: смена провайдера, адреса, `vision_model` и digest при том же имени модели — промах кэша; два параллельных одинаковых `kb ask` — один запрос к модели; первый из двух падает по таймауту — второй делает свой запрос и выходит с кодом 0.

**Где LLM полезна (первые функции):**
- `3d kb ask ВОПРОС` — сначала всегда печатается детерминированный поиск (§19.2.3, top-5). Если `kb_qa` включён и провайдер доступен, ниже идёт ответ модели по тексту этих статей со ссылками на их `id`. Ответ без валидной ссылки отбрасывается с заметкой. Тот же вызов доступен в TUI (раздел KB).
- Заметки к фото (`photo_notes`): к фото калибровки и инцидента VLM пишет короткий комментарий (`source: llm:<model>`, в `evidence`) рядом с результатом OpenCV. Значения калибровки и поля инцидента он не меняет; правка — только после подтверждения пользователя.
- Запасной разбор голосовых интентов (`intent_fallback`, #172) — только для фраз, которые не разобрали правила, и всегда с подтверждением (§11.3).

**Правила деградации:**
1. Провайдер `none` (по умолчанию) — сеть не трогается, выводится детерминированный результат, код 0.
2. Провайдер недоступен, таймаут, невалидный JSON по `schema`, ответ без ссылок — детерминированный результат и одна строка «LLM: недоступен (<причина>)», код 0. Повторной попытки и ожидания сверх `timeout_s` нет.
3. `--llm require` — недоступность становится ошибкой с кодом MissingDependency; `--no-llm` выключает LLM на один вызов.
4. Вывод LLM всегда помечен (`source: llm:<model>`) и никогда сам не меняет данные: не пишет `settings_change`, не закрывает задачи, не применяет калибровку.
5. `3d llm status [--json]` показывает провайдера, модели, доступность и включённые функции; строка состояния TUI и сенсор HA показывают то же.

### 19.7 Порядок реализации
1. #357 (модель KB, `3d kb`, ранжирование) и #360 (оболочка TUI, реестр действий) — параллельно.
2. #358 (первая статья; нужны lint из #357 и правило `k1se-x-rods-bushings` из #166); #359 (подсказки; нужны #165, #166, #281).
3. #361 (разделы TUI; калибровка — после #277); #363 (аудит и фикстура `no_llm`) — параллельно.
4. #362 (HA: KB и паритет по реестру из #361); #364 (провайдер LLM, `3d kb ask`, заметки к фото).
5. Приёмка эпика #356: инцидент «X тугой» в TUI → подсказка первой статьи → связь → видно в HA; весь сценарий при выключенном ИИ; `3d kb ask` с Ollama и без неё.

## 20. Подбор цвета и быстрая калибровка смесей (эпик [#410](https://github.com/alex-mextner/3d-cli/issues/410))

Требование (tg#22816, 2026-10-17): тулинг подбора и быстрой калибровки цветов для смешивания по слоям на U1. Повод — случай того же дня: плитка PTA/PTB в смеси `431` вышла с ΔE 16,6 от макета. Маленький тестер поймал бы это до печати. Плитка лежала плашмя, поэтому её верх показал один филамент. Два hex в смеси были оценками (Snapdragon yellow и бордовый).

| Тикет | Фаза | Что |
|---|---|---|
| [#116](https://github.com/alex-mextner/3d-cli/issues/116) | 1 | модель рецептов: оптическое среднее, CIEDE2000, видимость полос; `3d mix recipes` (§20.3) |
| [#411](https://github.com/alex-mextner/3d-cli/issues/411) | 1 | `3d mix check FILE.3mf` и строка `mix face` в `print-preflight`: смешанная деталь показывает цвет на стенках (§20.6) |
| [#412](https://github.com/alex-mextner/3d-cli/issues/412) | 1 | виды калибровок `filament_color` и `mix_color`, измерение каталога `mix` (§20.4) |
| [#413](https://github.com/alex-mextner/3d-cli/issues/413) | 1 | `3d mix tester`: цель → кандидаты из загруженных катушек → стол из стоячих пластинок (§20.2) |
| [#415](https://github.com/alex-mextner/3d-cli/issues/415) | 2 | цель по фото или рендеру макета: область, баланс белого, медиана в Lab |
| [#416](https://github.com/alex-mextner/3d-cli/issues/416) | 2 | `3d mix readout`: фото тестера → измеренный цвет каждой пластинки (§20.5) |
| [#417](https://github.com/alex-mextner/3d-cli/issues/417) | 2 | `3d mix fit` + `3d mix record`: поправки цвета катушек и измеренные смеси → записи в 3d-api (§20.3, §20.5) |
| [#418](https://github.com/alex-mextner/3d-cli/issues/418) | 2 | строка `mix_calibration` в `print-preflight`: нет свежего измерения смеси → WARN и команда тестера (§20.7) |
| [#419](https://github.com/alex-mextner/3d-cli/issues/419) | 3 | `mix face` по одному G-code, без проекта (§20.6) |
| [#420](https://github.com/alex-mextner/3d-cli/issues/420) | 3 | `3d mix tester --shelf`: кандидаты из катушек Spoolman, которые не загружены |

Связанные: [#333](https://github.com/alex-mextner/3d-cli/issues/333) (необязательный тест смешивания в столах v2, §8.3) использует тот же тестер; [#202](https://github.com/alex-mextner/3d-cli/issues/202) (F8, каталог измеренных цветов) закрывается видом `filament_color`; [#309](https://github.com/alex-mextner/3d-cli/issues/309) правит hex катушки в Spoolman; [#115](https://github.com/alex-mextner/3d-cli/issues/115) считает видимые цвета по G-code.

### 20.1 Как U1 смешивает цвет
- Full Spectrum (смешанные филаменты Snapmaker Orca) не смешивает пластик. На каждом слое область печатает **один** физический филамент, а от слоя к слою филаменты чередуются. Строка `mixed_filament_definitions` с ручным шаблоном кончается на `cm1,<цифры>` (одна цифра — один слой, цифра — слот). Строка Local-Z кончается на `cm0`: доли, каждый слой делится на подслои. Виртуальный филамент получает id = число физических + 1 + номер строки, строки считаются с нуля: при 4 физических первая строка — филамент 5 (`slicing/head_sequence.py`).
- Глаз смешивает полосы только на **стенках**. Горизонтальный верх показывает последний слой, то есть чистый филамент; на рельефе каждая терраса показывает свой слой. Отсюда правило §20.6.
- SPEC 58 (babushkin-dom, 2026-10-14): лучше всего выглядят короткие равномерные шаблоны с периодом 1–3 слоя. Длинные шаблоны дают видимые полосы, даже если их средний цвет ближе.
- Окно «Match» и превью Snapmaker Orca считают пигментной моделью и для непрозрачного PLA ошибаются на ΔE 14–21. Модель §20.3 считает оптическое среднее.

### 20.2 Тестер (`3d mix tester`, #413)
```
цель (hex; фото — #415) ─┐
катушки: --slots | --printer (загруженные; #420 — полка Spoolman) ─┴→ кандидаты + ΔE (§20.3)
   → NAME.3mf (стоячие пластинки, эталоны, номера) + NAME.mix.json + NAME.png
   → слайс в Snapmaker Orca → print-preflight (head seq, mix face) → printer start
   → фото → readout (§20.5) → record → записи 3d-api
```
- **Кандидаты.** Все целочисленные веса по загруженным катушкам с длиной цикла не больше `--max-cycle` (по умолчанию 3, SPEC 58) и НОД весов 1. Шаблон — веса, разнесённые по циклу равномерно (как `build_weighted_gradient_sequence` в Snapmaker Orca). В тестер попадают лучший кандидат и разные варианты вокруг него: светлее, темнее, теплее, зеленее, короче. Всего `--count`, по умолчанию 6.
- **Стол.** Каждый кандидат — пластинка 20 × 2,4 мм высотой `--height` (по умолчанию 6 мм, 50 слоёв по 0,12). Пластинка стоит, поэтому её лицевая грань вертикальна и сам тестер проходит `mix face`. Каждая загруженная катушка печатает такую же пластинку-эталон: по эталонам считывание делает баланс белого и поправку цвета катушки. Номер стоит перед пластинкой семисегментными цифрами, печатается самой тёмной катушкой. QR со ссылкой на манифест добавит #322.
- **Цена.** Пластинка весит около 0,3 г. Время определяют смены голов. Эталоны печатаются на каждом слое, поэтому на каждом слое работают все загруженные головы: смен на слой столько же, сколько загруженных голов. Манифест пишет число слоёв, смен и оценку времени.
- **Проект.** NAME.3mf — проект Snapmaker Orca: загруженные катушки как физические филаменты, одна строка `mixed_filament_definitions` на кандидата, у пластинки — её виртуальный филамент. Настройки проекта — системные пресеты U1 из установленного Snapmaker Orca (пробный экспорт CLI, как у построителей столов) или из `--base-3mf` готового проекта U1.
- **Манифест** `NAME.mix.json`, схема `printops.mix_tester/1`: цель; слоты (hex, откуда hex — Spoolman, измерение `filament_color` или ввод; id катушки и филамента, если известны); кандидаты (шаблон, веса, предсказанный hex, ΔE, ΔE полос на 30 см, ключ смеси §20.4); прямоугольники пластинок и номеров на столе в мм — по ним считывание находит пластинки на фото. Ключ смеси есть, только если id филаментов всех её слотов известны. Для слотов, заданных одним hex (`--slots 1=#000000`), ключа нет, и `3d mix record` такую смесь не запишет, пока слоты не связаны с катушками.

### 20.3 Модель цвета и ΔE
- Цвет смеси — среднее по площади в **линейном** sRGB (доли слоёв = веса), затем CIELAB D65. Отличие — CIEDE2000 (`lib/printops/color.py`, проверено на 34 парах Sharma et al.). Модель перенесена из исследования Full Spectrum (babushkin-dom `docs/full-spectrum/tools/colour.py`, `recipes.py`; #116).
- Видимость полос: профиль стенки (один филамент на слой, цикл повторяется) размывается гауссианой с σ = 1′ дуги на расстоянии взгляда (30 см и 1 м) в линейном свете. ΔE полос — наибольшее отличие размытого профиля от его среднего. Меньше 2 полос не видно, больше 5 видно.
- Оценка кандидата: ΔE + 0,2 × ΔE полос на 30 см. Полосатая стенка хуже, чем чуть другой оттенок.
- **Калибровка модели**, по порядку доверия:
  1. есть запись `mix_color` для этой смеси (§20.4) — предсказание равно измерению;
  2. есть запись `filament_color` катушки — её hex заменяет hex Spoolman. Первые на очереди — оценённые Snapdragon yellow и бордовый;
  3. #417: подбор эффективных цветов филаментов на стенке. Он учитывает полупрозрачность: тонкий слой светлого пластика на тёмном выглядит темнее. Это МНК по ΔE всех измеренных смесей с регуляризацией к чистому эталону. Отчёт показывает ошибку до подбора и после.

### 20.4 Данные: виды калибровок `filament_color` и `mix_color` (#412)
Каталог §7.2 получает два вида, тип значения `color` (`#RRGGBB`, хранится в верхнем регистре) и новое измерение scope `mix` (только вместе с `printer`). Сокращённо, полностью — в `fleet/calibrations.yaml`:
```yaml
filament_color:            # как филамент реально печатается (поправка hex катушки)
  scope: [filament]
  lot_sensitive: true
  baseline: required       # схема каталога: виды со scope filament всегда required
  methods: [mix_tester_photo, colorimeter, manual]
  values: {hex: color, spoolman_hex: color?, delta_e: number?, lab_l/lab_a/lab_b: number?}
  apply: [journal]
mix_color:                 # измеренный цвет одной смеси на одном принтере
  scope: [printer, mix]
  baseline: assumed
  methods: [mix_tester_photo, manual]
  values: {hex: color, predicted_hex: color?, delta_e_predicted: number?}
  apply: [journal]
```
- **Ни один из видов не требуется перед печатью.** `filament_color` выключен политикой (`fleet/policy.yaml`, `calibrations.kinds.filament_color: off`). Поэтому движок требований его не перебирает: нет `no_spool`, `new_lot` и задач принтера (§7.5) из-за неизмеренного цвета. Без записи модель берёт hex Spoolman. Виды со scope `mix` движок пропускает сам (ниже).
- **Ключ смеси** `mix`: высота слоя и цикл слоёв в id филаментов Spoolman (`lib/printops/mix_key.py`). Цикл сначала сокращается до наименьшего повторяющегося куска (`4343` → `43`). Потом из всех его поворотов берётся наименьший при сравнении id **как чисел**: `h0.12:f3.f12.f7`. Поэтому `431` и `314` — одна смесь. Для строки Local-Z ключ — доли в целых процентах, по возрастанию id, сумма 100: `h0.12:lz:f1=25,f4=75`. Любой ключ на входе API приводится к этой форме. Слоты в ключ не входят: та же смесь на других головах того же принтера — та же запись.
- **Движок #167.** Миграция `0081_calibration_mix.sql` добавляет колонку `mix`. `RunRequest` и REST принимают поле `mix`; виду со scope `mix` оно обязательно, остальным запрещено. Новая запись того же принтера и ключа заменяет старую (`superseded_by`); запись другого ключа старую не трогает. `GET /v1/calibrations?mix=` и `3d calibrate ls --mix` фильтруют по ключу. Требования по голове и по принтеру (§8, задачи §7.5) виды со scope `mix` не перебирают: их проверяет строка §20.7 по смесям конкретного стола.
- **История.** Записи калибровок — это и есть история (#264). `3d mix record` (#417) пишет их через `POST /v1/calibrations`, а без 3d-api — в буфер журнала настроек.

### 20.5 Считывание (#416, #417)
```
фото (верхняя камера #342 / телефон)
  → выпрямление и масштаб: фидуциалы и номера/QR (#273, #322; 3d calibrate rectify)
  → пластинки по прямоугольникам манифеста; края и номер маскируются
  → баланс белого по белому эталону (или серой карте) → медиана в Lab по каждой пластинке
  → mix-readout/1: измеренный hex, ΔE к предсказанию и к цели; размеченный PNG
  → 3d mix fit (поправки катушек) → 3d mix record → filament_color, затем mix_color в 3d-api
```
Верхняя камера видит только верх стоячей пластинки, то есть один слой. Поэтому для верхней камеры пластинки снимают со стола и кладут лицом вверх на белую карту: раскладку задаёт манифест. С телефона можно снимать стоящий ряд спереди. Фото с QR другого манифеста отвергается. Все записи одного считывания несут в `params` sha256 его `mix-readout/1`; `3d mix record` пишет сначала `filament_color`, потом `mix_color`.

### 20.6 Ориентация: цвет на стенках (#411, #419)
- **Правило.** Для каждой детали в смешанном филаменте (ручной шаблон или Local-Z) площади граней в координатах стола делятся по нормали грани: верх — нормаль смотрит вверх и отклоняется от вертикали не больше чем на 30° (n_z ≥ cos 30°); стенки — нормаль в пределах 30° от горизонтали (|n_z| ≤ sin 30°); склоны — остальные грани с нормалью вверх. Грани с нормалью вниз лежат на столе или под деталью и не видны. **FAIL**, если верх больше половины стенок: с одной стороны видна примерно половина стенок, но весь верх. **WARN**, если склонов больше половины стенок (террасы). Деталь в физическом филаменте не проверяется.
- **3MF** (`lib/slicing/mix_faces.py`). Входы: `mixed_filament_definitions`, экструдеры деталей в `model_settings.config` (экструдер `0` — как у объекта), сетки с преобразованиями build и component. Плитка 20 × 20 × 3 плашмя: верх 400 мм² при половине стенок 120 мм² — FAIL. Стоя: верх 60 мм², стенки 920 мм² — PASS.
- **G-code** (#419), когда проекта нет. Смешанный объект — тот, в котором работает больше одной головы: шаблон чередует головы по слоям, Local-Z печатает обе головы на каждом слое (`head_sequence` такие объекты не проверяет, а здесь они входят). Видимый верх — длина экструзии `;TYPE:Top surface` × `;WIDTH:`, стенки — длина `;TYPE:Outer wall` × высоту слоя. Правило то же. Многоцветная деталь из физических филаментов (разные части разными головами) тоже попадёт сюда, поэтому без проекта строка даёт WARN, а не FAIL.
- **Где проверяется.** Генератор тестера проверяет свой стол. `3d mix check FILE.3mf` проверяет проект до слайса. `print-preflight` выводит строку `mix face` по проекту рядом с G-code: без проекта — SKIP, а после #419 — по G-code. Позже — post-processing Orca (§9.2, рубеж 1).

### 20.7 Проверка перед печатью (#418)
Строка `mix_calibration` в гейтах PrintOps (§9.1). Для каждого смешанного филамента стола: шаблон из проекта → головы → катушки на них (3d-api) → id филаментов → ключ §20.4 → действующая запись `mix_color` этого принтера. Действующая значит не заменена, не истекла и не старше записей `filament_color` своих филаментов. Запись `filament_color` того же считывания (тот же sha256 в `params`) свежести не отменяет. Нет записи → WARN `mix 431 (T4 Snapdragon, T3 оранжевый, T1 бордовый) не измерена: 3d mix tester --target '#…' --printer u1`. ΔE измерения к цвету детали в модели выше порога политики → WARN, по политике FAIL. Уровни — `fleet/policy.yaml` `calibrations:`, как у `calibration_current`; принятие — `--accept calibration:mix_color --reason …`.

### 20.8 Порядок реализации
1. Фаза 1, параллельно: #411 (гейт ориентации и линтер), #412 (виды и движок), #116 + #413 (модель и тестер, MVP: цель hex, слоты явно или с принтера).
2. Фаза 2: #416 (нужны #273 и #342) → #417 → #418; #415 — в любой момент.
3. Фаза 3: #419, #420; тестер в калибровочной сессии (#277, #333).
4. Приёмка эпика: цвет плитки PTA/PTB → тестер на U1 меньше 30 мин → фото → исправленный рецепт в пределах ΔE 3 от измеренной пластинки; записи видны в `GET /v1/calibrations`.
