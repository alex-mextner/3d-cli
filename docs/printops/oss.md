# PrintOps: обзор OSS/SaaS для учёта филамента, очереди и истории по разнородному парку

Дата среза: 2026-10-14. Звёзды, лицензии и даты `pushed_at` / последнего релиза взяты из GitHub REST API (`gh api repos/...`) в этот день. Всё, что не проверено по коду, докам или README, помечено **[INFERENCE]**.

Парк: Snapmaker U1 (Klipper/Moonraker, 4 головы, 192.168.0.26), Creality K1 SE на стоке (Creality WS, Moonraker только после root), Bambu P1S соседа, Bambu A1 mini + AMS lite друга, принтеры хакспейса.

---

## 1. Spoolman: ядро экосистемы

| | |
|---|---|
| Repo | https://github.com/Donkie/Spoolman |
| Лицензия | MIT |
| Активность | ★≈2.9k, последний релиз v0.27.0 от 2026-09-25, push 2026-10-12. Релизы примерно раз в 2–6 недель (v0.24 в 07/2026 … v0.27 в 09/2026) |
| Где работает | Сервер: Docker/Python, FastAPI, БД SQLite/Postgres/MySQL/CockroachDB. На принтере не работает |
| Мульти-принтер | Сам о принтерах ничего не знает. Принтер для него — просто строка в `location` |
| Мульти-тул | Понятия «голова/слот» нет. Это задача Moonraker-макросов или адаптеров |

### Модель данных (по коду `spoolman/database/models.py`, коммит d102f94)
- **Vendor**: `name`, `empty_spool_weight`, `comment`, `external_id`, `extra{}`.
- **Filament**: `vendor_id`, `name`, `material`, `price`, `density`, `diameter`, `weight` (нетто полной катушки), `spool_weight`, `article_number`, `color_hex`, `multi_color_hexes`, `multi_color_direction`, `settings_extruder_temp`, `settings_bed_temp`, `external_id`, `extra{}`, `tags[]`.
- **Spool**: `filament_id`, `registered`, `first_used`, `last_used`, `price`, `initial_weight`, `spool_weight`, `used_weight`, **`location: String(64)`**, **`lot_nr: String(64)`**, `comment`, `archived`, `extra{}`, `tags[]`.
- **Extra fields** задаются отдельно для vendor, filament и spool. Типы: `text, integer, integer_range, float, float_range, datetime, boolean, choice` (`extra_field_registry.py`). Значения хранятся как JSON-строки, на практике дважды сериализованные (см. [U1 design notes](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/design/spoolman.md)). С v0.25 по ним можно фильтровать и сортировать ([PR #773](https://github.com/Donkie/Spoolman/pull/773)).
- **Tag** (новое в v0.27): физическая NFC/RFID-метка с ключом по UID, нормализованному в uppercase hex. `target_type` = spool | filament, тип `location` зарезервирован. `format` носит только информационный характер: `openprinttag, opentag3d, ntag, bambu, tigertag, qidi, creality, prusa`. Содержимое метки Spoolman **не декодирует**, а `payload_b64` просто пересылает дальше ([release v0.27.0](https://github.com/Donkie/Spoolman/releases/tag/v0.27.0), `spoolman/tags.py`).
- **Settings**: `currency`, `base_url`, `locations` (упорядоченный список), `locations_spoolorders`.

### REST API (`/api/v1`, OpenAPI доступен на `/api/v1/docs`)
- CRUD для `vendor`, `filament`, `spool`, `field/{entity}/{key}`, `setting/{key}`.
- `PUT /spool/{id}/use` принимает `{use_weight | use_length}` (ровно одно из двух). Списывает расход.
- `PUT /spool/{id}/measure` принимает `{weight}` — текущий брутто-вес. Spoolman сам пересчитывает `used_weight`, это готовый вход для взвешивания.
- `GET /location` и `PATCH /location/{name}` (переименование локации меняет её у всех катушек сразу), `GET /material`, `/lot-number`, `/article-number`.
- `POST /tag/scan` — readers (ESPHome/HA/nfc2klipper) шлют UID и получают в ответ spool или filament. Websocket `/tag/scan` транслирует событие всем подписчикам с debounce 3 с ([wiki: Tag scanners](https://github.com/Donkie/Spoolman/wiki/Tag-scanners)).
- `GET /spool/group`, `/search` (свободный текст с похожестью цветов, v0.26), `/export/{spools,filaments,vendors}`, `/externaldb/*` (каталог [SpoolmanDB](https://github.com/Donkie/SpoolmanDB)).
- **Websocket**: на коллекции и на конкретные объекты (`/spool`, `/spool/{id}`, `/filament...`, `/vendor...`, `/setting/{key}`). Типы событий: `added | updated | deleted` (`api/v1/models.py: EventType`).
- **Prometheus**: включается `SPOOLMAN_METRICS_ENABLED=TRUE`. Метрики: `spool_price`, `spool_weight_used`, `spool_initial_weight`, `filament_density`, `filament_diameter`, `filament_weight`, filament info (`spoolman/prometheus/metrics.py`).
- **Авторизации нет by design.** Граница безопасности — сеть плюс CSRF/Origin-проверки (`spoolman/security.py`). Для доступа извне нужен reverse proxy с forward-auth (поддержка 401-reload добавлена в v0.26.1).
- Один uvicorn-процесс. Состояние relay живёт в памяти (`scanrelay.py`).

### Пробелы Spoolman (с доказательствами)
| Пробел | Доказательство |
|---|---|
| Локация — одна плоская строка до 64 символов, отдельной таблицы локаций нет, вложенности нет | `Spool.location = String(64)`; [issue #1202 «hierarchical (nested) locations»](https://github.com/Donkie/Spoolman/issues/1202), open с 2026-10-01; автор сам делает строки вида `DreamMaker - AMS A - Slot 1` |
| Сушки нет | [PR #711 «tracking drying of spools»](https://github.com/Donkie/Spoolman/pull/711): draft, не смержен, обновлялся 2026-01; [#243](https://github.com/Donkie/Spoolman/issues/243) закрыт |
| Закупок/заказов/поставщиков/«склада» нет | В моделях только `price` на filament и spool. [#207 «Inventory Management»](https://github.com/Donkie/Spoolman/issues/207) open с 2023 |
| Принтеров, голов, заданий и истории печати нет | В моделях этих сущностей нет. Историю печати ведёт Moonraker (`auxiliary_data.spool_ids`) |
| Аудит-лога расхода нет: `used_weight` — просто число | `/use` и `/measure` меняют `used_weight` без журнала событий [INFERENCE по коду: таблицы событий нет] |
| Нет auth и multi-tenant | `security.py` |
| Поиск по extra-полям (например, `card_uids`) клиенты делают выборкой `limit=1000` и фильтрацией у себя | U1 spoollink design notes, шаг 4 |

### Интеграции вокруг Spoolman
| Проект | Что делает | Где работает | Лицензия / активность |
|---|---|---|---|
| **Moonraker `[spoolman]`** — [docs](https://moonraker.readthedocs.io/en/latest/configuration/#spoolman), [код](https://github.com/Arksine/moonraker/blob/master/moonraker/components/spoolman.py) | Держит **один** active spool (`/server/spoolman/spool_id`). Следит за `toolhead.position[E]` и каждые `sync_rate` (5 с) шлёт `PUT /spool/{id}/use {use_length}`. При смене экструдера сбрасывает базу E. Пишет `spool_ids` в history `auxiliary_data`. Есть proxy к Spoolman `/server/spoolman/proxy`. Remote method `spoolman_set_active_spool` вызывается из макросов | На принтере (в Moonraker) | GPL-3.0, ★1.47k, push 2026-09-29. Формальные релизы редки (v0.7.1 в 2021), разработка идёт rolling |
| **Мульти-тул в Fluidd** — [docs](https://github.com/fluidd-core/fluidd/blob/develop/docs/docs/features/multi-material.md) | Шаблон: `[gcode_macro T0] variable_spool_id: None` + `SET_ACTIVE_SPOOL ID=...` в toolchange-макросе. Выбранные spool сохраняются через `[save_variables]`. Перед печатью проверяет: spool выбран, филамента хватит, тип совпадает со слайсером | Фронтенд + Klipper-макросы | Fluidd GPL-3.0, ★1.84k, активен. Mainsail (GPL-3.0, ★2.2k) имеет аналогичную панель Spoolman [INFERENCE: Mainsail-доку не открывал] |
| **Disane87/spoolman-homeassistant** — https://github.com/Disane87/spoolman-homeassistant | HA-сенсоры на каждую катушку (вес, локация, lot, цена…). Локации становятся «хабами» для устройств. Сервисы `spoolman.patch_spool` и `spoolman.use_spool_filament`. Klipper-часть объявлена deprecated, рекомендуют Moonraker `[spoolman]` | HA (HACS) | MIT, ★257, стабильный v1.4.0 от 2026-05-10, push 2026-10-13 |
| **gibz104/SpoolmanSync** — https://github.com/gibz104/SpoolmanSync | Веб-UI: назначение spool на AMS/**CFS**-слоты, списание веса по завершении печати, QR/NFC, low-stock-алерты в HA. Работает поверх ha-bambulab и ha_creality_ws | HA add-on или Docker (встроенный HA) | MIT, ★81, тег v1.6.14, push 2026-10-03 |
| **drndos/OpenSpoolMan** — https://github.com/drndos/openspoolman | Bambu: назначение spool на трей через телефон/NFC/QR, AUTO SPEND по оценке слайсера, послойный учёт, история печати, проверка совпадения материала трей↔spool | Сервер (Docker/Helm), MQTT LAN | MIT, ★186, v0.3.0 от 2025-12-23, push 2026-01 |
| **mrkirby153/bambu-spoolman** — https://github.com/mrkirby153/bambu-spoolman | Слушает принтер, парсит G-code (`M620` — смена филамента, `M730` — слой), списывает в Spoolman. Пишет AMS/tray в extra-поля spool | Сервер (Docker) | MIT, ★45, v2.0.0 от 2026-03-06. Несколько AMS и LAN-only печати автор не тестировал (README) |
| Miquelsn/bambulab-spoolman, atownsend247/spoolman-bambu-filament-status, Yarasarda/bambu-spoolman-bridge | Похожие мосты AMS→Spoolman | Сервер | ★60 / 10 / 6, мелкие проекты |
| **Bambuddy** (см. §6) | Собственный склад + синхронизация со Spoolman | Сервер | AGPL-3.0 |
| **mdziekon/octoprint-spoolman** — https://github.com/mdziekon/octoprint-spoolman | OctoPrint → Spoolman | OctoPrint | AGPL-3.0, ★55, 1.4.0 от 2025-10. Для этого парка нерелевантно |
| **lucasbalon/moonraker-spoolman-creality** — https://github.com/lucasbalon/moonraker-spoolman-creality | Патч `spoolman.py` под Moonraker-сборки Creality OS | Rooted Creality | ★1, свежий (2026-09) |
| **Bespok3d/spoolman-klipper-helper** — https://github.com/Bespok3d/spoolman-klipper-helper | U1: active spool следует за прочитанной меткой, расход пишется по каждой печати | Плагин на принтере | AGPL-3.0, ★1, v0.1.36 (2026-08) |

---

## 2. OctoPrint SpoolManager / FilamentManager

- **SpoolManager**: [OllisGit/OctoPrint-SpoolManager](https://github.com/OllisGit/OctoPrint-SpoolManager), ★171, лицензия в API не указана, последний релиз 1.7.0 от 2022-03. Помечен как **abandoned** ([plugins.octoprint.org #1213](https://github.com/OctoPrint/plugins.octoprint.org/issues/1213)). Форки: [dojohnso](https://github.com/dojohnso/OctoPrint-SpoolManager) тоже abandoned ([#1319](https://github.com/OctoPrint/plugins.octoprint.org/issues/1319)), [WildRikku](https://github.com/WildRikku/OctoPrint-SpoolManager) — новый мейнтейнер с 2024-09.
- **FilamentManager**: [malnvenshorn](https://github.com/malnvenshorn/OctoPrint-FilamentManager), AGPL-3.0, последний push 2021. Форк [OllisGit](https://github.com/OllisGit/OctoPrint-FilamentManager) — 2023. Фактически мёртв.
- **Вывод**: OctoPrint в парке не используется, а сами плагины заброшены. Брать не стоит.

---

## 3. Bambu: учёт филамента

### AMS RFID
- Метки MIFARE Classic 1K. Данные подписаны RSA-2048, принтер отвергает метки без валидной подписи. Клонировать можно только целиком (тот же UID и данные), подделать нельзя ([Bambu-Research-Group/RFID-Tag-Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide), ★1.7k, без лицензии, push 2025-09).
- Сторонние катушки в AMS распознаются только по ручной настройке трея (тип и цвет в Studio/Handy). `remain` для них = −1/неизвестно [INFERENCE: из семантики `remain_enabled` ниже].

### greghesp/ha-bambulab
- Repo: https://github.com/greghesp/ha-bambulab. MIT, ★2.37k, v2.2.26 от 2026-09-12.
- Подключение: MQTT в LAN-режиме (IP + access code + serial) или через Bambu Cloud.
- Сенсоры AMS ([docs/entities.mdx](https://github.com/greghesp/ha-bambulab/blob/main/docs/entities.mdx)): Active tray / index, Humidity Index, Humidity и Temperature (только **P1/P2/X1/H2**, то есть у A1 mini с AMS lite их нет), сушка (только AMS 2 Pro/HT), Tray 1–4.
- Атрибуты трея: Color, Empty, Unknown, State, K value (P1/A1), Name, Nozzle min/max, Remaining Filament, Spool serial number, Type. В коде есть `remain`, `remain_enabled`, **`tag_uid`**, **`tray_uuid`** (`definitions.py`).
- `remain_enabled` зависит от флага принтера `ams_calibrate_remaining` (`pybambu/models.py:3454`), то есть от настройки «оценивать остаток» в Studio. Работает только для Bambu-RFID катушек [INFERENCE].
- **BambuStudio filament remaining**: тот же `remain` % из MQTT `ams.tray[]`. Своего учёта в граммах нет.
- **Ограничения доступа**: со свежими прошивками облачный вход сторонних сервисов только для мониторинга. Управление требует LAN developer mode ([SimplyPrint K1 SE connect page](https://simplyprint.io/setup-guide/creality/k1-se/client/connect): «Cloud login is monitor-only on current firmware»; [Printago](https://printago.io/features): «over Bambu Cloud or LAN developer mode»).
- Принтеры чужие (P1S соседа, A1 mini друга), поэтому LAN-доступ требует access code, serial и нахождения в той же сети, а облачный вход — аккаунта владельца. Это нужно согласовать [INFERENCE: организационный вопрос].

---

## 4. Creality: RFID/CFS и стоковый K1 SE

### CFS и RFID
- CFS для K1-серии ставится upgrade kit, который совместим с **K1/K1C/K1 Max/K1 SE**, кроме моделей 2025 года. После установки область печати становится 220×215×250 ([Creality Wiki CFSUK](https://wiki.creality.com/en/k1-flagship-series/CFSUK), [store blog](https://store.creality.com/blogs/tutorial/creality-k1-series-cfs-upgrade-kit)). Протокол CFS по RS485 закрыт ([reddit](https://www.reddit.com/r/crealityk1/comments/1pc80wz/should_i_update_my_firmware_to_25334_cfs_firmware/)).
- Метки Creality: MIFARE Classic 1K, 48 байт зашифрованных данных, ключ сектора выводится из UID, по две метки на катушку. Подписи нет, поэтому метки можно писать самим: [DnG-Crafts/K2-RFID](https://github.com/DnG-Crafts/K2-RFID), [srobinson9305/cfs-programmer](https://github.com/srobinson9305/cfs-programmer), [sybethiesant/CFSWriter](https://github.com/sybethiesant/CFSWriter), [mitt3n/SpoolFID](https://github.com/mitt3n/SpoolFID) (пишет CFS-метки из Spoolman). SimplyPrint тоже пишет CFS-метки ([help](https://help.simplyprint.io/en/article/the-creality-material-standard-nfcrfid-for-the-creality-cfs-1crrofa/)).
- В телеметрии `rfid` — это **id материала, а не серийник метки**: две одинаковые катушки неразличимы (ha_creality_ws README).

### Стоковый K1 SE: WebSocket `ws://<ip>:9999`
Поля по [3dg1luk43/ha_creality_ws](https://github.com/3dg1luk43/ha_creality_ws) (`ws_client.py`, `sensor.py`):
- `printFileName`, `printProgress`/`dProgress`, **`usedMaterialLength`** (мм, общий за текущую печать), `materialStatus` (runout), `err`, `model`/`modelVersion`.
- Ожидаемые слайсером длина и вес приходят из метаданных файла. Вес бывает пуст для файлов из Creality Print. Мультиматериал не разбивается по филаментам ([#122](https://github.com/3dg1luk43/ha_creality_ws/issues/122)).
- `cfsConnect` и `boxsInfo.materialBoxs[]` есть только с CFS. По каждому слоту: `vendor, type, name, color` (7-hex с паддингом), `rfid`, percent, `state/selected`, `box_id/slot_id`, `min/max_temp`, `pressure`. По боксу: temp/humidity. Сервис `set_cfs_material` пишет материал в слот.
- В README K1 SE отмечен так: «No box temperature, no light, optional MJPEG camera».
- Историю заданий в стоковом WS не искал [INFERENCE: вероятно отсутствует или неудобна, поэтому историю строить по переходам состояния].

**ha_creality_ws**: AGPL-3.0, ★96, v0.9.9 от 2026-10-08, очень активен. Работает в HA и держит одно WS-соединение на принтер.

**Root-путь**: [Guilouz Creality-Helper-Script](https://github.com/Guilouz/Creality-Helper-Script-Wiki) и форк [jantzeno/Creality-Helper-Script-K1SE](https://github.com/jantzeno/Creality-Helper-Script-K1SE) ставят Moonraker + Fluidd/Mainsail, после чего доступен Moonraker `[spoolman]`. SimplyPrint для K1 SE прямо требует root + Helper Script ([setup guide](https://simplyprint.io/setup-guide/creality/k1-se)).

---

## 5. Snapmaker U1

### Сток
- [Snapmaker/u1-klipper](https://github.com/Snapmaker/u1-klipper): GPL-3.0, ★78, push 2026-08. Модули `klippy/extras/filament_detect.py` и `fm175xx_reader.py` (4 канала RFID), `filament_protocol.py`, `filament_feed.py`, `filament_entangle_detect.py`.
- `filament_detect.get_status()` → `{info:[per-channel dict], state:[...], config}`. G-code: `FILAMENT_DT_QUERY/UPDATE/CLEAR/SELF_TEST`.
- Стоковый ридер понимает только MIFARE Classic 1K в проприетарном формате Snapmaker ([U1-ext rfid.md](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/design/rfid.md)).
- Moonraker API на стоке открыт: на это опираются сторонние проекты (Filarack подключается по «Moonraker address», [CASAI77/spoolscan](https://github.com/CASAI77/spoolscan) назначает слоты через Moonraker). Читать `printer.objects.query?filament_detect&print_stats` и `server.history` можно [INFERENCE: на моём U1 не проверял]. Правка `moonraker.conf` и SSH на стоке недоступны [INFERENCE: extended FW добавляет SSH и выбор Fluidd/Mainsail как фичи].

### Extended firmware (paxx12)
- https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware: GPL-3.0, ★1.05k, стабильный v1.6.0-paxx12-22 от 2026-09-07, rolling v2.0.0-develop от 2026-09-09, push 2026-10-08.
- Возможности: SSH, Fluidd/Mainsail, Klipper/Moonraker includes, хуки PRINT_START/END, Prometheus/HA-мониторинг, **OpenRFID** (читает Bambu, Creality, Anycubic, Elegoo, Qidi, OpenSpool, OpenTag3D, TigerTag; для Bambu и Creality нужны ключи), **AFC-Lite** (4 «лейна» в Fluidd/Mainsail), **Spoolman Integration** ([docs/spoolman.md](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/spoolman.md)).
- **`filament_detect` в extended** ([design/filament_detect.md](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/design/filament_detect.md)):
  - Канал `info[0..3]`: `VENDOR, MAIN_TYPE, SUB_TYPE, RGB_1..5, ALPHA, HOTEND_MIN/MAX_TEMP, BED_TEMP, CARD_UID[], CARD_TYPE (NTAG|M1), SKU`.
  - Только чтение: `OFFICIAL, CARD_EVENT_TIME, DIAMETER, WEIGHT, LENGTH, DRYING_TEMP, DRYING_TIME, MF_DATE…`.
  - **`POST /printer/filament_detect/set {channel, info}`** — внешние ридеры и приложения (и наш сервис) могут записать данные в канал. Это точка входа для голосовой коррекции.
- **SpoolLink**: Moonraker-компонент `[spoollink]`. Резолвит spool по ID или по UID метки через extra-поле `card_uids` у spool, пишет `variant` у filament. Отправляет метаданные в Klipper и отслеживает active spool в Moonraker, чтобы Spoolman списывал расход. G-code `SET_SPOOL_ID LANE=E0 SPOOL_ID=5` (или `CHANNEL=0..3`). `AFC_lane.get_status()` отдаёт `spool_id` во Fluidd/Mainsail. Включается в UI `firmware-config` (Settings → Snapmaker Components → Spoolman Integration). Spoolman должен быть доступен с принтера по HTTP.
- **Ответ на вопрос «можно ли Moonraker spoolman на U1»**: да, штатно на extended FW ≥ v1.6.0 (+ per-lane spool через SpoolLink/AFC-Lite). На стоке нет [INFERENCE: нет способа править `moonraker.conf`]. Без прошивки остаётся серверный адаптер по Moonraker API.
- Конфликт: старые самописные `spoolman_multi_tool` include ломают SpoolLink ([#567](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/pull/567)).
- Экосистема вокруг U1:
  - Приложения: [spool-link-apps](https://github.com/paxx12-snapmaker-u1/spool-link-apps) (GPL-3.0), SpoolPainter, SpoolKid, SpoolTagger, 3DMRP.
  - Внешние ридеры для драйбокса: [wasikuss](https://github.com/wasikuss/snapmaker-u1-remote-rfid-reader) (MIT), [baze](https://gitlab.com/baze/snapmaker-u1-drybox-nfc-reader) (CC BY-NC).
  - Прочее: [ubaccu/u1-filament-automation](https://github.com/ubaccu/u1-filament-automation) (Spoolman → Orca → PA), [Piets/u1-spoolman-bridge](https://github.com/Piets/u1-spoolman-bridge).

---

## 6. Стандарты NFC-меток

| Стандарт | Носитель / формат | Открытость | Запись на принтер | Связь со Spoolman |
|---|---|---|---|---|
| **OpenPrintTag** (Prusa) — [spec repo](https://github.com/OpenPrintTag/openprinttag-specification), https://specs.openprinttag.org | **NFC-V (ISO 15693)**, ICODE SLIX2. NDEF + CBOR: main region (UUID-ы, GTIN, материал, цвета, вес нетто, тара, температуры, `drying_temperature`, `drying_time`) + **auxiliary region, перезаписываемый**: `consumed_weight`, `storage_location`, `purchase_time/price/currency`, `last_stir_time`, `workgroup` | MIT, ★286, push 2026-10-09 | Prusa | Spoolman v0.27 хранит UID (`format=openprinttag`), payload не парсит. **Важно**: PN532 не читает NFC-V, телефоны читают [INFERENCE для PN532 по ISO 15693, косвенно подтверждает Spoolman `models.py` про копирование Prusa NFC-V на NTAG215 для PN532 (#776)] |
| **OpenSpool** — [spuder/OpenSpool](https://github.com/spuder/OpenSpool) | NTAG215/216, NDEF `application/json` `{protocol, version, type, color_hex, brand, min_temp, max_temp}` (+ `spool_id` в U1-расширении) | NOASSERTION, ★774, 1.21.2 (2025-08) | U1 extended (встроенный ридер), ESP32-ридеры для Bambu | SpoolLink и OpenSpoolMan связывают с Spoolman |
| **TigerTag** — [guide](https://github.com/TigerTag-Project/TigerTag-RFID-Guide) | NTAG213, бинарный формат + публичный реестр/API. TigerTag+ — подписанный вариант с облачными патчами от производителя | CC-BY-4.0 (спека), ★27, очень активен | Через OpenRFID на U1 | `format=tigertag` (только UID) |
| **OpenTag3D** — https://opentag3d.info | NTAG213/215/216 | открытый | OpenRFID | `format=opentag3d` |
| **Bambu** | MIFARE Classic 1K, подпись RSA-2048 | закрыт, только чтение | Только AMS, нужны оригинальные метки | Только UID. Мосты используют `tray_uuid`/`tag_uid` из MQTT |
| **Creality CFS** | MIFARE Classic 1K, 48 байт AES-зашифрованы, ключ из UID | реверснут, писать можно | CFS (K1-kit/K2/Hi) | SpoolFID, SimplyPrint пишут метки. В телеметрии нет серийника |

**Вывод по интеропу**: Spoolman с v0.27 — нейтральный реестр «UID → spool» для любых меток. Для собственных меток практичнее всего NTAG215 + OpenSpool JSON: их читают U1 extended, телефоны и ESP32. OpenPrintTag идеологически лучший (в aux есть consumed_weight и storage_location), но NFC-V хуже поддерживается дешёвыми ридерами.

---

## 7. Управление парком (fleet): SaaS и серверы

| Продукт | Что даёт | Где работает | Klipper / U1 | K1 SE сток | Bambu | Филамент / склад | Лицензия / активность |
|---|---|---|---|---|---|---|---|
| **Moonraker `history` + `job_queue`** ([history API](https://github.com/Arksine/moonraker/blob/master/docs/external_api/history.md), [job_queue](https://github.com/Arksine/moonraker/blob/master/docs/external_api/job_queue.md)) | История по принтеру: `filament_used` (мм), `metadata` (включая `filament_weights[]` по инструментам, `referenced_tools`, `filament_type/name/colors`), `auxiliary_data` (`spoolman.spool_ids`), totals. Локальная очередь | На принтере | ✅ | ❌ (нужен root) | ❌ | только через `[spoolman]` | GPL-3.0 |
| **Klipper `print_stats.filament_used`** | Суммарные мм за печать, **без разбивки по экструдерам** ([printer_objects](https://github.com/Arksine/moonraker/blob/master/docs/printer_objects.md)) | На принтере | ✅ | ❌ | – | – | GPL-3.0 |
| **Bambuddy** — https://github.com/maziggy/bambuddy | Self-hosted «замена Bambu Cloud»: очередь (batch orders, pipelines, slicing sidecar), print log, **встроенный склад катушек** (AMS-назначения, учёт по 3MF-оценке с fallback на дельту AMS remain%, цена/кг, low-stock, поставщики и артикулы, **storage locations: полки/ящики/драйбоксы с синхронизацией в Spoolman, HA-сенсор влажности на локацию**), сушка AMS 2 Pro/HT, Prometheus, MQTT→HA | Сервер (Docker) | ❌ (в README 0 упоминаний Klipper/Moonraker/Snapmaker/Creality) | ❌ | ✅ LAN | ✅ богато | AGPL-3.0, ★3.09k, daily-релизы (v1.2.6b1 от 2026-10-09) |
| **FDM Monster** — https://github.com/fdm-monster/fdm-monster | Сервер фермы: OctoPrint, **Moonraker**, PrusaLink, **Bambu LAN**; история заданий, очередь. Интеграция со Spoolman в roadmap не отмечена (unchecked) | Сервер | ✅ | ❌ | ✅ LAN | ❌ | AGPL-3.0, ★193, 2.1.1 (2026-05), push 2026-10 |
| **Obico** — [server](https://github.com/TheSpaghettiDetective/obico-server), [moonraker-obico](https://github.com/TheSpaghettiDetective/moonraker-obico) | AI-детекция провалов, удалённый доступ; **self-hostable** | Агент на принтере или хосте + сервер (Docker, GPU/CPU) | ✅ | ❌ (нужен root + Moonraker) | ⚠️ сами заявляют поддержку Bambu ([blog](https://www.obico.io/blog/best-3d-printer-failure-detection/)), SimplyPrint утверждает, что только через OctoPrint-мост ([compare](https://simplyprint.io/alternatives/obico)) [не проверено] | ❌ | AGPL-3.0, ★1.9k / 188, moonraker-obico v2.2.3 (2026-09) |
| **OctoEverywhere** — [plugin](https://github.com/QuinnDamerell/OctoPrint-OctoEverywhere) | Удалённый доступ, AI-детекция, уведомления. Companion для Klipper ставится на любой Linux; есть Creality OS и Bambu Connect | Агент + облако (SaaS) | ✅ | ⚠️ на самом K1 нужен root [INFERENCE], companion работает через Moonraker | ✅ | ❌ | плагин AGPL-3.0, сервис проприетарный; 5.4.0 (2026-09) |
| **SimplyPrint** — https://simplyprint.io | Облако: очередь, браузерный слайсер, **Filament Manager** (автосписание, NFC: чтение и запись Bambu, CFS, OpenPrintTag, OpenSpool, OpenTag; автосоздание spool из AMS RFID), мультиматериал AMS/ACE/CFS/MMU | SaaS + клиент на принтере (`[simplyprint]` в Moonraker) | ✅ | ❌ (официально нужен root, [guide](https://simplyprint.io/setup-guide/creality/k1-se)) | ✅ (облако только для мониторинга) | ✅, продвинутое — платные тарифы ([multi-material](https://simplyprint.io/features/multi-material)) | проприетарный |
| **Printago** — https://printago.io | Ферма-SaaS + **Self-Hosted edition** (Docker Compose, офлайн-лицензия): material-aware routing, AMS/CFS/ACE/Canvas и **Snapmaker quad-nozzle** с учётом по слотам, filament stock (beta, поставки с ценой, аудит коррекций), spool tracking (beta), REST 300+ endpoints, MQTT/WS, webhooks, custom fields | Облако или self-hosted + Fuse LAN bridge | ✅ (через Fuse) | ✅ заявлен «Creality … CFS» [INFERENCE: подключение стокового K1 SE не проверено] | ✅ cloud/LAN dev mode | ✅ | проприетарный ([features](https://printago.io/features)) |
| **3DPrinterOS** — https://www.3dprinteros.com | Облачная ферма для образования и бизнеса: очередь, approvals, учёт материала ([help](https://intercom.help/3DPrinterOS/en/articles/9712538-track-filament-in-3dprinteros)) | SaaS + клиент | ✅ | ? | ✅ | базовый | проприетарный |
| **Filarack** — https://filarack.com | SaaS: визуальная карта склада (полки/боксы/слоты, drag&drop), **менеджер сушки (moisture score, партии сушки, HA-webhook драйбоксов, EMC)**, синк печатей из Moonraker history и Bambu Cloud, **U1 по головам T0–T3**, Slicer-Link для остальных принтеров, использует Spoolman API | SaaS + Browser Bridge | ✅ U1 явно | Slicer-Link (по оценке) | ✅ облако | ✅ | проприетарный, бесплатный [INFERENCE: исходники не найдены] |

Мелкие проекты для смешанного парка: [Anto5314/filament-tracker](https://github.com/Anto5314/filament-tracker) (Creality OS + Spoolman, ★0), [MKloberg/3dmrp](https://github.com/MKloberg/3dmrp) (Windows-only ферма с U1).

**Готового OSS, который одновременно закрывает Klipper U1 (по головам), стоковый K1 SE через WS и Bambu, по сути нет.** Ближе всего FDM Monster (Moonraker + Bambu, но без филамента и без Creality WS) и проприетарные Printago и Filarack.

---

## 8. Таблица «что где ставить»

| Принтер | Что возможно на принтере | Серверный адаптер (наш сервис) | Источник сигналов для self-correction |
|---|---|---|---|
| **U1 (192.168.0.26)** | **Extended FW**: Moonraker `[spoolman]` + SpoolLink (`SET_SPOOL_ID LANE=E0..E3`), OpenRFID, AFC-Lite. Moonraker сам списывает расход по active spool, на toolchange spool переключается. **Сток**: только чтение Moonraker API [INFERENCE] | Moonraker WS: `print_stats`, `filament_detect.info[0..3]` (тип, RGB, CARD_UID), `toolhead.extruder`. `server.history` (`filament_weights[]` по инструментам, `spool_ids`). Запись: `filament_detect/set`, G-code `SET_SPOOL_ID` через `printer/gcode/script` | RFID UID и цвет по каналу; per-tool граммы из метаданных G-code; live mm от Moonraker spoolman |
| **K1 SE сток** | Ничего, без root нет Moonraker. С root: Helper Script → Moonraker → `[spoolman]` (возможно с патчем [lucasbalon](https://github.com/lucasbalon/moonraker-spoolman-creality)) | Клиент `ws://ip:9999` (поля как в ha_creality_ws) или HA-сущности ha_creality_ws. Одна «голова» = location `PRN:K1SE/T0`. Расход: `usedMaterialLength` в конце печати → `PUT /use {use_length}`. Внимание: ha_creality_ws тоже держит WS-соединение | `usedMaterialLength`, ожидаемая длина и вес из слайсера, `materialStatus` (runout). Без CFS RFID нет, только голос и взвешивание |
| **Bambu P1S (сосед), A1 mini + AMS lite (друг)** | Ничего (закрытая прошивка) | ha-bambulab (HA) или прямой MQTT LAN (`device/<serial>/report`), либо Bambuddy, OpenSpoolMan, bambu-spoolman как готовые мосты. Location `PRN:P1S/AMS1/S2`, `PRN:A1M/AMSL/S1..4`, `.../EXT` | `tray_type`, `tray_color`, `tag_uid`, `tray_uuid`, `remain%` (только Bambu RFID), active tray. Граммы по 3MF/G-code (`M620`). Humidity у AMS lite нет |
| **Хакспейс** | Неизвестно | По ситуации: Moonraker, если Klipper; иначе ручной или голосовой ввод, Slicer-side оценка | Взвешивание до и после |
| **HA** | – | ha-bambulab, ha_creality_ws, spoolman-homeassistant (сенсоры и `use_spool_filament`), Moonraker HA-интеграция; SpoolmanSync как готовый вариант для Bambu и Creality | Драйбоксы и гигрометры, весы (ESPHome), NFC-ридеры (ESPHome → `POST /tag/scan`) |

---

## 9. Рекомендация

### 9.1 Spoolman как бэкенд spool/filament/vendor — **да**
**Аргументы за:**
- MIT, активен (4 релиза за 3 месяца), REST + WS + Prometheus.
- Нативно поддержан Moonraker, Fluidd, Mainsail и U1 extended FW (SpoolLink), а также HA (Disane87) и Bambu-мостами.
- Есть `/measure` для весов и с v0.27 реестр NFC UID.

Собственное хранилище катушек лишило бы нас всей этой экосистемы (U1 SpoolLink ищет spool именно в Spoolman).

**Свой сервис (PrintOps)** держит только то, чего нет в Spoolman: принтеры и головы, задания, журнал расхода, сушки, закупки, адреса склада, голосовые события. Спулы он читает и пишет через API Spoolman и слушает его WS.

### 9.2 Адресация
- `Spool.location` — **каноническая строка-путь ≤ 64 символа** (лимит колонки): склад `WH:A/S2/B3/04` (зона/полка/коробка/слот), принтер `PRN:U1/T3`, `PRN:P1S/AMS1/S2`, `PRN:A1M/AMSL/S4`, `PRN:K1SE/T0`, сушилка `DRY:SUNLU1`.
  - Так Dashboard и группировка Spoolman, а также HA-хабы Disane87 работают без доработок.
  - `PATCH /location/{old}` переименовывает адрес у всех катушек сразу.
- **Не дублировать** путь в extra-полях (shelf/box/slot). Иначе неизбежна рассинхронизация, к тому же extra-поля дважды сериализованы и фильтруются на клиенте. Разбор пути делает PrintOps. Структурированная таблица `locations` с иерархией, ёмкостью и QR живёт в PrintOps, а в Spoolman уходит только листовой путь. Если когда-нибудь закроют [#1202](https://github.com/Donkie/Spoolman/issues/1202), можно будет мигрировать.
- Конвенцию SpoolLink для U1 сохранить: extra `card_uids` у spool и `variant` у filament. Сейчас в Spoolman есть ещё и нативная таблица `tag`, так что UID лучше писать в оба места, пока SpoolLink не перейдёт на `tag` [INFERENCE: переход не анонсирован].

### 9.3 Журнал сушки — **своя таблица**
- `drying_sessions(id, dryer, started_at, ended_at, temp_c, target_h, humidity_before/after, source)` плюс связь `drying_session_spools`.
- В Spoolman зеркалить только `extra.last_dried_at` (datetime) и, возможно, `extra.dry_state` (choice). Этого хватит для сортировки и фильтра в UI и в HA.
- Почему не extra-поля целиком: истории в них нет, а PR #711 висит в draft.
- Рекомендованные `drying_temperature` и `drying_time` брать из OpenPrintTag или U1 `DRYING_TEMP/TIME`, если метка их содержит.

### 9.4 Закупки
- Своя модель: `suppliers`, `purchase_orders`, `po_lines`, `received_spools → spool_id`.
- При приёмке создаём spool в Spoolman с `price`, `lot_nr`, `initial_weight`, `location=WH:...`.
- Low-stock считаем как агрегат по filament в PrintOps (или берём из SpoolmanSync/HA).

### 9.5 Очередь и история
- **История**: своя fleet-БД `jobs(printer, source_job_id, file, started/ended, status, filament_mm_total, slicer_estimates)` + `job_tool_usage(job, tool, spool_id, grams_est, grams_reported, source)`. Источники:
  - Moonraker `server.history` + WS `notify_history_changed` (U1), где есть `filament_weights[]` по инструментам и `spool_ids`;
  - Bambu MQTT `print` (или ha-bambulab);
  - Creality WS — переходы состояния + `usedMaterialLength`.
- **Очередь**: тонкий слой поверх нативных механизмов — Moonraker `job_queue` для U1 (и K1 после root), Bambu — отправка через LAN FTP/MQTT [INFERENCE: требует developer mode на чужом принтере]. Для мультивендорной очереди готовых OSS нет; FDM Monster можно рассмотреть как donor/референс (AGPL!).

### 9.6 Self-correction: ledger + reconciliation
Каждое изменение остатка записывается **событием** в своём ledger: `(spool_id, delta_g, source, confidence, job_id?)`. Источники:
1. `gcode_estimate` — метаданные слайсера по инструменту (Moonraker `filament_weights`, Bambu 3MF). Средняя уверенность.
2. `moonraker_live` — Moonraker `[spoolman]` `/use`. Высокая уверенность по мм; в граммы переводится через density/diameter.
3. `printer_reported` — Creality `usedMaterialLength`, Bambu `remain%` (грубо, только RFID-катушки).
4. `weigh` — весы → `PUT /measure`. **Абсолютная привязка**: перекрывает накопленный дрейф.
5. `rfid_slot` — UID и цвет из `filament_detect` / AMS `tag_uid` / CFS `rfid+color`. Это **идентичность** катушки в слоте, а не расход: при расхождении с ожидаемым spool поднимается конфликт.
6. `voice` — «I loaded white PLA into U1 head 4»:
   - резолвим filament (material=PLA, цвет ≈ #FFFFFF через Spoolman search с color similarity, v0.26) и кандидатов spool (не в принтере, ближе по складу, наименьший остаток);
   - ставим `location=PRN:U1/T3`;
   - на U1 с extended FW вызываем `SET_SPOOL_ID LANE=E3 SPOOL_ID=n`; на стоке (пока действует D4) в принтер ничего не пишем — `filament_detect/set` есть только в extended FW, а `SET_PRINT_FILAMENT_CONFIG` — отдельный тикет с подтверждением;
   - через несколько секунд сверяем с `filament_detect.info[3]` (`MAIN_TYPE`, `RGB_1`, `CARD_UID`).

**Правило одного писателя**: если на U1 включён Moonraker `[spoolman]`, PrintOps **не делает** `/use` для заданий U1 и только сверяет по оценке G-code. Иначе расход спишется дважды. Для K1 SE и Bambu писатель — PrintOps (или выбранный мост, но не оба одновременно).

### 9.7 Адаптеры по семействам принтеров
| Адаптер | Транспорт | Чтение | Запись |
|---|---|---|---|
| `klipper-moonraker` (U1, K1 после root, хакспейс) | WS JSON-RPC 7125 | print_stats, toolhead.extruder, filament_detect (U1), history, file metadata | `printer.gcode.script` (SET_SPOOL_ID / SET_ACTIVE_SPOOL), `filament_detect/set`, job_queue |
| `creality-ws` (K1 SE сток) | `ws://:9999` | состояние, `usedMaterialLength`, `boxsInfo` (если CFS) | только CFS-материал; иначе ничего |
| `bambu-mqtt` (P1S, A1 mini) | MQTT TLS 8883 LAN (или HA ha-bambulab как источник) | `print.ams.ams[].tray[]`, `vt_tray`, gcode_state, `mc_percent`, имя файла | (опционально) `ams_filament_setting` [INFERENCE] |
| `ha-bridge` | HA WS API | сущности ha-bambulab / ha_creality_ws / весы / гигрометры | `spoolman.*` сервисы, уведомления |
| `voice-intent` | REST (наш) | – | ledger + Spoolman PATCH + адаптер принтера |

Каждый адаптер нормализует данные в `PrinterSlotState{printer, tool/slot, material, color, tag_uid, remain_pct?}` и `JobEvent`. Reconciler сравнивает их с location в Spoolman.

### 9.8 Безопасность
Spoolman без auth. REST для колонки и прочий внешний доступ публиковать только через PrintOps (с токеном) или reverse proxy с forward-auth. Сам Spoolman держать в LAN, но доступным с U1, так как SpoolLink ходит в него напрямую по HTTP.
