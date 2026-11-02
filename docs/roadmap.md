# Путь от текущего MVP к полному JAVES-AI

## 1. Точка старта

Сейчас готово ядро Routing и integration-ready Main Infra/Controls. Все
component boundaries исполняются fake-реализациями, поэтому можно продолжать
разрабатывать систему уже сейчас: стабилизировать orchestration, API, IAM,
persistence, dashboard contract и deployment skeleton без ожидания CV/media
команд.

Однако «останется просто подключить» верно только при одном условии: real
components реализуют уже зафиксированные порты и проходят acceptance. Если они
вернут другие timestamps, ID semantics, OPEN behavior или lifecycle, потребуется
не wiring, а изменение системы.

## 2. Этап 0 — текущая integration baseline

Готово, если:

- Routing tests, renderer, simulator и benchmark запускаются;
- fake end-to-end проходит create -> search -> result -> review -> close ->
  export -> delete;
- IAM no-signup и роли работают;
- contracts/schema/acceptance доступны;
- migrations и clean wheel проверены;
- архитектура/границы/не-готовые части документированы.

Это состояние подходит для параллельной разработки остальных компонентов, но
не для реальных CCTV.

## 3. Этап 1 — заморозка интеграционных контрактов

Работы:

1. Owners Perception/Clip/Retention/Site/Dashboard читают
   [`components.md`](components.md) и
   показывают реальные sample payloads.
2. Разногласия фиксируются в [`../QUESTIONS.md`](../QUESTIONS.md) и
   [`../PROPOSALS.md`](../PROPOSALS.md).
3. Выпускается `javes-boundary.v1` вместо `v1alpha1`; breaking изменения получают
   новую contract version и период dual-read/one-write.
4. Golden payloads становятся test fixtures и Schema публикуется как artifact.
5. Error taxonomy, deadlines, retry ownership и cancellation фиксируются.

Gate: каждый owner письменно подтверждает вход, выход, idempotency и error
semantics; Main Infra прогоняет его fixtures через Pydantic + acceptance.

## 4. Этап 2 — production foundation

Работы:

- production composition root и process entrypoints;
- PostgreSQL CI/staging, pool/timeouts/lease queries;
- постоянный private filesystem volume для `FileArtifactStore`;
- authenticated worker transport + DLQ;
- secrets/TLS/CORS/CSP/network policies;
- OpenTelemetry logs/metrics/traces и alerts;
- backup/restore и migration release job;
- Site Config immutable registry/publisher;
- fake adapters запрещены в production profile.

Gate: пункты deployment acceptance в
[`deployment.md`](deployment.md), включая concurrent claim,
crash replay, restore и rollback rehearsal.

## 5. Этап 3 — подключение real components

Рекомендуемый порядок минимизирует неопределённость:

1. **Site Config** — без корректных cameras/times/maps остальные результаты не
   интерпретируемы.
2. **Perception historical finite probes** — сначала bounded запросы без OPEN;
   подключение к существующему CCTV Archive находится внутри Perception.
3. **Clip cut/state/export** — после стабильных tracklet/source refs; доступ к
   внешнему CCTV Archive находится внутри Clip.
4. **Perception OPEN/live** — subscriptions, cancel, reconnect и proxy.
5. **Retention** — удаляет только product-owned ArtifactStore/gallery/outputs;
   внешний `archive_id` остаётся read-only.
6. **Dashboard** — может разрабатываться раньше на fake API, а на этом этапе
   проходит real browser staging acceptance.

Для каждого компонента: adapter -> contract suite -> shadow -> canary -> metrics
window -> production enable. Следующий компонент не обязан ждать полного UI,
если его boundary можно проверить API tests.

## 6. Этап 4 — pilot

Ограниченный site и камеры, реальные операторы, но с ручной проверкой каждого
critical result. Собираются:

- tracklet/candidate precision/recall на согласованной разметке;
- search success/time/camera-minutes;
- gate false rejects и long-gap review rate;
- upstream CCTV coverage и clock errors, возвращённые Perception/Clip;
- clip/export success;
- queue/worker latency и cost;
- operator review time/ошибки;
- deletion SLA и audit completeness.

До pilot обязательны privacy/legal review, incident runbook, named on-call,
data access audit и понятная маркировка algorithmic uncertainty.

## 7. Этап 5 — hardening из ROUND2

После базовой интеграции, а не вместо неё:

- overlapping camera groups и correlated misses;
- UNKNOWN/leak paths и richer outage semantics;
- elevators/escalators/rooms с measured dwell models;
- crowd penalties и early review;
- undo/revoke через evidence replay;
- manual witness sightings;
- empirical appearance/estimate calibration;
- profile/site-specific timing calibration;
- retention/security review;
- shadow run на размеченных данных;
- load/chaos/soak и horizontal scaling.

Каждая функция требует измеряемого success metric и regression dataset. Нельзя
объявлять probability calibrated только потому, что число находится в `[0,1]`.

## 8. Definition of Done по компонентам

### Routing

- все acceptance сценарии PDF и
  [`testing.md`](testing.md) зелёные;
- deterministic/pure/UTC/state round trip;
- graph/time/branches/misses/retries/OPEN/LONG_GAP/modes/stops корректны;
- SVG safe/deterministic/multi-floor/fallback;
- real-data shadow metrics согласованы и версии model/params/site pinned;
- latency/memory укладываются в SLO.

### Main Infra / Controls

- PostgreSQL multi-replica tests, outbox/crash/CAS/idempotency зелёные;
- API versioned, RBAC/no-signup/session security проверены;
- all mutations audited permanently; secrets are never written into audit
  payloads, and stored audit events are never redacted or deleted;
- worker callbacks authenticated/correlated;
- close/export/delete и retention barrier работают при сбоях;
- metrics/alerts/runbooks/backup restore/rollback готовы.

### Perception

- gallery/probe/live contracts v1;
- finite/empty/partial/error/OPEN/cancel/restart suites;
- stable IDs, UTC correction, bbox and source refs;
- calibrated score contract либо явная ranking-only маркировка;
- quality/cost/latency на representative dataset достигают согласованных порогов;
- no cross-case/gallery leak.

### Clip

- exact trims, supported codec corpus, atomic artifacts/checksums;
- idempotent cut/state/export и concurrent duplicates;
- HOLD/CONFIRMED/RELEASED state machine;
- ordered export, missing/unavailable/error behavior;
- source/archive auth и retention registration.

### Retention

- **готово в core:** явная policy для classes/TTL/legal hold, durable inventory,
  resumable/idempotent deletion, late-resource/partial-failure handling,
  multi-location receipts, tombstone/proof и Main case-TTL integration;
- **готово для local deployment:** Main ArtifactStore, allow-listed files и
  live-store adapter;
- **до подключения real workers:** подключить product-owned ArtifactStore и
  Perception/gallery/Clip-output backends; внешний CCTV Archive не удаляется;
  срок кейса уже зафиксирован в 14 календарных дней; audit хранится бессрочно.

### Внешний CCTV Video Archive

- не является компонентом или deliverable JAVES-AI;
- его API, доступность и исходная retention policy принадлежат существующей
  CCTV-системе;
- совместимость проверяют владельцы Perception и Clip на своей стороне;
- Main Infra и Retention не вызывают его и не удаляют `archive_id`.

### Site Config

- immutable versioned publish workflow;
- complete validation/assets/checksums/preview;
- old versions readable for active/history cases;
- atomic publish and cached pinned snapshot;
- camera clock ownership documented;
- operator runbook and rollback to prior version for new cases.

### Dashboard

- login/change-password, no signup, ROOT IAM UI;
- exact role visibility plus backend 403 checks;
- create/list/workspace/review/close/export/delete flows;
- shows Routing SVG without route geometry code;
- live TTL/error behavior;
- accessibility, CSP/XSS/CSRF/token handling, browser matrix;
- loading/offline/conflict/retry UX.

### Deployment

- immutable reproducible image/SBOM/scans;
- migrations and restore/rollback rehearsed;
- TLS/secrets/network/default deny;
- PostgreSQL/shared filesystem/transport production-ready;
- no fake enabled in production;
- SLO dashboards/alerts/on-call/incident and DR runbooks;
- synthetic full lifecycle passes after every deployment.

## 9. Полный продуктовый DoD

JAVES-AI можно считать полностью готовым только когда одновременно выполнено:

1. Все component DoD выше.
2. Командная PDF и versioned contracts синхронизированы.
3. End-to-end real path проходит без ручного вмешательства в инфраструктуру:
   login -> create -> site pin -> gallery -> finite/OPEN search -> review ->
   route SVG -> clips -> close -> ordered export -> policy retention/delete.
4. Любой процесс можно перезапустить в каждой критической точке без duplicate
   business effect, lost state или resurrection deleted case.
5. Security/privacy/legal review завершены; данные и доступы аудируемы.
6. Quality, latency, availability, cost, RPO/RTO и deletion SLA измерены и
   соответствуют заранее утверждённым числам.
7. Реальный annotated-data benchmark и pilot sign-off получены.
8. Backup restore, incident response и rollback фактически отрепетированы.
9. Документация, schemas, runbooks и source code относятся к одной release
   version и доступны владельцам компонентов.

До выполнения этих пунктов корректная формулировка статуса — «integration MVP»
или «pilot», а не «готовый production продукт».

