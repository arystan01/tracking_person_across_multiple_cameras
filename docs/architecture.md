# JAVES-AI: фактическая архитектура и код

Этот документ описывает состояние репозитория, а не желаемую картинку. Всё,
что помечено **fake**, выполняет контракт и позволяет тестировать оркестрацию,
но не обрабатывает реальное видео, не удаляет реальные объекты и не заменяет
production-инфраструктуру.

## 1. Граница текущей реализации

Реализованы:

- чистое ядро Routing: гипотезы, scoring, probes, режимы, stop conditions;
- детерминированная SVG-отрисовка маршрута по этажам;
- версионированные Python/JSON-контракты;
- framework-independent Case Manager;
- HTTP Controls API на FastAPI;
- закрытый IAM с заранее создаваемыми аккаунтами и RBAC;
- in-memory и SQLAlchemy persistence, Alembic migrations;
- durable jobs, Routing triggers и transactional outbox;
- artifact abstraction и локальное атомарное файловое хранилище;
- fake Perception, Clip, Retention, внешний CCTV Archive, Site Config и
  Dashboard requests, плюс отдельный real Retention core и real Perception
  worker (`javes_perception/`: YOLO26 + ByteTrack + OSNet);
- unit, integration, HTTP, persistence, restart, concurrency и acceptance tests.

Не реализованы как production-компоненты:

- настоящее чтение CCTV и Re-ID в Perception;
- ffmpeg/media pipeline в Clip;
- интеграционные handlers удаления из Perception/Clip storage (локальный
  `FileArtifactStore` и durable Retention core уже реализованы);
- адаптеры Perception/Clip к существующей внешней CCTV-системе;
- versioned Site Config service и UI публикации конфигураций;
- браузерный Dashboard;
- production process assembly, broker transport, постоянный файловый volume,
  observability, backup/restore, TLS и deployment manifests.

## 2. Почему это модульный монолит

Сейчас orchestration, API, persistence и локальный Routing находятся в одном
Python deployment unit, но зависят от `Protocol`-портов. Это сохраняет
транзакционную целостность кейса и ускоряет изменение ещё не замороженных
контрактов. Perception, Clip, Retention и Site Config остаются заменяемыми
границами; доступ к внешнему CCTV Archive инкапсулирован внутри Perception/Clip.
После измерения нагрузки конкретный adapter можно вынести в отдельный сервис без
переписывания доменной логики.

Микросервисы сейчас добавили бы distributed transactions, broker semantics,
service discovery и наблюдаемость до того, как известна реальная нагрузка. Это
не запрет на последующее разделение: outbox, immutable commands, stable IDs и
versioned DTO уже создают необходимый шов.

## 3. Структура репозитория

| Путь | Назначение |
|---|---|
| `routing/contracts/` | Routing-owned модели: probes, results, graph, state, hypotheses, estimate, rendered route |
| `routing/site.py` | строгая загрузка и валидация site YAML |
| `routing/timemodel.py` | lognormal time/dwell likelihood и arrival mass |
| `routing/update.py` | normalisation observations, visits, hypothesis branching/scoring/anchors |
| `routing/expand.py` | следующие finite/OPEN probes и retry lifecycle |
| `routing/predict.py` | estimate, modes, dormant/ended, stop reasons |
| `routing/step.py` | `init_state()` и чистый round function `step()` |
| `routing/render.py` | чистая безопасная генерация self-contained SVG |
| `routing/worker.py` | `step_and_render()` — локальный worker boundary |
| `routing/sim/` | независимый seeded simulator и benchmark |
| `javes_contracts/` | стабильный импорт DTO, JSON Schema и cross-message acceptance checks |
| `javes_main/domain/` | lifecycle/status models без HTTP/DB кода |
| `javes_main/application/` | Case Manager, IAM, metrics и CLI bootstrap root |
| `javes_main/ports/` | Protocol-порты persistence/workers/storage/config/clock |
| `javes_main/adapters/` | локальный Routing, fake workers, memory/file storage, in-memory Site Config |
| `javes_main/persistence/` | memory repo, SQLAlchemy repo, Alembic migrations |
| `javes_main/scheduler/` | job polling, Routing trigger leases, outbox delivery |
| `javes_main/api/` | FastAPI Controls и worker callback endpoints |
| `javes_retention/` | policy, durable inventory/journal, deletion routing/backends и worker |
| `javes_fakes/` | детерминированные contract doubles; не production реализация |
| `tests/` | executable specification текущего поведения |
| `examples/` | канонические JSON/YAML payloads |

## 4. Архитектура выполнения

```text
Dashboard ──HTTPS──> Controls API ──> Case Manager ──> Repository/Postgres
                          │                 │                 │
                          │                 ├── durable jobs ─┼──> Perception
                          │                 ├── triggers ─────┼──> Routing local
                          │                 └── outbox ───────┼──> Clip/Retention
                          │
                          ├── private SVG/artifact response <── Artifact Store
                          └── private live proxy <────────────── Perception stream

Perception/Clip ──> Video Archive
Routing renderer <── pinned SiteGraph + floor bytes
```

Dashboard никогда не вызывает worker напрямую. Routing никогда не читает БД,
файлы, сеть или текущее системное время. Case Manager связывает доменную
операцию с durable records; внешняя доставка происходит после транзакции.

## 5. Полный жизненный цикл кейса

### 5.1 Создание и подготовка

1. Авторизованный `OPERATOR`, `ADMIN` или `ROOT` отправляет `POST /v1/cases`
   с `Idempotency-Key`.
2. Case Manager получает строго указанную пару `site_id/site_version` и текущую
   версию `RoutingParams`, затем закрепляет обе версии в кейсе.
3. Routing создаёт начальный `SearchState` из `last_known`.
4. В одной транзакции создаются `CaseRecord(PREPARING)`, audit event,
   idempotency record и outbox `TARGET_GALLERY`.
5. Perception возвращает `TargetGalleryResult`; только после корреляции
   `command_id/case_id/model_version` кейс переходит в `SEARCHING`.
6. Создаётся durable Routing trigger `GALLERY_READY`.

### 5.2 Поисковый round

1. Routing dispatcher арендует trigger.
2. Case Manager вне длинной DB-транзакции вызывает локальный чистый Routing с
   pinned state/graph/params, новыми results/anchors и явным `now`.
3. Routing возвращает новое состояние, probes, hypotheses, estimate, mode,
   done/stop reason и готовые SVG.
4. Case Manager CAS-сохраняет кейс по `expected_revision`, создаёт jobs для
   probes, route artifacts, case-data audit records, retention registrations и
   Clip HOLD commands.
5. Конкурирующий устаревший результат получает revision conflict и безопасно
   повторяется через durable trigger; одинаковый trigger не применяется дважды.

### 5.3 Perception job

1. Scheduler арендует `PENDING` job, выставляя owner и lease deadline.
2. Adapter отправляет `PerceptionProbeJob` реальному или fake worker.
3. Worker возвращает `ProbeResult` через внутренний callback.
4. Main Infra хранит неизменённый raw result для аудита, но в Routing передаёт
   sanitised evidence: только совпадающие case/probe/camera, уникальные tracklets
   и candidates, ссылающиеся на tracklet того же результата.
5. Duplicate payload становится idempotent no-op. Другой ответ на уже
   завершённый finite job отклоняется. Для OPEN допускаются повторные
   монотонные updates до `stream_closed=true`.
6. Accepted result создаёт Routing trigger. Actual `processed_duration_s`, а не
   длина запрошенного окна, входит в camera-minute budget.

### 5.4 Review и clips

1. Candidates попадают в `CaseView`; Dashboard показывает crop/score как
   предложение оператору, не как доказанную личность.
2. Для кандидата в surviving hypothesis Case Manager ставит `CLIP_CUT` с
   lifecycle `HOLD`, если Perception дал стабильный `clip_ref`.
3. Решение `CONFIRM`/`REJECT` создаёт Routing anchor и соответственно
   `CONFIRM`/`RELEASE` для Clip. Раннее решение запоминается и применяется после
   позднего завершения cut.
4. Противоречивое повторное решение запрещено: полноценный undo требует
   evidence replay и относится к hardening.

### 5.5 Close, export и delete — разные операции

- `CLOSE`: останавливает Routing, отменяет active/OPEN Perception jobs, очищает
  live pointer и отправляет `CLOSE_CASE` в Retention. Результаты кейса остаются.
- `EXPORT`: разрешён для закрытого/сохранённого кейса, берёт только CONFIRMED
  clips в порядке visits лучшей гипотезы и отправляет `CLIP_EXPORT`.
- `DELETE`: доступен только роли с `case.delete`, сначала ставит
  `DELETE_REQUESTED`, отменяет работу и через causal outbox запускает
  `DELETE_CASE`. После подтверждения содержимое кейса purged/redacted, а
  полный append-only audit (кто, когда, что сделал) остаётся бессрочно.

Retention command не обгоняет ещё не доставленные clip/register команды того же
кейса. Outbox делает доставку at-least-once, поэтому каждый внешний worker обязан
быть idempotent по command/request ID.

## 6. Routing: данные и алгоритм

### 6.1 Вход и выход

```python
state = init_state(case_info, site_graph, params)
step_result = step(state, probe_results, site_graph, anchors, params, now)
rendered = render_route_maps(step_result, site_graph, floor_assets)
```

`SearchState` сериализуем и непрозрачен для других компонентов. `step()` не
изменяет аргументы. Время timezone-aware, нормализуется в UTC. `"OPEN"` — wire
значение; внутри Python оно представлено `None`.

### 6.2 Алгоритмический round

1. Result IDs дедуплицируются, ошибки/unknown cameras безопасно пропускаются.
2. Tracklets одной камеры объединяются в visit только при допустимом time gap,
   пространственной близости и candidate association.
3. Entry/exit regions ограничивают допустимые edges; отсутствие region hit
   оставляет все edges камеры.
4. Visit ветвится во все совместимые hypotheses. Hard gate запрещает движение
   быстрее `t_min`; опоздание штрафует log-likelihood, но не отвергает путь.
5. Clipped boundary оставляет hard gate и снимает только lateness penalty.
6. Appearance, transition и covered miss terms суммируются как score; score не
   объявляется калиброванной вероятностью.
7. LONG_GAP/re-entry создаётся с `requires_confirmation` и не становится
   главным маршрутом до operator confirm.
8. Эквивалентные visit sequences объединяются, hidden edge paths сохраняются,
   затем остаются top `B` hypotheses.
9. Expand создаёт top `K` camera/time probes, ambiguity probes, tail probes и
   OPEN watch probes для blind/OUTSIDE.
10. Predict вычисляет относительные zone weights, mode и stop condition.

### 6.3 Режимы и stop priority

- `CATCHING_UP`: есть исторические непроверенные окна.
- `LIVE_TRACKING`: лучшая подтверждённая гипотеза содержит open tracklet.
- `WAITING`: best dormant или в blind/OUTSIDE, работают OPEN watches.
- `PAST_SUMMARY`: поиск завершён либо маршрут полностью в прошлом.

Приоритет остановки: operator close, budget, max gap, all ended. FAILED,
TIMEOUT и UNAVAILABLE не считаются miss; после `max_retries` окно становится
`unobserved`. Только DONE-empty с реальным покрытием штрафует ожидавший путь.

### 6.4 Отрисовка

Routing строит standalone SVG для каждого этажа. План этажа встраивается data
URI; лучший маршрут solid, альтернативы/approximate/gaps dashed, камеры и
estimate имеют отдельные markers. Для отсутствующего плана создаётся abstract
graph. Нет scripts, event handlers и external image links; labels XML-escaped.

Dashboard получает URL на уже построенный SVG и не вычисляет path. Если
Dashboard будет сам рисовать маршрут, появятся две расходящиеся реализации и
это будет нарушением архитектуры.

## 7. Main Infra domain и надёжность

### 7.1 State machines

Case:

```text
PREPARING -> SEARCHING -> CLOSED -> EXPORTING -> SAVED
      \          \          \          \
       \----------+----------+-----------> DELETE_REQUESTED -> DELETED
                                        -> FAILED (terminal operational failure)
```

Jobs: `PENDING -> LEASED -> DONE|FAILED|TIMEOUT|UNAVAILABLE|CANCELLED`.
Expired lease возвращает незавершённую работу. OPEN job остаётся активным до
explicit close/cancel.

Outbox: `PENDING -> LEASED -> DELIVERED`, либо retry до `FAILED`; delete может
`CANCELLED`-пометить сообщения, которые больше нельзя доставлять.

Routing trigger: `PENDING -> LEASED -> DONE`, с retry/FAILED и CAS защиты кейса.

### 7.2 Persistence

`InMemoryRepository` предназначен для tests/demo. `SqlAlchemyRepository`
сохраняет cases, jobs, audit events, idempotency, users, sessions, outbox, raw
probe results, immutable case-data и Routing triggers. Alembic migrations
`0001..0006` создают эти структуры. Production target — PostgreSQL; SQLite
достаточен только для локальной проверки.

`case_events` и IAM events — постоянный append-only security audit. Операции
case DELETE/TTL очищают media/search content, но не изменяют actor, time, action
или details audit event. Обычные diagnostic logs не являются источником аудита.

Durability правила:

- внешние side effects создаются через transactional outbox;
- любой command имеет стабильный dedupe ID;
- HTTP mutation имеет `Idempotency-Key` и fingerprint payload;
- state update имеет optimistic revision check;
- worker leases имеют owner/expiry;
- raw result и derived state разделены;
- route/artifact keys immutable по content.

## 8. Controls API и IAM

Нет публичной регистрации. ROOT создаётся один раз CLI-командой, затем ROOT
создаёт заранее авторизованные аккаунты через admin API. Логин Dashboard —
username/password, ответ — opaque bearer session. Пароли хэшируются Argon2id;
есть forced password change, lockout, expiry, logout/session revocation и audit.
MFA сейчас не заявлено.

Роли:

| Роль | Возможности |
|---|---|
| `ROOT` | все разрешения, создание/блокировка users, роли, reset, delete |
| `ADMIN` | операции кейса, включая delete и audit, но не управление IAM |
| `OPERATOR` | create/read/review/close/export, без delete |
| `VIEWER` | read-only |
| `SERVICE` | служебная identity; worker callback дополнительно защищается worker token |

Основные endpoints:

| Метод и путь | Назначение |
|---|---|
| `POST /v1/auth/login` | login/password -> bearer session |
| `GET /v1/auth/me` | текущая роль/permissions |
| `POST /v1/auth/change-password` | смена собственного пароля |
| `/v1/admin/users...` | ROOT-only IAM lifecycle |
| `POST /v1/cases` | создать кейс |
| `GET /v1/cases` | список кейсов |
| `GET /v1/cases/{id}/view` | Dashboard projection |
| `POST /v1/cases/{id}/decisions` | CONFIRM/REJECT |
| `POST /v1/cases/{id}/commands/{close|export|delete}` | разные lifecycle команды |
| `GET /v1/cases/{id}/routes/{rev}/{floor}.svg` | приватный Routing SVG |
| `GET /v1/cases/{id}/live` | приватный proxy live bytes |
| `POST /internal/v1/jobs/{job}/results` | callback Perception |
| `POST /internal/v1/cases/{id}/live` | metadata callback Perception |

Точные request/response models находятся в `javes_contracts` и
`javes_main/api/auth.py`; не следует переписывать их вручную по этому тексту.

### 8.1 Каталог публичных данных

| Модель | Producer -> Consumer | Обязательный смысл |
|---|---|---|
| `CreateCaseBody` | Dashboard -> Controls | case/operator/target, reference ref, exact last-known, bounded window, profile, pinned site/version |
| `TargetGalleryCommand/Result` | Main -> Perception -> Main | idempotent создание/обновление target gallery |
| `ProbeRequest` | Routing -> Main -> Perception | одна camera, UTC window или OPEN, reason, hypothesis IDs, retry lineage |
| `PerceptionProbeJob` | Main -> Perception | job/case/gallery/deadline + ProbeRequest |
| `Tracklet` | Perception -> Routing/Clip | stable ID, case/probe/camera, interval, source-frame bboxes, clipping/open flags, artifact refs |
| `Candidate` | Perception -> Routing/Review | stable optional ID, same-result tracklet, appearance score, cosine/calibration/association metadata |
| `ProbeResult` | Perception -> Main -> Routing | status, evidence, coverage, actual cumulative seconds, total people, explicit stream close/error |
| `SearchState` | Routing <-> Case Store | opaque deterministic state with pinned versions, hypotheses, probes, budgets and dedupe caches |
| `StepResult` | Routing -> Main | state, next probes, hypotheses/best, ambiguities/rejections, estimate, mode/done/stop |
| `RenderedRouteBundle` | Routing -> Main | route revision + standalone SVG per floor |
| `LiveFeedUpdate` | Perception -> Main | expiring metadata and opaque stream ref; never a route input |
| `CutClipCommand/ClipRecord` | Main <-> Clip | materialize tracklet interval, processing status and HOLD/CONFIRMED/RELEASED lifecycle |
| `ExportClipsCommand/Result` | Main <-> Clip | immutable ordered confirmed clip list -> one export artifact |
| `RetentionResource` | Main -> Retention | resource inventory/class/TTL/legal hold |
| `RetentionCommand/Result` | Main <-> Retention | distinct CLOSE/DELETE/TTL command and deletion/preservation proof |
| `VideoSegmentRequest/Result` | Perception/Clip <-> Archive | camera window -> full/partial/unavailable stable source |
| `SiteGraph` | Site Config -> Routing | immutable topology, timings, maps, regions, blind/outside metadata |
| `ReviewDecisionBody` | Dashboard -> Controls | exact candidate/tracklet and CONFIRM/REJECT with actor/time |
| `CaseCommandBody` | Dashboard -> Controls | separate CLOSE/EXPORT/DELETE with actor/time |
| `CaseView` | Controls -> Dashboard | projection only: lifecycle, mode, best/alternatives, candidates, estimate, SVG URLs, clips/export/live/errors |

Wire field names всегда canonical: `camera_id`, `tracklet_id`, `t_start`,
`t_end`, `appearance_score`; legacy Python aliases принимаются только для
совместимости. Unknown fields запрещены. `SearchState` и raw hypothesis details
не являются API Dashboard.

### 8.2 Persistent records

Миграции создают:

- `cases` — целый versioned `CaseRecord` и revision/lifecycle indexes;
- `jobs` — Perception payload/state/attempt/lease;
- `case_events` — append-only operational/IAM audit;
- `idempotency` — key, request fingerprint и replay response;
- `users`, `auth_sessions` — закрытый IAM;
- `outbox` — durable external commands;
- `probe_results` — immutable raw worker deliveries;
- `case_data` — immutable route revisions, states, requests, evidence,
  decisions и artifact metadata;
- `routing_triggers` — durable reasons для следующего pure Routing round.

В текущей SQL реализации некоторые complex aggregates хранятся JSON. Это
осознанный MVP trade-off: versioned opaque state остаётся целым, а поля,
нужные для claims/lifecycle, индексируются отдельно. Перед большим масштабом
нужно измерить query patterns, а не заранее раскладывать каждую hypothesis в
реляционные таблицы.

### 8.3 Failure ownership

| Сбой | Кто фиксирует | Результат |
|---|---|---|
| invalid public request | Controls/Pydantic | 4xx, без durable side effect |
| duplicate HTTP mutation | idempotency store | прежний response или conflict при другом payload |
| worker unavailable/timeout | job/scheduler | terminal result для round, Routing retry/unobserved semantics |
| malformed/uncorrelated evidence | Main boundary | raw хранится, unsafe evidence пропускается и логируется |
| Routing compute exception | Routing trigger | retry до FAILED; case не получает partial state |
| optimistic revision conflict | repository/dispatcher | trigger повторяется на свежем state |
| external Clip/Retention error | outbox | retry/backoff, затем visible FAILED |
| process crash after worker side effect | outbox + worker idempotency | повторная доставка даёт тот же semantic result |
| artifact key collision | ArtifactStore | fail closed, существующие bytes не перезаписываются |
| case delete race | lifecycle + causal outbox | новые results не воскрешают case; content purged/redacted |

## 9. Fake stack: что он доказывает и чего не доказывает

Fake workers доказывают, что orchestration, IDs, retries, state transitions,
outbox, projection и контракты согласованы. Они детерминированы и имеют
failure/empty/partial/OPEN сценарии.

Они не доказывают:

- качество detection/tracking/Re-ID;
- корректность реальных CCTV timestamps и codecs;
- производительность GPU/ffmpeg;
- доступность архива и live transport;
- физическое удаление реплик/backup;
- browser UX/accessibility;
- PostgreSQL/shared-filesystem/broker работу под production нагрузкой.

Именно поэтому real adapter сначала проходит `javes_contracts.acceptance`,
tests из [`testing.md`](testing.md) и shadow
environment, и только потом заменяет fake.

## 10. Важные архитектурные поправки

По сравнению с первоначальными набросками закреплено:

- маршрут и его SVG рисует Routing; Dashboard только отображает;
- Case Manager, а не Routing, владеет jobs/persistence/network;
- `appearance_score` без calibration version — ranking signal, не вероятность;
- camera-minute budget считается по фактически обработанным секундам;
- отдельного cloud/object storage нет: `object_key` — относительный путь в
  локальном `FileArtifactStore`;
- внешний CCTV Archive является read-only зависимостью Perception/Clip и не
  входит в JAVES-AI Retention;
- полный security audit хранится бессрочно, включая actor/time/action/details;
- case content TTL равен 14 календарным дням после close;
- SiteGraph/params/calibration должны быть version-pinned;
- clock offsets применяются upstream один раз;
- close, export/save и delete — разные команды;
- retention — policy и проверяемый workflow, не бессрочное хранение;
- OPEN — subscription/upsert lifecycle, а не бесконечный обычный request;
- at-least-once доставка требует idempotent worker API;
- SVG и live feeds приватны и проверяются тем же RBAC, что CaseView.

Эти изменения не ломают продуктовую идею PDF. Они делают границы компонентов
однозначными и устраняют предположения, которые были небезопасны для реальной
эксплуатации.

