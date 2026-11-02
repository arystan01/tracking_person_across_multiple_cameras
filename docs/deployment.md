# JAVES-AI deployment runbook

## 1. Честный статус

Репозиторий можно устанавливать wheel-ом, мигрировать SQLite/PostgreSQL,
запускать tests и полный fake HTTP demo. Durable Retention core и local storage
deletion реализованы, но систему пока нельзя объявить production deployment:
отсутствуют production adapters/transport для Perception, Clip и Site Config,
удаление из product-owned Perception/Clip storage, а также готовый process
factory, manifests и monitoring integration. Внешний CCTV Video Archive не
является deployment unit JAVES-AI и не управляется Retention.

```bash
python -m pip install -e '.[test,infra]'
python -m pytest -q
javes-main-demo
```

Fake demo — локальная проверка orchestration, не способ запуска с CCTV.

## 2. Целевая topology

Минимальный production deployment:

```text
TLS ingress
  ├── Dashboard static/BFF
  └── Main API replicas
        ├── PostgreSQL (system of record)
        ├── private persistent filesystem (JAVES artifact directory)
        ├── Scheduler replicas (jobs, Routing triggers, outbox)
        ├── Site Config client/service
        ├── Perception command/callback transport
        ├── Clip command/result transport
        └── Retention command/result transport

Perception + Clip ──> Video Archive
All services ──> central logs/metrics/traces/secrets
```

В малом пилоте Main API и Scheduler могут быть двумя процессами одного image.
Routing остаётся библиотекой внутри scheduler. GPU Perception и media Clip
развёртываются отдельно. Отдельного cloud/object storage нет. Один deployment
использует постоянный `JAVES_ARTIFACT_ROOT`; при нескольких Main replicas этот
каталог должен быть одним и тем же mounted/shared filesystem для всех replicas.
SQLite подходит только локальному single-process режиму.

## 3. Build и supply chain

1. Build выполняется из clean checkout на Python 3.11–3.13.
2. Создаётся wheel, а не копируется working directory:

   ```bash
   python -m pip wheel --no-deps -w dist .
   ```

3. В CI прогоняются команды из [`testing.md`](testing.md), dependency/SBOM и image scan.
4. Image запускается non-root, имеет read-only root filesystem и отдельный
   writable volume/tmp только там, где необходимо.
5. Release идентифицируется image digest + application version + migration
   revision + contracts version.
6. Один и тот же immutable image продвигается dev -> staging -> production.

Не включать в image `.git`, caches, local sqlite, demo output, source video,
reference images, secrets и временные artifacts. `.gitignore` уже исключает
обычные generated paths; container ignore должен повторять эту policy.

## 4. Production assembly, который ещё нужно добавить

Нужен composition root, например `javes_main/production.py`, который:

1. читает `MainSettings.from_env()` и отдельные typed настройки worker URLs,
   TLS, persistent artifact root и Site Config;
2. создаёт SQLAlchemy engine/repository;
3. создаёт `FileArtifactStore` из `JAVES_ARTIFACT_ROOT` и LiveFeedPort;
4. создаёт HTTP/queue adapters, реализующие `PerceptionPort`, `ClipPort`,
   `SiteConfigPort`, и собирает `RetentionWorker` с backend для каждого реально
   используемого storage locator;
5. вызывает `assemble_system(...)`;
6. создаёт FastAPI через `create_app(...)`;
7. не включает `run_fake_jobs_inline`;
8. предоставляет отдельный scheduler entrypoint и graceful shutdown.

До появления этих adapters нельзя маскировать fake как production через env
flag. При старте production profile должен fail closed, если хотя бы один
обязательный endpoint/secret/storage отсутствует.

## 5. Конфигурация

Уже поддерживаемые Main Infra переменные:

| Variable | Смысл |
|---|---|
| `JAVES_DATABASE_URL` | SQLAlchemy URL; production — PostgreSQL |
| `JAVES_ARTIFACT_ROOT` | постоянный private каталог `FileArtifactStore` во всех окружениях |
| `JAVES_WORKER_TOKEN` | shared callback token только до перехода на mTLS/service identity |
| `JAVES_SCHEDULER_POLL_S` | период bounded scheduler loop |
| `JAVES_JOB_LEASE_S` | lease Perception job |
| `JAVES_OUTBOX_LEASE_S` | lease outbox delivery |
| `JAVES_SESSION_TTL_S` | TTL пользовательской сессии |

Production composition должен добавить typed settings как минимум для:

```text
JAVES_ENV=production
JAVES_PUBLIC_BASE_URL
JAVES_ALLOWED_ORIGINS
JAVES_TRUSTED_PROXY_CIDRS
JAVES_SITE_CONFIG_URL / credential
JAVES_PERCEPTION_URL|queue / credential
JAVES_CLIP_URL|queue / credential
JAVES_RETENTION_URL|queue / credential
JAVES_OTEL_ENDPOINT
JAVES_LOG_LEVEL
```

Секреты поступают из secret manager/workload identity, не из image, Git или
ConfigMap. Значения не печатаются при старте. Worker token регулярно
ротируется; preferred target — mTLS или подписанные service credentials.

## 6. Database

### 6.1 Provision

- отдельная DB/user с минимальными permissions;
- TLS required;
- UTC connection/session timezone;
- managed backups + point-in-time recovery;
- connection pool ограничен числом replicas;
- statement/lock/idle transaction timeouts;
- migrations выполняет один release job, а не каждая API replica.

### 6.2 Migration

```bash
javes-main-migrate --database-url \
  'postgresql+psycopg://javes:<secret>@db.internal/javes' upgrade
```

Перед production нужны PostgreSQL driver и CI lane на реальном PostgreSQL.
Проверить текущую revision, backup/restore и forward compatibility до rollout.
Migration policy: additive schema -> compatible app rollout -> backfill ->
удаление старого только в следующем release. Alembic downgrade не считается
основной recovery strategy; rollback обычно app rollback при совместимой БД.

### 6.3 Concurrency gate

В staging обязателен multi-process test:

- два scheduler одновременно claim jobs/outbox/triggers;
- одна запись арендуется одним owner;
- expired lease возвращается;
- parallel case updates дают CAS conflict, не lost update;
- одинаковый Idempotency-Key выполняет side effect один раз;
- delete не обгоняет clip/register outbox того же case.

SQLite не доказывает эти свойства PostgreSQL.

## 7. Bootstrap IAM

После миграций и до открытия ingress выполняется один раз:

```bash
javes-main-bootstrap-root \
  --database-url 'postgresql+psycopg://javes:<secret>@db.internal/javes' \
  --username root
```

Пароль вводится через защищённый interactive secret channel, не как shell
argument/history. Bootstrap fail-ит, если пользователи уже существуют. ROOT
входит в Dashboard, создаёт персональные аккаунты и меняет временные пароли.
Публичного signup endpoint нет.

Day-1 security checklist:

- TLS/HSTS, private admin/internal routes;
- rate limit login и callbacks;
- Argon2 memory/cpu параметры проверены на целевом hardware;
- session/token не логируется;
- CORS allowlist, CSP, frame-ancestors, secure cookies/BFF design;
- default-deny network policies;
- ROOT break-glass procedure и audit export;
- MFA честно помечено deferred, а не «эмулируется».

## 8. Локальное artifact storage

Отдельные облачные хранилища не планируются. Во всех окружениях используется
`FileArtifactStore` с persistent `JAVES_ARTIFACT_ROOT` на локальном или
подключённом private filesystem:

- `object_key` — логический относительный путь `cases/{case_id}/...`, а не cloud ID;
- запись атомарна: временный файл + rename, существующий контент не перезаписывается;
- запрещены path traversal, symlinks наружу и публичный доступ к каталогу;
- checksum и MIME metadata проверяются приложением;
- bytes выдаются только через авторизованный Main API;
- Retention физически удаляет файл через `FileArtifactStore.delete()`;
- файловый backup не должен продлевать доступность case content сверх принятой
  политики без отдельного решения.

Reference image upload требует malware/type/size validation и отдельной
authorization операции. SVG отдаётся как `image/svg+xml` с CSP, `nosniff` и
private cache headers. Routing SVG self-contained и без script/external URLs,
но хранение всё равно private.

## 9. Site Config deployment

Для MVP достаточно versioned каталога Site Config на private filesystem. Для
каждой published version хранятся manifest, assets, checksums и metadata.

Release site version:

1. оператор конфигурации создаёт draft;
2. CI/service запускает `load_site`, `check_site_snapshot`, render smoke и
   component tests из [`components.md`](components.md);
3. floor assets загружаются под content-addressed keys;
4. manifest получает final version и checksums;
5. snapshot атомарно помечается published;
6. `get_graph/get_floor_assets` читают только published snapshot;
7. synthetic case проверяет route rendering;
8. только затем version появляется в create-case selector.

Не обновлять map file по прежнему path без новой version. При недоступности Site
Config новый case не создаётся; активный case должен иметь доступ к cached,
checksum-verified pinned snapshot.

## 10. Worker transport

HTTP и broker допустимы, если adapter сохраняет одинаковую семантику:

- command envelope: contract version, message ID, case correlation, deadline,
  trace ID;
- explicit ack только после durable acceptance;
- at-least-once delivery, bounded retry, DLQ;
- result callback authenticated and replay-safe;
- payload size limit; video передаётся ref/stream, не в JSON;
- worker result проверяется Schema + acceptance до применения;
- cancellation best-effort, но late result остаётся auditable и не воскресит
  deleted/closed state.

Для OPEN лучше отдельный subscription lease/heartbeat protocol, а не бесконечный
HTTP request. Main Infra должен уметь cancel и считать cumulative duration без
двойного списания.

## 11. API и Scheduler processes

API process:

- stateless кроме DB/storage;
- health live проверяет process, ready — DB/migrations/critical config;
- не выполняет fake inline jobs;
- graceful drain перестаёт принимать mutations до shutdown.

Scheduler process loop:

```text
while running:
  reclaim/claim jobs
  poll active OPEN subscriptions
  enqueue due heartbeats
  drain bounded Routing triggers
  drain bounded outbox
  emit metrics
  wait scheduler_poll_s with jitter
```

Каждая итерация bounded; shutdown прекращает claims, завершает текущую короткую
операцию или оставляет lease для reclaim. Несколько replicas допустимы только
после PostgreSQL concurrency gate.

## 12. Observability

Минимальные metrics:

- HTTP rate/latency/status по route без case/username labels;
- cases по lifecycle/mode;
- jobs/outbox/triggers pending/leased/failed/oldest age/retries;
- Routing round latency, hypotheses, probes, camera-seconds, unobserved windows;
- Perception coverage/status/processing lag;
- Clip cut/export latency/failure;
- Retention deletion lag/failure/preserved legal holds;
- archive availability ratio;
- DB pool/transaction conflicts;
- auth success/failure/lockout без паролей/token.

Trace связывает request -> case command -> outbox/job -> result -> Routing
trigger через correlation IDs. Обычные diagnostic logs структурированы и не
содержат sensitive payloads/refs; они не заменяют постоянный append-only
security audit в `case_events`/IAM events. Audit хранит actor/time/action/details
бессрочно и не меняется при case DELETE/TTL.
Alert минимум на queue age, DLQ/FAILED, repeated CAS conflicts, DB/storage
unavailable, deletion SLA breach и login anomaly.

## 13. Backup, disaster recovery и deletion

- регулярно проверять restore PostgreSQL в изолированное окружение;
- versioned site snapshots резервировать отдельно;
- восстановление artifact-файлов проверять по checksums;
- определить RPO/RTO до пилота;
- encryption keys и credentials иметь documented recovery;
- delete policy явно описывает backup tombstone/expiry: нельзя обещать мгновенное
  стирание immutable backup, если платформа этого не делает;
- legal hold override документируется и аудитируется.

Quarterly restore drill должен восстановить case state + artifacts + exact
site/params version и воспроизвести CaseView.

## 14. Rollout и rollback

Порядок выпуска:

1. backup и migration dry-run;
2. contract schemas published;
3. adapters deploy dark/shadow;
4. migration job;
5. scheduler canary (одна replica, claims limited);
6. API canary;
7. synthetic case end-to-end;
8. один pilot site/camera group;
9. metrics observation window;
10. gradual expansion.

Rollback: остановить новые claims, отключить canary adapter, вернуть совместимый
application image, не откатывать destructive migration автоматически. Уже
принятые durable messages либо завершаются старым совместимым consumer, либо
явно переводятся в operator-visible FAILED/DLQ. Нельзя очищать queues вслепую.

## 15. Deployment acceptance

Production gate проходит только если:

- clean wheel/image и полный test matrix зелёные;
- migrations/rollback compatibility проверены на clone production DB;
- ROOT bootstrap и no-signup подтверждены;
- real component contract suites зелёные;
- PostgreSQL multi-replica claim/CAS test зелёный;
- artifacts private и deletion proof проверен;
- Site Config immutable version опубликована и pinned;
- TLS/CSP/CORS/network policies/secrets scan зелёные;
- dashboards/alerts/runbooks/on-call определены;
- backup restore drill пройден;
- synthetic create -> search -> review -> close -> export -> delete выполнен;
- rollback rehearsal выполнен.

