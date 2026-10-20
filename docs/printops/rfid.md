# PrintOps: RFID/NFC катушек, собственная маркировка, ESPHome‑станция

Дата исследования: 2026‑10‑14. Парк: Snapmaker U1 (192.168.0.26, Klipper/Moonraker, 4 головы), Creality K1 SE (сток, 192.168.0.27), Bambu P1S + AMS (сосед), Bambu A1 mini + AMS lite (друг). Целевая система: монорепо 3d-cli, REST‑сервис 3d-api, Home Assistant на Odroid (Белград).

Пометка **[INFERENCE]** означает вывод или оценку, которую я не проверял напрямую. Всё остальное подтверждено ссылкой, локальным файлом или живым ответом принтера.

---

## 0. Источники по U1: локальный дамп прошивки и один read‑only запрос

### 0.1 Что лежит в `/tmp/u1resume`
- В `/tmp/u1resume/fw/` 9 модулей klippy extras: `print_task_config.py`, `pause_resume.py`, `virtual_sdcard.py`, `machine_state_manager.py`, `timelapse.py`, `defect_detection.py` и т.д. Самих `filament_detect.py`, `filament_protocol.py` и `fm175xx_reader.py` **в дампе нет**. Их исходники я взял из открытого репозитория Snapmaker [Snapmaker/u1-klipper](https://github.com/Snapmaker/u1-klipper) (`klippy/extras/filament_detect.py`, `filament_protocol.py`, `fm175xx_reader.py`, ветка main). Snapmaker опубликовал их 30.03.2026: [блог Snapmaker](https://www.snapmaker.com/blog/snapmaker-u1-firmware-now-on-github/).
- `/tmp/u1resume/u1_cfgfile_text.json`, строки 41–61, содержит конфиг NFC:
  ```ini
  [fm175xx_reader]
  soc_spi_bus: 2
  soc_spi_dev_num: 0
  extra_spi_bus: 2
  extra_spi_dev_num: 1
  soc_spi_speed_max: 500000   # то же для extra
  # Equivalently: etruder ~ extruder3
  soc_ch_1: 3
  soc_ch_2: 2
  extra_ch_1: 0
  extra_ch_2: 1
  [filament_detect]
  ```
  Маркеров расширенной прошивки (`extended`, `openrfid`, `paxx`) в конфиге нет.

### 0.2 Живой запрос (единственный, read‑only)
`GET http://192.168.0.26:7125/printer/objects/list`. Среди объектов есть `filament_detect`, `filament_parameters`, `print_task_config`, `filament_feed left`, `filament_feed right`, `filament_motion_sensor e0_filament … e3_filament`, `mqtt`. Объекта `fm175xx_reader` в списке нет: у него нет `get_status`.

`GET http://192.168.0.26:7125/printer/objects/query?filament_detect`:
```json
{"filament_detect": {
  "info": [ {"VERSION":0,"VENDOR":"NONE","MANUFACTURER":"NONE","MAIN_TYPE":"NONE","SUB_TYPE":"NONE",
             "TRAY":0,"ALPHA":255,"MULTI_MODE":0,"COLOR_NUMS":1,"ARGB_COLOR":4294967295,
             "RGB_1":16777215,"RGB_2":16777215,"RGB_3":16777215,"RGB_4":16777215,"RGB_5":16777215,
             "DIAMETER":0,"WEIGHT":0,"LENGTH":0,"DRYING_TEMP":0,"DRYING_TIME":0,
             "HOTEND_MAX_TEMP":0,"HOTEND_MIN_TEMP":0,"BED_TYPE":0,"BED_TEMP":0,
             "FIRST_LAYER_TEMP":0,"OTHER_LAYER_TEMP":0,"SKU":0,"MF_DATE":"19700101",
             "RSA_KEY_VERSION":0,"OFFICIAL":false,"CARD_UID":0}, ... ×4 ],
  "state": [0,0,0,0],
  "config": {"startup_stay": false}}}
```
Что из этого следует:
- Все 4 канала пустые: значения по умолчанию из `FILAMENT_INFO_STRUCT`, `state=0` (IDLE). Ни одна катушка с распознанной меткой сейчас не загружена.
- В ответе нет полей `CARD_TYPE` и `CARD_EVENT_TIME`, которые добавляет расширенная прошивка paxx12 ([filament_detect.md](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/design/filament_detect.md)). Вместе с отсутствием маркеров в конфиге это значит, что **на U1 стоит стоковая прошивка** [INFERENCE: сравнивал схемы полей, версию прошивки не запрашивал].

### 0.3 Как U1 читает метки (исходники u1-klipper + дамп)
| Вопрос | Ответ | Где подтверждено |
|---|---|---|
| Чип считывателя | **FM17580** (Fudan Micro, `FM175XX_CHIP_TYPE_FM17580`). Работает только по ISO 14443A/B, ISO 15693 не поддерживает | `fm175xx_reader.py` стр. 107–111; [LCSC/JLC: FM17580 = ISO14443A, ISO14443B](https://jlcpcb.com/partdetail/FUDANMICRO-FM17580_QNA_TG/C2895152) |
| Сколько считывателей и как они разделены по головам | Два чипа FM17580 на SPI2: dev0 называется «soc», dev1 «extra». Каждый чип GPIO‑переключателем RF (`rf_1_pin`, `rf_2_pin`, gpiochip1 линии 27/24) переключается между 2 антеннами. Итого 4 канала, один на каждый экструдер e0..e3. В конфиге: extra→ch0, ch1; soc→ch3, ch2. Антенны стоят у боковых держателей катушек (filament_feed left/right) | `fm175xx_reader.py` стр. 195–300; cfg выше; [paxx12 rfid.md: «put the tag on the side next to the U1 housing»](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/design/rfid.md) |
| Когда читает | По событию: вставили филамент в порт (`filament_feed:port`), runout/present, на старте, если `startup_stay=false` и филамент уже вставлен. Ещё вручную через `FILAMENT_DT_UPDATE CHANNEL=n`. Постоянного опроса нет | `filament_detect.py` `_feed_port_evt_handle`, `_runout_evt_handle`, `_ready` |
| Какие метки читает сток | Только **MIFARE Classic 1K**: проверка `SAK == 0x08`. NTAG (SAK 0x00) и NFC‑V сток игнорирует | `fm175xx_reader.py` стр. 949 |
| Ключи секторов | Выводятся из UID через HKDF‑SHA256: 16 ключей A и 16 ключей B, salt `Snapmaker_qwertyuiop[,.;]` / `…_1q2w3e`, info `key_a_<n>`. Соли лежат в открытом коде, так что любой считыватель с MIFARE‑auth (PN532 и т.п.) может прочитать официальные метки Snapmaker | `fm175xx_reader.py` стр. 138–139, 935–942, 991–1016 |
| Подпись | **RSA‑2048 PKCS#1 v1.5 + SHA‑256** по байтам 0..639 (секторы 0–9). Подпись лежит в секторах 10–15. `RSA_KEY_VERSION` (2 байта, sector 2) выбирает один из 10 вшитых публичных ключей (0..9). Если подпись не сходится (`FILAMENT_PROTO_SIGN_CHECK_ERR`), канал получает пустую структуру | `filament_protocol.py` стр. 185–356 |
| `OFFICIAL` | `True` ставится **только** после успешной проверки подписи. Тогда же заполняются `CARD_UID`, `SKU`, `VENDOR`, `MAIN_TYPE` и другие поля | `filament_protocol.py` стр. 466–468 |
| Последствия для UI | При `OFFICIAL=True` поля слота нельзя редактировать: `filament_edit=False`, а `SET_PRINT_FILAMENT_CONFIG` выдаёт ошибку «official filament, not configurable!», если не передан `FORCE=1`. Неофициальная инфа не перетирает уже заданного вендора | дамп `/tmp/u1resume/fw/print_task_config.py` стр. 320–327, 491–499, 607–608 |
| Ручная установка филамента (сток) | G‑code `SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER=<0..3> VENDOR= FILAMENT_TYPE= FILAMENT_SUBTYPE= FILAMENT_COLOR_RGBA= ALPHA= COLOR_NUMS= COLORS= [FORCE=1]`. Через этот вход 3d-api может записать в слот наши данные без RFID | дамп `print_task_config.py` стр. 122–123, 585–597 |

**Вывод по U1:**
- Сток читает **только метки Snapmaker, подписанные их ключом RSA**. Свои метки сделать нельзя: нет приватного ключа. Для чужой или неподписанной метки сток **не отдаёт даже UID** (`CARD_UID=0`), привязать катушку по UID не получится.
- Расширенная прошивка paxx12 ([сайт](https://snapmakeru1-extended-firmware.pages.dev/), [rfid.md](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/design/rfid.md)) добавляет:
  - NTAG213/215/216 с **OpenSpool JSON** (NDEF `application/json`);
  - движок OpenRFID: Bambu и Creality с пользовательскими ключами, Anycubic, Elegoo, Qidi, OpenTag3D, TigerTag, SpoolEase;
  - режим «External» для внешних ESP32+PN532 ридеров;
  - endpoint `POST /printer/filament_detect/set`;
  - интеграцию со Spoolman по ID или UID карты.
- ISO 15693 (OpenPrintTag) на U1 **невозможен аппаратно**: так прямо пишет paxx12, и это совпадает с характеристиками FM17580.
- Парсер NDEF в расширенной прошивке перебирает **все** записи и берёт MIME `application/json` (TNF=2). URI‑запись перед ним не мешает: [filament_protocol_ndef.py](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/overlays/firmware-extended/13-patch-rfid/root/home/lava/klipper/klippy/extras/filament_protocol_ndef.py), стр. 84–123, 248–263.

---

## 1. Матрица совместимости меток

### 1.1 Форматы
| Формат | Чип / физика | Данные | Защита | 3‑я сторона читает? | 3‑я сторона пишет свои? | Лицензия / спека |
|---|---|---|---|---|---|---|
| **Bambu Lab** | MIFARE Classic 1K (ISO14443A), на катушке 2 метки [INFERENCE] | Проприетарные блоки | Ключи 16 секторов выводятся из UID через KDF (публично известен с 11.2024); **RSA‑2048 подпись** по всему содержимому | **Да**: Proxmark3 / Flipper / ESP32+PN532 с выведенными ключами | **Нет**. AMS отвергает метку без валидной подписи. Можно только **клонировать** настоящую метку 1:1 на magic Gen2 / FUID, Gen1 AMS отвергает | [Bambu-Research-Group/RFID-Tag-Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide) (FAQ «Can I create custom tags? No, tags are digitally signed») |
| **Creality CFS** (K2, K1‑series+CFS, Hi) | MIFARE Classic 1K, **2 метки** на катушке (по одной на щёчку) | Sector 1, блоки 4–6: 48 байт ASCII (дата, vendor `0276`, batch, material ID, цвет, длина, serial), зашифрованы **AES‑128‑ECB** статическим ключом. Ключ сектора 1 = первые 6 байт AES(UID×4, u_key) | Только статические ключи AES, которые уже утекли. **Подписи нет** | **Да** | **Да**: CFS принимает самодельные метки (Android‑приложение «RFID for CFS», Flipper‑app) | [DnG-Crafts/K2-RFID](https://github.com/DnG-Crafts/K2-RFID); [mitchsurp/K2-RFID-Flipper](https://github.com/mitchsurp/K2-RFID-Flipper) (раскладка и ключи); [CrealityRfid.md](https://github.com/Bambu-Research-Group/RFID-Tag-Guide/blob/main/CrealityRfid.md) |
| **Snapmaker U1** | MIFARE Classic 1K | Раскладка M1 из `filament_protocol.py` | Ключи HKDF из UID (соли в открытом коде) + **RSA‑2048** | **Да** (код открыт) | **Нет** для стока (нужен приватный ключ). На расширенной прошивке вместо этого используют NTAG/OpenSpool | см. §0.3 |
| **OpenSpool** | NTAG215/216 (ISO14443A, NFC Forum Type 2). NTAG213 (144 Б) официально **не подходит** | Одна NDEF‑запись MIME `application/json`: `{"protocol":"openspool","version":"1.0","type","color_hex","brand","min_temp","max_temp"}`. У paxx12 есть расширение U1: `subtype`, `alpha`, `additional_color_hexes`, `bed_min/max_temp`, `weight`, `diameter` | Нет | Да (любой телефон) | Да | [openspool.io/rfid.html](https://openspool.io/rfid.html), [spuder/OpenSpool](https://github.com/spuder/OpenSpool), [U1 extension](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/design/openspool.md) |
| **OpenPrintTag** (Prusa) | **NFC‑V / ISO 15693**, спроектирован под **ICODE SLIX2 320 B** | NDEF‑запись MIME `application/vnd.openprinttag`. Payload из секций CBOR: meta, main (статика), aux (динамика, например расход, пишет принтер). В сообщении **разрешены другие NDEF‑записи** | Возможны пароль/lock на всё, кроме aux | Да: Android, iOS (Core NFC `NFCISO15693Tag`), PN5180 | Да | [specs.openprinttag.org](https://specs.openprinttag.org), репо [OpenPrintTag/openprinttag-specification](https://github.com/OpenPrintTag/openprinttag-specification), лицензия **MIT** (GitHub API). Раздел про данные: `docs_src/nfc_data_format.md` §2 |
| **TigerTag** | NTAG213 (ISO14443‑3, NTAG21x) | **Бинарный** payload 144 Б на страницах 0x04–0x27 + онлайн/офлайн база продуктов | Нет | Да (их приложения, OpenRFID, SpoolSense) | Да | [TigerTag-RFID-Guide](https://github.com/TigerTag-Project/TigerTag-RFID-Guide), **CC‑BY‑4.0** |
| OpenTag3D (для полноты) | NTAG215/216 | NDEF binary | Нет | Да | Да | [opentag3d.info](https://opentag3d.info/) |

### 1.2 Поддержка в Spoolman и мостах
- **Spoolman v0.27.0** ([релиз 2026‑09‑25](https://github.com/Donkie/Spoolman/releases/tag/v0.27.0), [wiki «Tag scanners»](https://github.com/Donkie/Spoolman/wiki/Tag-scanners)) имеет **нативную привязку меток по аппаратному UID** к spool или filament:
  - `POST /api/v1/spool/{id}/tag`, `DELETE /api/v1/spool/{id}/tag/{uid}`;
  - `POST /api/v1/tag/scan` (ридер присылает UID, ответ `matched_spool_id` / `matched_filament_id`);
  - `GET /api/v1/spool?tag=<uid>`, websocket `/api/v1/tag/scan`;
  - Web NFC на Android по HTTPS.
  
  Работает с **любым** форматом (NTAG, Bambu, Creality, Qidi, Prusament, TigerTag), потому что смотрит только UID. **Содержимое не декодирует**. В коде (`spoolman/tags.py`) зарезервирован `TARGET_LOCATION`, но он ещё не реализован.
- Декодирование содержимого:
  - OpenPrintTag CBOR: PR [Donkie/Spoolman#1112](https://github.com/Donkie/Spoolman/pull/1112), **open, не смержен**;
  - TigerTag / Qidi / OpenPrintTag: форк [Spoolman‑NG](https://github.com/sherrmann/Spoolman-NG).
- Мосты и ридеры:
  - [nfc2klipper](https://github.com/bofh69/nfc2klipper): PN532 на хосте Klipper, `extra.nfc_id`, умеет OpenPrintTag/OpenTag3D;
  - [SpoolSense scanner](https://github.com/SpoolSense/spoolsense_scanner): ESP32 + PN5180/PN532/RC522. Читает и пишет OpenPrintTag, TigerTag, OpenTag3D, OpenSpool, расшифровывает Bambu, сам регистрирует катушки в Spoolman, есть MQTT + HA discovery;
  - [FilaMan](https://github.com/ManuelW77/Filaman), [SpoolmanScale](https://github.com/Niko11111/SpoolmanScale): ESP32 + PN532 + весы;
  - paxx12 U1 Spoolman + приложения SpoolLink, SpoolPainter ([spoolman.md](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/spoolman.md));
  - [OpenSpool](https://github.com/spuder/OpenSpool): ESP32+PN532 → Bambu по MQTT. Поддержка Spoolman у них только «planned».
- QR в Spoolman: штатные этикетки кодируют `WEB+SPOOLMAN:S-<id>` ([helixscreen#1744](https://github.com/prestonbrown/helixscreen/issues/1744), [spoolabel](https://github.com/mochery/spoolabel)).

### 1.3 Ответ по принтерам
| Принтер | Встроенный ридер | Метки вендора | **Чужие** вендорские | **Наши** (NTAG/OpenSpool, SLIX2/OPT) | Чем закрываем |
|---|---|---|---|---|---|
| **Snapmaker U1, сток** (текущий) | 4 канала FM17580, ISO14443A | Snapmaker M1 + RSA → `OFFICIAL=true`, `CARD_UID` доступен | Нет. Bambu/Creality M1 не проходят подпись, UID не отдаётся | **Нет** (NTAG игнорируется, NFC‑V аппаратно не поддерживается) | Станция: «загрузить в U1/eN» → 3d-api пишет слот через `SET_PRINT_FILAMENT_CONFIG` [INFERENCE: не выполнял, это write]. Для официальных катушек Snapmaker 3d-api читает `filament_detect.info[n].CARD_UID` и находит spool в Spoolman по `?tag=` |
| **U1 + расширенная прошивка paxx12** (решение за пользователем) | То же железо | Да | Bambu/Creality при вводе ключей (OpenRFID), Anycubic, Qidi, TigerTag, OpenTag3D. Плюс UID‑only отчёт для Spoolman | **NTAG213/215/216 OpenSpool: да**. OpenPrintTag: нет | Нативно при вставке. Spoolman по UID |
| **Creality K1 SE, сток** | **Нет**. RFID появляется только с CFS + [K1 Series CFS Upgrade Kit](https://www.creality3dofficial.eu/products/k1-series-cfs-upgrade-accessory-kit) (K1 SE в списке совместимых) | — | — | — | Только станция или телефон: «загрузить в K1SE». С CFS можно было бы писать метки в формате Creality |
| **Bambu P1S + AMS** (сосед) | AMS читает MIFARE Classic | Bambu (подпись) | Нет | **Нет** (подпись). Обход: OpenSpool‑ридер выставляет слот AMS через MQTT. На свежих прошивках для этого нужны LAN‑only + Developer Mode ([Bambu wiki](https://wiki.bambulab.com/en/knowledge-sharing/enable-developer-mode), [3rd‑party integration](https://wiki.bambulab.com/en/software/third-party-integration)) | В HA через [ha-bambulab](https://github.com/greghesp/ha-bambulab) видны атрибуты лотка `tag_uid` / `tray_uuid` (есть в переводах сенсоров) → авто‑привязка Bambu‑катушек к Spoolman по UID, если принтер в нашем HA |
| **Bambu A1 mini + AMS lite** (друг) | AMS lite читает RFID «by default, cannot be disabled» ([wiki](https://wiki.bambulab.com/en/ams-lite/manual/intro-ams-lite)) | Bambu | Нет | Нет (как у P1S) | Как у P1S. Для не‑Bambu катушек слот выставляют руками (по [странице AMS lite](https://us.store.bambulab.com/products/ams-lite): «Non Bambu Lab Filaments need to be manually configured») |

---

## 2. Рекомендация по собственной маркировке

### 2.1 Что клеить на катушки без меток
**Основная метка: NTAG215, круглая наклейка 25 мм, две штуки на катушку (по одной на каждую щёчку), формат OpenSpool + URI.**

Почему именно NTAG215:
- Это **единственный** класс наших меток, который читают сразу U1 на расширенной прошивке, наш PN532 в ESPHome, любой Android и iPhone (Core NFC: MIFARE Ultralight‑family, [NFCMiFareFamily](https://developer.apple.com/documentation/corenfc/nfcmifarefamily)), Spoolman (по UID), а также OpenSpool‑ридеры для Bambu.
- ICODE SLIX2 / OpenPrintTag в нашем парке **не читает ни один принтер**. U1 не умеет аппаратно. Prusa‑принтеров у нас нет. Под ISO 15693 в ESPHome нужен PN5180, а официального компонента нет (см. §3). Сейчас это только плюс к цене и сложности.
- Две метки нужны, потому что антенна U1 смотрит на ту щёчку, что ближе к корпусу, а сторона зависит от держателя (left/right). Creality делает так же ([K2-RFID-Flipper, «Both tags must be written»](https://github.com/mitchsurp/K2-RFID-Flipper)).
- Если на катушке уже есть вендорская метка (Bambu/Creality), **свою не клеим**: paxx12 предупреждает, что вендорская метка мешает читать пользовательскую. Вендорскую привязываем к Spoolman **по UID**.

**Содержимое NDEF (одно сообщение, 2 записи, в сумме около 260 Б из 504 Б NTAG215):**
1. URI‑запись, **первой**, чтобы Android/iOS обрабатывали её при касании телефоном. Варианты:
   - (а) `https://www.home-assistant.io/tag/sp-<spoolman_id>`. HA Companion сам шлёт `tag_scanned` с `tag_id="sp-42"`, приложение ставить не нужно. Фоновое чтение на iPhone XS и новее ([HA Tags](https://www.home-assistant.io/integrations/tag/)). **Рекомендую**.
   - (б) `https://3d.<домен>/s/<id>`: открывает страницу 3d-api, но не создаёт событие в HA.
2. MIME `application/json` в формате OpenSpool U1 Extended: `protocol, version, brand, type, subtype, color_hex, alpha, min_temp, max_temp, bed_min_temp, bed_max_temp, weight, diameter` плюс наше поле `"spool_id": <id>`. Неизвестные поля парсер paxx12 игнорирует [INFERENCE: по коду `openspool_parse_payload` читаются только известные ключи]. Поле `spool_id` в OpenSpool используют SpoolLink и SpoolPainter (paxx12 spoolman.md).

**Схема идентификаторов:**
- **Первичный ключ катушки: аппаратный UID метки**, привязанный в Spoolman (`POST /api/v1/spool/{id}/tag`, оба UID, если меток две). Работает и для вендорских меток, которые мы не можем записать.
- **Вторичный: `sp-<spoolman_id>`** в URI или JSON. По нему катушку находит телефон через HA и паспорт на бумаге. Spoolman предупреждает: `tag_id` из HA Companion ≠ UID. 3d-api должен понимать оба варианта.
- Spoolman id остаётся канонической сущностью. Своих UUID не вводим, следуя YAGNI.
- **Дублирующий QR** на бумажной этикетке катушки: Spoolman `WEB+SPOOLMAN:S-<id>` или тот же URL. Это бесплатный резерв, если NFC не работает (например, iPhone без фонового чтения).

### 2.2 Принтеры без ридера (K1 SE, U1 на стоке для наших катушек, Bambu для не‑Bambu катушек)
Схема «скан‑скан» на станции или телефоне:
1. Сканируем метку катушки.
2. Сканируем метку слота (наклейка на держателе: `slot:u1/e2`, `slot:k1se/0`) или выбираем принтер и слот энкодером.
3. 3d-api записывает `spool → printer/slot` и по возможности пушит данные в принтер:
   - U1 на стоке: **ничего не пишем** (решение D4: только чтение). `SET_PRINT_FILAMENT_CONFIG` — отдельный тикет с явным подтверждением, не путь по умолчанию;
   - K1 SE: Moonraker (если доступен) или только учёт [INFERENCE];
   - Bambu: MQTT `ams_filament_setting` при Developer Mode, а без него только учёт.
4. Выгрузка: «unload» или вставка новой катушки в тот же слот автоматически закрывает предыдущую привязку.

Moonraker‑интеграция Spoolman держит **один** активный spool (`/server/spoolman/spool_id`). Для 4 голов U1 соответствие слот→spool 3d-api ведёт сам [INFERENCE по API Moonraker].

### 2.3 Ячейки хранения
- На каждую ячейку, коробку, полку, сушилку и слот принтера клеим **NTAG213 (хватает 144 Б) + напечатанный QR с тем же ID**.
- ID: `cell:<стеллаж>-<полка>-<место>` (например, `cell:A-2-03`), `dryer:<n>`, `slot:<printer>/<n>`. NDEF URI: `https://www.home-assistant.io/tag/cell-A-2-03`.
- Привязка UID ячейки хранится **в 3d-api**: `TARGET_LOCATION` в Spoolman пока не реализован. В Spoolman пишем только `spool.location = "A-2-03"`, это штатное строковое поле.
- Цены в Сербии (Elektro Modul, [категория](https://elektromodul.rs/nfc-stikeri/nfc-kartice-tagovi-i-stikeri/)): NTAG213 наклейка 70–90 RSD/шт, NTAG215 карта/наклейка около 100 RSD/шт. На AliExpress («NTAG215 sticker 25mm 50pcs», «NTAG213 sticker 100pcs») примерно 0,08–0,15 $/шт [INFERENCE: страницы AliExpress не открывал].

### 2.4 Входящие Prusament / OpenPrintTag
Отдельно ничего не клеим. Привязываем по UID: с Android через Spoolman Web NFC (нужен HTTPS) или через NFC Tools. iOS умеет ISO 15693 через Core NFC ([NFCISO15693Tag](https://developer.apple.com/documentation/corenfc/nfciso15693tag)). Как вариант на будущее, в станцию можно добавить PN5180 (§3.2).

---

## 3. ESPHome‑станция

> Конфиги ESPHome для станции (D32 и S3: `esphome/station.yaml`, `esphome/d32/*.yaml`) опубликованы в [alex-mextner/kettle-xiaomi](https://github.com/alex-mextner/kettle-xiaomi).

### 3.1 Операции
| Код | Действие | Что отправляет |
|---|---|---|
| `load` | загрузить в принтер X / голову‑слот Y | spool_uid, printer, slot |
| `unload` | выгрузить (вернуть в ячейку или «на столе») | spool_uid, [weight] |
| `dry_in` / `dry_out` | в сушилку / из сушилки (журнал сушки: T, время, вес до/после) | spool_uid, dryer, weight |
| `store` | положить в ячейку (вторым касанием метка ячейки) | spool_uid, cell_uid |
| `weigh` | взвесить. Spoolman сам вычитает тару (`spool_weight` у филамента или `empty_spool_weight` у вендора) | spool_uid, gross_g |
| `note` | проблема: «путается», «влажный», «ломкий»; выбор из списка энкодером | spool_uid, code |
| `bind` | новая метка → создать или привязать spool (UID в Spoolman) + записать NDEF | uid, spool_id |

UX: энкодер выбирает операцию, кнопка OK подтверждает, кнопка BACK отменяет. Касание катушкой, потом при необходимости вторым касанием цель. Перед отправкой экран показывает «PLA Black eSUN · 642 g · A‑2‑03». **Мутация уходит только по OK**: касание лишь выбирает катушку; запрос несёт одноразовый `nonce`, 3d-api отбрасывает повторы (replay).

### 3.2 Выбор ридера (статус в ESPHome)
| Ридер | Протоколы чипа | ESPHome | Итог |
|---|---|---|---|
| **PN532** (Elechouse V3 и клоны) | ISO14443A (NTAG, MIFARE Classic/Ultralight, читает UID Bambu/Creality/Snapmaker) | **Официальный** `pn532_spi`/`pn532_i2c`, `on_tag` с `x`=UID и `tag` с NDEF; чтение и запись NDEF ([docs](https://esphome.io/components/binary_sensor/pn532/)) | **Рекомендую для v1** |
| RC522 | ISO14443A | Официальный `rc522_spi`/`_i2c`, **только UID**, без NDEF ([docs](https://esphome.io/components/binary_sensor/rc522/)) | Годится, если нужен только UID |
| **PN5180** | ISO14443A + **ISO15693** (SLIX2) | **Официального нет** (feature request [#1972](https://github.com/esphome/feature-requests/issues/1972)). Внешний [JohnMcLear/esphome_pn5180](https://github.com/JohnMcLear/esphome_pn5180): только 14443A и UID. Для 15693 есть Arduino‑библиотека [ATrappmann/PN5180-Library](https://github.com/ATrappmann/PN5180-Library) или готовая не‑ESPHome прошивка [SpoolSense scanner](https://github.com/SpoolSense/spoolsense_scanner) | Опционально, если понадобится Prusament/OpenPrintTag |
| PN7160 / PN7150 | Чип умеет T5T (15693) | Официальный `pn7160_spi`/`_i2c`, но код `pn71xx` опрашивает **только NFC‑A/B/F** (`RF_DISCOVERY_CONFIG` в [pn71xx.h](https://github.com/esphome/esphome/blob/dev/esphome/components/pn71xx/pn71xx.h)), NFC‑V не опрашивает | Дороже PN532 и не даёт 15693 |
| ST25R3911B / ST25R95 | 14443 + 15693 | В ESPHome нет | Не рекомендую |

Spoolman wiki пришла к той же развилке: PN532 или RC522 «out of the box», PN5180 «only one that reads ISO15693… no official ESPHome component».

### 3.3 BOM (цены от 2026‑10‑14)
| # | Компонент | Сербия (проверено на сайте) | AliExpress (поисковый запрос, ≈цена [INFERENCE]) |
|---|---|---|---|
| 1 | ESP32‑S3 (N16R8, devkit). Подойдёт и обычный ESP32 | Elektro Modul: ESP32‑S3 Nano N16R8 **1 900 RSD**, ESP32‑S3‑Zero **860 RSD** ([поиск](https://elektromodul.rs/?s=esp32-s3&post_type=product)); MikroPrinc: ESP32 38‑pin ([стр.](https://www.mikroprinc.com/sr/proizvod/esp32-38-pin), цену не извлёк) | «ESP32-S3-DevKitC-1 N16R8» ≈ 6–9 $ |
| 2 | PN532 V3 | Elektroleum (Белград) **1 500 RSD** ([стр.](https://elektroleum.rs/product/pn532-nfc-rfid-v3-module-elektronske-komponente-senzori-beograd/)); Proelectronic ([стр.](https://www.proelectronic.rs/shop/arduino-razvojni-sistemi/1356-mhz-pn532-nfc-kartica-modul-citaca-kompatibilana-sa-raspberry-pi/)) | «PN532 NFC V3 module» ≈ 3–5 $ |
| 2′ | (опц.) PN5180 | Elektro Modul **1 150 RSD** ([категория](https://elektromodul.rs/nfc-stikeri/)) | «PN5180 ISO15693 module» ≈ 6–10 $ |
| 3 | Дисплей: OLED SSD1306 0.96″ I²C. Альтернатива TFT ST7789 | Elektro Modul: SSD1306 **450 RSD**; TFT 1.3″ 240×240 **820 RSD**; ST7789V 240×320 **1 050 RSD** ([поиск](https://elektromodul.rs/?s=st7789&post_type=product)) | «SSD1306 0.96 I2C» ≈ 2 $; «ST7789 1.9 inch» ≈ 4 $ |
| 4 | Энкодер EC11 с кнопкой | Elektro Modul **230–250 RSD** | «EC11 rotary encoder module» ≈ 1 $ |
| 5 | 2–3 тактовые кнопки | у тех же продавцов [INFERENCE ≈ 30–60 RSD/шт] | ≈ 1 $ за набор |
| 6 | HX711 | Elektro Modul **220 RSD** (зелёный) / **450 RSD** (синий) ([стр.](https://elektromodul.rs/load-cell-hx711-zeleni-modul-cena-srbija/)) | «HX711 5kg load cell kit» ≈ 3–5 $ (вместе с ячейкой) |
| 7 | Тензодатчик‑балка 5 кг | Elektro Modul, раздел [Senzori težine](https://elektromodul.rs/arduino/senzori/senzor-tezine/): load cell от **250–300 RSD**. Конкретно 5 кг в листинге не подтвердил | в комплекте с п.6 |
| 8 | Метки NTAG215 25 мм (катушки), NTAG213 (ячейки) | Elektro Modul 70–100 RSD/шт (дорого для партии) | «NTAG215 sticker 25mm» ≈ 0,1 $/шт |
| 9 | (опц.) ICODE SLIX2 | Prusa «Blank OpenPrintTag (10 NFC Tags)» **5,99 $** ([стр.](https://www.prusa3d.com/product/blank-openprinttag-10-nfc-tags/)) | «ICODE SLIX2 sticker» [INFERENCE ≈ 0,3–0,6 $/шт] |
| 10 | БП USB‑C 5 В, провода, макетка, корпус и платформа весов (печатаем сами) | ≈ 1 000 RSD [INFERENCE] | — |

**Итого:** около **6 000–7 000 RSD** (≈ 50–60 €) из сербских магазинов или **≈ 25–35 $** с AliExpress, без опционального PN5180. Kelco и MikroPrinc: поиск по их сайтам не вернул PN532/HX711 (поиск у них, судя по всему, работает на JS), наличие не проверено. ComTrade (потребительская ИТ‑розница) и «Elektronika Pavlović» не проверены: подтверждений, что они продают такие модули, я не нашёл.

### 3.4 Скетч ESPHome YAML
Скетч не компилировал. Имена компонентов и API сверены с исходниками ESPHome dev (`nfc/ndef_record.h`: `get_type()` / `get_payload()`; `http_request`: `json`, `capture_response`; `api`: `homeassistant.event` / `tag_scanned`).
```yaml
substitutions:
  station_id: "spool-station-1"
  api_base: "https://3d-api.lan:8443"           # 3d-api; пути ниже — /v1/... (plan.md §11.1)
  spoolman: "http://spoolman.lan:7912/api/v1"

esphome:
  name: ${station_id}
esp32:
  board: esp32-s3-devkitc-1
  framework: { type: esp-idf }
wifi: { ssid: !secret wifi_ssid, password: !secret wifi_password }
logger:
api:            # HA: события esphome.* и tag_scanned
ota: [{ platform: esphome }]

globals:
  - { id: op_idx, type: int, restore_value: no, initial_value: '0' }       # 0..6 → load/unload/dry_in/dry_out/store/weigh/note
  - { id: target_idx, type: int, restore_value: no, initial_value: '0' }   # индекс printer/slot из списка
  - { id: spool_uid, type: std::string, restore_value: no, initial_value: '""' }
  - { id: spool_label, type: std::string, restore_value: no, initial_value: '""' }  # «PLA Black · 642 g» из ответа 3d-api
  - { id: tare_raw, type: float, restore_value: yes, initial_value: '0' }

spi:
  clk_pin: GPIO12
  mosi_pin: GPIO11
  miso_pin: GPIO13

i2c: { sda: GPIO8, scl: GPIO9 }

http_request:
  timeout: 5s
  verify_ssl: true              # токен станции не уходит по plain HTTP; CA 3d-api прошит в прошивку

pn532_spi:
  id: nfc
  cs_pin: GPIO10
  update_interval: 1s
  on_tag:
    then:
      - lambda: |-
          // UID в виде "04-a2-b3-..."; payload OpenSpool достаём из NDEF, если он есть
          id(spool_uid) = x;
      - http_request.post:          # 1) резолв + регистрация скана в Spoolman (по UID)
          url: ${spoolman}/tag/scan
          request_headers: { Content-Type: application/json }
          json: |-
            root["uid"] = x;
            root["reader_id"] = "${station_id}";
      - http_request.post:          # 2) операция в 3d-api (3d-api сам ходит в Spoolman/принтеры)
          url: ${api_base}/v1/tags/scan    # контракт plan.md §11.1
          capture_response: true
          request_headers:
            Content-Type: application/json
            Authorization: !secret printops_station_token_header   # "Bearer <токен scope station>"
          json: |-
            static const char* OPS[] = {"load","unload","dry_in","dry_out","store","weigh","note"};
            root["station"] = "${station_id}";
            root["uid"] = x;
            root["op"] = OPS[id(op_idx)];
            // target_idx → {printer, slot} | location по списку из /v1/station/config
            root["target"] = id(target_idx);
            root["weight_g"] = id(scale_g).state;
          on_response:
            then:
              - lambda: |-
                  if (response->status_code == 200) id(spool_label) = body.substr(0, 40);
                  else id(spool_label) = "ERR " + to_string(response->status_code);
      - homeassistant.event:        # параллельно в HA (журнал, автоматизации)
          event: esphome.spool_station
          data:
            station: ${station_id}
            uid: !lambda 'return x;'
            op: !lambda 'static const char* O[]={"load","unload","dry_in","dry_out","store","weigh","note"}; return O[id(op_idx)];'

sensor:
  - platform: hx711
    id: scale_raw
    dout_pin: GPIO4
    clk_pin: GPIO5
    gain: 128
    update_interval: 0.5s
    internal: true
  - platform: template
    id: scale_g
    name: "Spool gross weight"
    unit_of_measurement: g
    accuracy_decimals: 0
    update_interval: 0.5s
    lambda: 'return (id(scale_raw).state - id(tare_raw)) / 420.0f;'   # 420 = коэффициент калибровки, подобрать гирей
  - platform: rotary_encoder
    id: enc
    pin_a: { number: GPIO6, mode: INPUT_PULLUP }
    pin_b: { number: GPIO7, mode: INPUT_PULLUP }
    on_clockwise:     [ lambda: 'id(op_idx) = (id(op_idx) + 1) % 7;' ]
    on_anticlockwise: [ lambda: 'id(op_idx) = (id(op_idx) + 6) % 7;' ]

binary_sensor:
  - platform: gpio            # кнопка энкодера OK: подтверждение — здесь (а не в on_tag) отправляется POST /v1/tags/scan
    pin: { number: GPIO15, mode: INPUT_PULLUP, inverted: true }
    name: "OK"
    on_click: [ script.execute: send_scan ]   # набросок: тело запроса как выше, перенесённое из on_tag в script
  - platform: gpio            # TARE (пустая платформа)
    pin: { number: GPIO16, mode: INPUT_PULLUP, inverted: true }
    name: "Tare"
    on_click: [ lambda: 'id(tare_raw) = id(scale_raw).state;' ]
  - platform: gpio            # BACK / отмена
    pin: { number: GPIO17, mode: INPUT_PULLUP, inverted: true }
    name: "Back"
    on_click: [ lambda: 'id(spool_uid) = ""; id(spool_label) = "";' ]

font:
  - { file: "gfonts://Roboto", id: f12, size: 12 }

display:
  - platform: ssd1306_i2c
    model: "SSD1306 128x64"
    address: 0x3C
    update_interval: 250ms
    lambda: |-
      static const char* O[] = {"LOAD","UNLOAD","DRY IN","DRY OUT","STORE","WEIGH","NOTE"};
      it.printf(0, 0,  id(f12), "Op: %s  T:%d", O[id(op_idx)], id(target_idx));
      it.printf(0, 16, id(f12), "Tag: %s", id(spool_uid).c_str());
      it.printf(0, 32, id(f12), "%s", id(spool_label).c_str());
      it.printf(0, 48, id(f12), "%.0f g", id(scale_g).state);
```
Что надо доработать при реализации:
- Список целей (`target_idx`) станция должна загружать из `GET ${api_base}/station/config` в начале работы, а не держать захардкоженным.
- Нужна двухшаговая схема «скан катушки → скан ячейки или слота»: если UID принадлежит ячейке, 3d-api считает это вторым шагом.
- Запись NDEF (`bind`) делается через `tag.set_write_message` / `pn532` write mode. Запись OpenSpool JSON как MIME‑записи из YAML ESPHome штатными action'ами невозможна: они пишут URI или текст [INFERENCE по docs: `set_write_message` формирует URI‑запись]. Поэтому JSON удобнее писать телефоном (PrintTag‑Web, NFC Tools, SpoolPainter) либо через lambda с `nfc::NdefMessage::add_record`.
- Для `homeassistant.event` имя события должно начинаться с домена `esphome.`. Возможно, в настройках ESPHome‑устройства в HA нужно включить «allow device to perform HA actions» [INFERENCE].

### 3.5 Интеграция с HA
1. ESPHome‑устройство подключается к HA через native API. Оно шлёт `esphome.spool_station` (журнал, уведомления) и при желании `homeassistant.tag_scanned`, чтобы в HA появились сущности `tag`.
2. Автоматизация HA на `tag_scanned` с `tag_id` вида `sp-*` или `cell-*` (касание телефоном с HA Companion) вызывает `rest_command` к 3d-api `POST /v1/tags/scan` (bearer-токен клиента `ha`) с `source=phone`. Затем HA присылает actionable notification «Загрузить в U1/e? · В сушилку · В ячейку», и ответ уходит в 3d-api. Это даёт «станцию в кармане» без железа.
3. 3d-api — источник правды по операциям. Spoolman хранит катушки, UID, `location`, вес. Журнал сушки ведёт 3d-api.
4. Сенсоры HA читают из 3d-api `GET /api/v1/printers/{p}/slots`: что стоит в слотах, остаток, «катушка в сушилке N ч».

### 3.6 Эндпоинты 3d-api (предложение)

Канонический контракт — [plan.md §11.1](plan.md): `POST /v1/tags/scan {uid, payload?, op, printer?, slot?, location?, weight_g?, nonce}` по HTTPS с проверкой сертификата и bearer-токеном узкого scope `station` (только операции станции). Таблица ниже — **исходный набросок, заменён этим контрактом**: префикс `/api/v1` и `/station/scan` не использовать; при расхождении прав plan.md. `target` из конфига станции 3d-api разворачивает в `printer`/`slot`/`location`. Нужны контрактные тесты «станция → 3d-api»: аутентификация, отказ plain HTTP и неверного сертификата, отказ повтора `nonce`.

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/api/v1/station/config?station=` | Операции, список целей (printers/slots, dryers), коды проблем |
| POST | `/api/v1/station/scan` | Единая точка входа: `{station, uid, op, target, gross_g, source}`. 3d-api: (1) резолвит UID → spool через Spoolman `GET /spool?tag=` или → cell по своей таблице; (2) выполняет op; (3) возвращает короткую строку для экрана |
| POST | `/api/v1/spools/{id}/load` | `{printer, slot}`: привязка; push в принтер — только Bambu MQTT при Developer Mode (D7: сейчас ручной режим), U1 сток — нет (D4) |
| POST | `/api/v1/spools/{id}/unload` | Снять привязку |
| POST | `/api/v1/spools/{id}/dry` | `{dryer, action: start|stop, temp_c, weight_g}` → drying_log |
| POST | `/api/v1/spools/{id}/location` | `{cell}`: в Spoolman `PATCH /spool/{id}` `location` |
| POST | `/api/v1/spools/{id}/weigh` | `{gross_g}`: в Spoolman `PUT /spool/{id}/measure {weight}` (Spoolman сам вычитает тару, [код](https://github.com/Donkie/Spoolman/blob/master/spoolman/api/v1/spool.py)) |
| POST | `/api/v1/spools/{id}/notes` | `{code, text}` |
| POST | `/api/v1/tags/{uid}/bind` | `{spool_id}|{cell_id}`: для spool прокси в Spoolman `POST /spool/{id}/tag`, для cell запись в своей таблице |
| GET | `/api/v1/tags/{uid}` | Что это за метка: spool, cell, slot, неизвестная |
| (фон) | poll U1 `filament_detect` | Для официальных катушек Snapmaker: `CARD_UID` → `spool?tag=` → авто‑привязка к слоту eN |

---

## 4. Затраты и выгода, рекомендация

| Вариант | Цена | Плюсы | Минусы |
|---|---|---|---|
| **Телефон + HA Companion** | 0 (только метки) | Без железа. Android пишет NTAG. iPhone XS и новее читает в фоне. Есть actionable‑уведомления. Spoolman Web NFC на Android (по HTTPS) | Нет весов. Медленно: разблокировать, выбрать действие. `tag_id` в HA ≠ UID. iOS **не работает с MIFARE Classic** (только Ultralight/Plus/DESFire, [NFCMiFareFamily](https://developer.apple.com/documentation/corenfc/nfcmifarefamily)), поэтому вендорские Bambu/Creality/Snapmaker метки iPhone не привяжет. ISO 15693 (NFC‑V) на iOS читается и пишется через Core NFC ([NFCISO15693Tag](https://developer.apple.com/documentation/corenfc/nfciso15693tag)). Есть ли запись NFC‑V в самом HA Companion, не проверено [INFERENCE] |
| **QR‑наклейки + камера телефона** | ≈ 0 (бумага, принтер этикеток) | Spoolman печатает QR штатно (`WEB+SPOOLMAN:S-<id>`). Работает на любом телефоне | Нужна прямая видимость. Этикетки затираются. Принтеры QR не читают. Без весов |
| **Встроенные ридеры принтеров** | 0 | Полный автомат для «своих» катушек | U1 на стоке понимает только Snapmaker. AMS только Bambu. У K1 SE ридера нет. Наши метки поймёт только U1 на прошивке paxx12 |
| **ESPHome‑станция (PN532 + весы + энкодер + экран)** | ≈ 6–7 тыс. RSD или 25–35 $ | Одно касание вместо 5 действий в UI. **Весы** + авто‑тара из Spoolman. Работает с любым UID, включая вендорские MIFARE. Полностью локально. Spoolman v0.27 принимает её «из коробки» | Надо собрать и обслуживать. Prusament (NFC‑V) читается только с PN5180 и доп. работой |

**Рекомендация:**
1. **Метки:** на все немаркированные катушки клеить **2× NTAG215** (URI HA‑tag `sp-<id>` + OpenSpool JSON) и дублировать бумажным QR. Ячейки, сушилки и слоты принтеров маркировать **NTAG213 + QR**. Вендорские катушки (Bambu/Creality/Snapmaker/Prusament) не трогать, привязывать **по UID** в Spoolman v0.27+.
2. **Одна ESPHome‑станция на PN532 с весами** у стеллажа и сушилки. Это главный инструмент учёта. **Телефон через HA** — резерв для операций на месте, например у принтера соседа или друга.
3. **U1:** решение за пользователем. Расширенная прошивка paxx12 даёт нативное чтение наших NTAG/OpenSpool и UID‑отчёт в Spoolman. Цена: неофициальная прошивка (warning paxx12 про гарантию). Без неё 3d-api пишет слот через `SET_PRINT_FILAMENT_CONFIG` и ловит `CARD_UID` официальных катушек Snapmaker.
4. **OpenPrintTag/PN5180 отложить**, пока в парке нет принтера с NFC‑V или не появилась заметная доля Prusament. Для единичных катушек хватит телефона.
5. **Bambu (сосед, друг):** наши метки AMS не прочитает. Привязку катушки к слоту делаем через станцию или телефон. Если их принтеры будут в нашем HA (ha-bambulab), Bambu‑катушки привязываются автоматически по `tag_uid`.
