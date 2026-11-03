# Спецификация оставшихся реальных компонентов

Документ предназначен владельцам Perception, Clip, Retention, Site Config и
Dashboard; раздел Video Archive описывает только границу с существующей внешней
CCTV-системой. Все внешние payloads импортируются из
`javes_contracts`; перед handoff генерируются JSON Schema командой
`javes-contract-schemas --out schemas`.

Общие правила для всех компонентов:

- timestamps — ISO-8601 с offset, на границе нормализуются в UTC;
- IDs стабильны, непрозрачны, не переиспользуются для другого payload;
- обработка команд at-least-once: одинаковый ID + одинаковый payload возвращает
  тот же результат, одинаковый ID + другой payload — ошибка;
- сообщения содержат `contract_version`, неизвестная major/version отвергается;
- секреты, signed URLs и исходные изображения не попадают в обычные логи;
- retries имеют bounded exponential backoff и DLQ/FAILED visibility;
- worker не принимает авторизацию и business decisions от Dashboard напрямую;
- before-enable gate: JSON Schema + `javes_contracts.acceptance` + component
  contract tests + staging shadow run.

Production transport для Perception, Clip и Retention — authenticated HTTP
pull/lease: `claim`, `heartbeat`, `result`, `failure` под
`/internal/v1/workers/{component}/tasks`. Routing использует отдельные
синхронные `/internal/v1/routing/init|step`; Site Config — exact-version GET
API. Локальные Python ports сохранены только как test seams.

## 1. Perception worker

Реализация: `javes_perception/` (YOLO26 + ByteTrack + OSNet). Как она устроена,
какая у неё точность и что ещё не сделано — в [`perception.md`](perception.md).
Ниже — требования к любому Perception worker.

### 1.1 Ответственность

Perception читает video segment, детектирует и трекает людей, формирует
tracklets, ранжирует кандидатов относительно target gallery и при активном
сопровождении публикует live metadata. Он не строит маршруты, не решает
CONFIRM/REJECT, не создаёт кейсы и не рисует карту.

Рекомендуемые внутренние модули:

```text
consumer -> command dedupe -> archive client -> decode/time normalisation
         -> detector -> tracker -> tracklet builder -> Re-ID/calibration
         -> artifact/crop writer -> result publisher
         -> optional live publisher
```

### 1.2 Gallery input/output

Вход `TargetGalleryCommand`:

```json
{
  "contract_version": "target-gallery.v1",
  "command_id": "gallery_cmd_01",
  "case_id": "case_17",
  "source": "reference",
  "image_ref": {"object_key": "cases/case_17/reference.jpg"},
  "tracklet_id": null,
  "model_version": "reid-v3"
}
```

`source=reference` требует `image_ref`; `source=confirmed_tracklet` требует
`tracklet_id`. Выход `TargetGalleryResult` повторяет `command_id/case_id` и
возвращает стабильный `gallery_ref`, число источников и фактическую model
version. Повторный command не должен повторно добавлять один источник.

### 1.3 Probe input

`PerceptionProbeJob` содержит job/case, gallery и ровно один `ProbeRequest`:

```json
{
  "contract_version": "perception-job.v1",
  "job_id": "job_88",
  "case_id": "case_17",
  "kind": "PROBE",
  "target_gallery_ref": "gallery_case_17_v3",
  "probe": {
    "probe_id": "p_41",
    "camera_id": "B",
    "t_from": "2026-10-16T05:16:00Z",
    "t_to": "2026-10-16T05:18:00Z",
    "priority": 0.72,
    "reason": "ROUTE_EXPANSION",
    "hypothesis_ids": ["h_1"],
    "retry_count": 0,
    "retry_of": null
  },
  "deadline": "2026-10-16T05:20:00Z"
}
```

Один probe — одна камера. Priority влияет на очередь, но не меняет результат.
`hypothesis_ids` нужны для трассировки, а не для изменения CV. Finite window
клипуется к запрошенным границам. Camera clock offset уже применён ingestion или
Archive; Perception отдаёт `UTC_CORRECTED` времена и не добавляет offset повторно.

### 1.4 Probe output

```json
{
  "probe_id": "p_41",
  "status": "DONE",
  "coverage_ratio": 1.0,
  "processed_duration_s": 120.0,
  "n_tracklets_total": 7,
  "stream_closed": false,
  "error_code": null,
  "tracklets": [{
    "tracklet_id": "t_412",
    "case_id": "case_17",
    "probe_id": "p_41",
    "camera_id": "B",
    "t_start": "2026-10-16T05:16:52Z",
    "t_end": "2026-10-16T05:17:11Z",
    "open": false,
    "clipped_start": false,
    "clipped_end": false,
    "bbox_first": [38, 210, 64, 150],
    "bbox_last": [598, 190, 70, 160],
    "crop_paths": [],
    "clip_ref": {"archive_id": "archive_B_20261016"}
  }],
  "candidates": [{
    "candidate_id": "cand_03",
    "tracklet_id": "t_412",
    "appearance_score": 0.68,
    "cosine": 0.57,
    "calibration_version": "reid-cal-v3"
  }]
}
```

Семантика:

- `DONE` + пустые arrays — важное отрицательное наблюдение;
- `FAILED`, `TIMEOUT`, `UNAVAILABLE` возвращают coverage 0 и никаких evidence;
- `coverage_ratio` — реально декодированная часть окна, не доступность камеры;
- `processed_duration_s` — фактические camera-seconds и обязательно для DONE;
- `n_tracklets_total` — все люди до Re-ID filter;
- bbox — source-frame `[x,y,width,height]`, не координаты карты;
- candidate обязан ссылаться на tracklet того же result;
- `appearance_score` в `[0,1]`; probability semantics допустима только с
  согласованной calibration version;
- stable `clip_ref` должен переживать retry и быть пригоден Clip worker;
- unknown/invalid frame не маскируется как DONE-empty.

### 1.5 OPEN и live

`"t_to":"OPEN"` создаёт subscription. Обновления используют тот же
`probe_id`; продолжающийся человек — тот же `tracklet_id`, неизменные
camera/t_start и неубывающий t_end. `processed_duration_s` кумулятивен для этого
probe. Закрытие по cancel/EOF возвращает `stream_closed=true`. После close новые
updates не публикуются.

Live preview отделён от Routing result. `LiveFeedUpdate` содержит metadata,
opaque короткоживущий `stream_ref`, bbox, observed/expiry и тип JPEG/MJPEG/HLS.
Bytes доступны только через Main Infra proxy. Dashboard не получает прямые
camera credentials или внутренний URL.

### 1.6 Ошибки и acceptance

Рекомендуемые error codes: `VIDEO_UNAVAILABLE`, `DECODE_FAILED`,
`CAMERA_UNKNOWN`, `GALLERY_UNAVAILABLE`, `MODEL_FAILED`, `DEADLINE_EXPIRED`,
`CANCELLED`. `TIMEOUT` означает истёкший deadline, `UNAVAILABLE` — данных нет,
`FAILED` — обработка могла быть выполнена, но результат недостоверен.

Обязательные tests: finite hit, DONE-empty, partial coverage, unavailable,
duplicate command, ID conflict, unknown gallery, timestamps/bbox, multiple
people, candidate correlation, OPEN upsert/close/cancel, cumulative duration,
restart recovery, callback retry, load/backpressure, calibration fixture.

## 2. Clip worker

### 2.1 Ответственность

Clip materializes review clips из стабильного archive/source ref, хранит
независимые processing и lifecycle state, и собирает подтверждённые клипы в
строго заданном порядке. Он не выбирает кандидатов, не меняет Routing и не
применяет retention policy самостоятельно.

Рекомендуемые модули: command consumer/dedupe, archive reader, precise trim,
transcoder, immutable artifact writer, metadata/checksum store, lifecycle
state machine, ordered exporter.

### 2.2 Cut

`CutClipCommand` содержит `clip_request_id`, case/candidate/tracklet/camera,
UTC window, padding, stable `source_ref`, `initial_state=HOLD`. Padding уже
включён Case Manager в `t_from/t_to`; worker не должен применить его второй раз.

`ClipRecord` возвращает те же correlation fields:

- processing `PENDING|READY|FAILED|UNAVAILABLE`;
- lifecycle `HOLD|CONFIRMED|RELEASED` только для usable artifact;
- фактические `t_start/t_end/duration_s`;
- stable `artifact_ref`, `mime_type`, cryptographic checksum;
- monotonic revision и error code.

Входной `source_ref.archive_id` — read-only ссылка на внешнюю CCTV-систему.
Выходной `artifact_ref` клипа/экспорта обязан указывать на хранилище,
принадлежащее JAVES-AI (`object_key` или owned URI), чтобы 14-дневная retention
policy могла удалить созданные нами bytes, не затрагивая исходный архив.

READY cut начинает с HOLD. Один `clip_request_id` всегда обозначает один и тот
же clip. Публикация artifact атомарна: consumers не видят partial file.

### 2.3 State

`ClipStateCommand` имеет уникальный command ID, case/clip, действие
`HOLD|CONFIRM|RELEASE`, actor и time. Worker проверяет принадлежность clip к
case, разрешённый переход, увеличивает revision и возвращает полный record.
Повтор той же команды idempotent. RELEASE означает право на policy cleanup, а
не мгновенное доказательство физического удаления.

### 2.4 Export

`ExportClipsCommand.ordered_clip_ids` — непустой уникальный список в порядке
visits лучшего маршрута. Worker не пересортировывает список. Все clips должны
быть READY+CONFIRMED и принадлежать case. Выход возвращает тот же порядок,
missing IDs при failure, либо один stable MP4 ref/checksum/duration при READY.

Обязательные tests: codec/timestamp precision, audio policy, unavailable source,
padding at archive boundaries, idempotent cut, ID conflict, state transitions,
early decision race, ordered multi-camera export, missing/unconfirmed clip,
atomic publish, checksum, restart/retry and concurrent duplicate delivery.

## 3. Retention worker

### 3.1 Ответственность

Retention — единственный компонент, который превращает policy command в
проверяемое удаление из хранилищ, принадлежащих JAVES-AI: ArtifactStore,
Perception/Clip outputs, metadata и caches. Внешний CCTV Video Archive ему не
принадлежит; `archive_id` является read-only source reference и никогда не
удаляется JAVES-AI. Право пользователя на DELETE проверяет Controls RBAC.

`ArtifactStore` — не облачный сервис, а принадлежащий JAVES-AI каталог на
локальном/подключённом диске. `object_key` — безопасный относительный путь внутри
этого каталога. Физическое удаление означает вызов `ArtifactStore.delete()` и
исчезновение самих bytes, а не только удаление ссылки из БД. Отдельные S3/MinIO
или другие облачные хранилища в текущей архитектуре не планируются.

### 3.2 Регистрация ресурсов

`RetentionResource` регистрируется до destructive command и содержит stable
resource ID, case, kind, optional artifact ref, class, TTL и legal hold:

- `EPHEMERAL`: temporary crops/intermediates;
- `REVIEW_HOLD`: clips до решения;
- `CASE_RESULT`: route JSON/SVG/export metadata;
- `CONFIRMED_EVIDENCE`: подтверждённые clips/exports;
- `AUDIT`: полный append-only журнал actor/time/action/details без TTL.

Повторная регистрация идентична; перенос resource ID между cases запрещён.
Поздняя регистрация в уже удалённый case должна немедленно попасть в cleanup,
а не воскресить данные.

### 3.3 Команды и результат

- `CLOSE_CASE`: фиксирует close marker, но не удаляет данные до 14-day TTL;
- `TTL_EXPIRED`: удаляет только истёкшие и не held ресурсы;
- `DELETE_CASE`: удаляет всё case-scoped, кроме legal hold и бессрочного
  append-only audit/tombstone.

`RetentionResult` коррелируется по command/case/action, перечисляет deleted и
preserved IDs без пересечения и выдаёт `audit_tombstone_id`. Успешный ответ
разрешён только после фактического завершения или durable deletion workflow с
проверяемым final status; «поставили в очередь» не равно «удалили».

Обязательные tests: each class, future/due TTL, legal hold, close vs delete,
late resource, object absent, partial backend failure, retry/resume, duplicate
command, ID conflict, permanent complete audit and deletion proof.

### 3.4 Реализованный Retention core

Production-shaped core находится в `javes_retention/` и напрямую реализует
существующий `RetentionPort`:

- `RetentionPolicy` ничего не удаляет на `CLOSE_CASE`, удаляет все
  non-audit/non-held ресурсы на `DELETE_CASE` и due/inherited ресурсы на
  `TTL_EXPIRED`; `AUDIT` и legal hold сохраняются;
- `SQLiteRetentionRepository` хранит inventory, case tombstones, command
  snapshot и per-resource attempts/proofs; полный append-only журнал действий
  хранится в Main Infra без TTL;
- `RetentionWorker` сначала durable-записывает intent, затем удаляет locations,
  продолжает после partial failure/restart и возвращает success только после
  final receipt для каждого locator;
- `DeletionRouter` требует backend для каждого product-owned `object_key` и URI;
  `archive_id` отклоняется как внешний read-only источник. Неизвестный locator
  не считается удалённым. Несколько locators у одного
  ресурса считаются replicas и должны завершиться все;
- есть безопасные backends для Main ArtifactStore и allow-listed `file://`, а
  также callback boundary для Perception/gallery и live storage;
- late registration после DELETE/TTL немедленно применяет tombstone; close
  marker сам по себе не удаляет поздний ресурс;
  снятие legal hold после DELETE также запускает cleanup;
- после final receipt resource inventory заменяет удалённые artifact paths на
  SHA-256 locators; Retention command и Main audit навсегда сохраняют actor,
  time, action и проверяемый proof;
- Main Infra регистрирует reference image, gallery metadata, tracklet/candidate
  metadata, crops/embeddings, live buffers, route SVG, clips и exports;
- новая route/export revision переводит предыдущую в `EPHEMERAL`, поэтому
  policy отличает текущие результаты от промежуточных; обе категории остаются
  до case TTL и удаляются на `TTL_EXPIRED`;
- после подтверждённого CLOSE Main сохраняет case payloads до retention
  deadline; после DELETE/TTL content redacted, а lifecycle становится
  `DELETED`;
- case TTL зафиксирован в 14 календарных дней от close и настраивается через
  `CaseManager(retention_ttl_s=...)`; scheduler выдаёт ровно один durable
  `TTL_EXPIRED`. `sweep_expired()` добирает explicit future TTLs после case
  tombstone.

Для локальной сборки используется `build_local_retention_worker(...)`.
`javes-retention-inspect retention.sqlite` показывает inventory и proofs без
деструктивных действий; `health_snapshot()` отдаёт state counts, legal holds,
failed deletion count/age и число tombstones для monitoring adapter. Реальные
Perception/gallery handlers являются integration adapters: их нельзя подменять
успешным no-op. Для локального ArtifactStore физическое удаление уже реализовано.

## 4. Video Archive

### 4.1 Ответственность

Video Archive — существующая внешняя CCTV-система, а не часть JAVES-AI. Она
принимает camera/time window и возвращает доступный immutable source segment
или описание partial/unavailable. Она знает codecs, storage tiers, camera clock
correction и source provenance. Routing и Main Infra к ней не обращаются;
consumers — Perception и Clip. JAVES-AI не управляет её retention и никогда не
удаляет данные по `archive_id`.

`VideoSegmentRequest`:

```json
{
  "contract_version": "video-archive-request.v1",
  "request_id": "seg_41",
  "camera_id": "B",
  "t_from": "2026-10-16T05:16:00Z",
  "t_to": "2026-10-16T05:18:00Z"
}
```

`VideoSegmentResult` повторяет requested window, возвращает
`AVAILABLE|PARTIAL|UNAVAILABLE`, covered range, coverage ratio, stable source
ref, `source_clock=UTC_CORRECTED`, error code. AVAILABLE строго 1.0, PARTIAL
между 0 и 1, UNAVAILABLE строго 0 без source ref.

Archive должен уметь обнаруживать holes, не считать container duration
истиной без timestamps, поддерживать range/stream access и выдавать short-lived
signed playback URL только через отдельную авторизованную операцию. Stable ref
не должен быть expiring URL.

Обязательные tests: full/partial/multiple holes/unavailable, boundary frames,
DST/timezone/clock offsets, codec variants, concurrent readers, cold tier,
corrupt segment, live-to-archive transition, immutable identity and checksum.

## 5. Site Config

### 5.1 Назначение и модель публикации

Site Config хранит не «текущий mutable YAML», а immutable опубликованные
snapshots `(site_id, version)`. Case pin-ит version при создании. Исправление
создаёт новую version; существующий кейс не меняет topology посреди поиска.

Состав snapshot:

```text
manifest.yaml
maps/<floor assets>
params reference/version (либо отдельный immutable RoutingParams registry)
checksums.json
publication metadata: author, created_at, status, schema version
```

Draft можно редактировать, published — нельзя. Publish pipeline:

1. parse strict YAML (`extra`/unknown fields запрещены);
2. validate unique node/edge/floor IDs и все ссылки;
3. validate `0 < t_min <= t_typ <= t_max`;
4. validate probabilities и normalize only with explicit warning/approval;
5. validate polygons, map positions and every `map_path` against floor bounds;
6. validate vertical transitions and entrances/borders;
7. verify every floor file, MIME, dimensions and checksum;
8. render preview/fallback SVG and run a tiny route smoke;
9. assign immutable version/checksums and atomically publish;
10. make snapshot readable before accepting new cases referencing it.

### 5.2 YAML minimum

```yaml
site: demo-building
version: "2026-10-16.1"
timezone: Asia/Seoul
map:
  floors:
    - id: 1F
      image: maps/1F.png
      size_px: [1600, 900]
      m_per_px: 0.05
cameras:
  A:
    zone: 1F-NW
    floor: 1F
    map_xy: [120, 340]
    clock_offset_s: 0
    regions:
      east: [[610, 80], [640, 80], [640, 300], [610, 300]]
  B:
    zone: 1F-N
    floor: 1F
    map_xy: [300, 220]
edges:
  - id: A-B-east
    from: A
    via: east
    to: B
    d_m: 25
    t_min: 10
    t_typ: 20
    t_max: 60
    map_path: [[120, 340], [210, 300], [300, 220]]
blind_nodes:
  - id: shop_1F
    kind: room
    floor: 1F
    map_xy: [500, 400]
    borders: [B]
    dwell: {median_s: 300, sigma: 0.9}
outside:
  entrances: {A: 0.6, B: 0.4}
  dwell: {median_s: 600, sigma: 1.1}
max_gap_s: 2400
```

Region polygons относятся к source video pixels; `map_xy/map_path` — к pixel
space соответствующего floor image. Межэтажный edge помечается `vertical:true`
и не имеет одной общей `map_path`: Routing поставит exit/entry markers.

`clock_offset_s` — metadata для ingestion/Archive. Site Config отдаёт его этим
компонентам, но поток, дошедший до Routing, уже UTC-corrected. Routing не
применяет offset.

### 5.3 API/port

Main Infra ожидает три read operations:

```python
get_graph(site_id, version) -> SiteGraph
get_floor_assets(site_id, version) -> dict[str, bytes]
get_params(version=None) -> RoutingParams
```

Ключ floor asset может быть `floor_id` или точный `floor.image`; рекомендуемый
канон — поддерживать оба. Read должен возвращать один согласованный snapshot.
При отсутствии version/asset запрос падает до создания кейса. Нельзя silently
подставить latest.

Обязательные tests: valid sample, every invalid reference/timing/coordinate,
duplicate IDs, probability handling, missing/wrong-size/corrupt asset,
multi-floor/vertical route, immutable published version, concurrent publish/read,
old version retrieval, checksum, Routing render smoke.

## 6. Dashboard

### 6.1 Auth и security

Dashboard имеет только страницу login username/password. Signup, invitation и
создание аккаунта в пользовательском UI отсутствуют. ROOT создаёт аккаунты в
административной части Main Infra, аналогично закрытой IAM console. После
первого входа пользователь с `must_change_password=true` видит только форму
смены пароля и logout.

UI строит доступные controls по permissions из `/v1/auth/me`, но backend RBAC
остаётся обязательным. OPERATOR не видит delete; VIEWER не видит mutation;
только ROOT видит IAM. Токен желательно держать в secure HttpOnly cookie после
добавления BFF/CSRF защиты; текущий API возвращает opaque bearer, поэтому MVP
клиент хранит его только в памяти и не пишет в localStorage/logs.

### 6.2 Экраны

- Login/change password.
- Case list: lifecycle, mode, revision, site/version, updated time.
- Create case: reference asset, last camera/zone+time, bounded search window,
  profile, explicit site/version.
- Case workspace: готовый SVG по этажам, mode/estimate, candidates, clips,
  live preview, unobserved/errors, progress.
- Review: candidate crop/video, camera/time/score/calibration label, explicit
  CONFIRM/REJECT with duplicate-submit protection.
- Close/export/delete dialogs как разные действия; delete показывает
  необратимость и доступен только permission.
- ROOT IAM: list/create/role/reset/disable/enable. Нет self-role/self-disable и
  нельзя удалить последнего active ROOT.

### 6.3 Data flow

Dashboard poll-ит `CaseView` или позднее получает websocket event, сравнивает
`revision/route_revision`, затем обновляет `<img src="...svg">`. SVG не
вставляется как untrusted inline HTML. Floor switch только меняет готовый URL.
Dashboard не знает `SearchState`, не вычисляет graph path/mode/score и не
вызывает worker.

Каждая mutation отправляет новый `Idempotency-Key`; retry того же действия
повторяет тот же key/payload. 401 ведёт на login, 403 скрывает/блокирует control,
409/revision conflict обновляет view, 422 показывает validation details без
утечки internals, 5xx даёт retry с тем же key.

Live URL отдаётся Main Infra, истекает и не сохраняется. UI показывает age и
expired/unavailable, bbox overlay относится к frame coordinates. Route SVG и
live content нельзя грузить с другого origin без утверждённой CSP/CORS policy.

Обязательные tests: permissions per role, no signup route, forced password
change, token expiry/logout, case list/create, revision polling, multi-floor SVG
display without geometry code, candidate idempotency, close/export/delete,
live expiry, keyboard/accessibility, CSP/XSS, slow/offline/API error states.

## 7. Порядок подключения

1. Заморозить v1alpha payloads совместно с владельцем компонента.
2. Реализовать adapter к текущему `Protocol`, не менять Case Manager под детали
   транспорта.
3. Прогнать generated Schema и функции `javes_contracts.acceptance` на golden,
   empty, duplicate, error и retry payloads.
4. Запустить тесты fake и real adapters одним набором сценариев.
5. Подключить staging archive/site, но оставлять результаты shadow-only.
6. Сравнить correlation, timings, coverage, costs и failure rates.
7. Canary включить один site/camera group с быстрым rollback на fake/disabled
   adapter (fake допустим только в non-production).
8. После observation window зафиксировать contract v1 либо выпустить новую
   совместимую version.

Готовность конкретного компонента определяется checklist в
[`roadmap.md`](roadmap.md), а не фактом успешного
ответа на один happy-path запрос.

