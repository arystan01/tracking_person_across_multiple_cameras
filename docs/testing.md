# JAVES-AI testing strategy

## 1. Быстрый запуск

```bash
python -m pip install -e '.[test,infra]'
python -m pytest -q
```

Тесты не требуют сети, GPU, ffmpeg, PostgreSQL или реального видео. Они
детерминированы и используют temporary directories, in-memory adapters и SQLite
там, где проверяется SQL persistence.

## 2. Что покрывает текущий набор

| Файл | Область |
|---|---|
| `tests/test_timemodel.py` | hard gate, peak-normalized lognormal, lateness, blind/outside dwell |
| `tests/test_site.py` | YAML defaults/validation, regions, probabilities, maps, paths, multi-floor rules |
| `tests/test_algorithm.py` | 10 исходных Routing сценариев: gate, ring/chord, misses, late, re-entry, anchors, modes/stops, baseline, purity, efficiency |
| `tests/test_b1_score.py` | appearance score formula |
| `tests/test_b2_b3_b6.py` | profiles, clock tolerance, bbox convention, dormant/idempotent behavior |
| `tests/test_mvp_contracts.py` | canonical aliases, aware UTC, start anchors, retries, OPEN, LONG_GAP, branching, SVG safety/determinism |
| `tests/test_contract_package.py` | deterministic JSON Schema и version rejection |
| `tests/test_adapter_acceptance.py` | reusable cross-message checks against fake implementations |
| `tests/test_perception_worker.py` | real Perception worker на подставных моделях: ранжирование, стабильные ID, DONE-empty, partial coverage, ошибки, OPEN/cancel, галерея после рестарта |
| `tests/test_integration_fakes.py` | Perception/Clip/Retention/Archive/Dashboard fake semantics |
| `tests/test_auth.py` | root bootstrap, users, passwords, roles, lockout/session behavior |
| `tests/test_main_infra.py` | Case Manager happy path, decisions, close/export/delete, artifacts/audit |
| `tests/test_controls_api.py` | HTTP auth/RBAC/routes/idempotency/live/API behavior |
| `tests/test_scheduler.py` | job leases, OPEN polling, outbox, Routing triggers, ordering/retries |
| `tests/test_persistence.py` | SQL migrations, restart, CAS, immutable results/case-data, deletion |
| `tests/test_main_demo.py` | полный fake HTTP flow |
| `tests/test_settings.py` | required environment and typed values |

`pytest`-число выше количества функций из-за parametrized cases. Истиной
является вывод конкретного CI run, а не число в документе.

## 3. Проверка Routing

```bash
python -m pytest -q \
  tests/test_timemodel.py \
  tests/test_site.py \
  tests/test_algorithm.py \
  tests/test_b1_score.py \
  tests/test_b2_b3_b6.py \
  tests/test_mvp_contracts.py
```

Критические invariants:

- один и тот же input -> byte-identical canonical JSON;
- input state не изменён;
- JSON round trip SearchState сохраняет поведение;
- результат не зависит от hash/set order;
- hard physical gate нельзя снять clipped timestamp;
- FAILED/TIMEOUT/UNAVAILABLE не становятся miss;
- LONG_GAP не входит в main route без confirm;
- OPEN IDs стабильны и close explicit;
- SVG валиден как XML, self-contained, без script/external URL и byte-stable.

Дополнительный hash-seed lane:

```bash
PYTHONHASHSEED=1 python -m pytest -q tests/test_mvp_contracts.py
PYTHONHASHSEED=777 python -m pytest -q tests/test_mvp_contracts.py
```

## 4. Contract и adapter acceptance

Сначала сгенерировать Schema:

```bash
javes-contract-schemas --out schemas
```

Для каждого real adapter тест обязан:

1. валидировать JSON соответствующей Pydantic/Schema model;
2. вызвать cross-message checker из `javes_contracts.acceptance`;
3. повторить identical command и получить тот же semantic result;
4. повторить тот же ID с изменённым payload и получить conflict;
5. проверить empty/error/partial/open/cancel/restart paths;
6. сохранить request/result fixtures как redacted golden JSON;
7. выполнить тест через настоящий transport, а не direct Python call.

Доступные checkers:

```python
from javes_contracts.acceptance import (
    check_gallery_result,
    check_probe_result,
    check_cut_result,
    check_clip_state_result,
    check_export_result,
    check_retention_result,
    check_video_segment_result,
    check_site_snapshot,
)
```

Они проверяют correlations и lifecycle invariants, которые JSON Schema не
может выразить. Case Manager дополнительно хранит raw worker result и передаёт
Routing только sanitised evidence.

### Perception matrix

| Scenario | Expected |
|---|---|
| finite hit | DONE, correlated tracklet/candidate, actual duration |
| finite empty | DONE, empty evidence, coverage > 0 |
| partial decode | DONE, `0 < coverage_ratio < 1`, clipped evidence |
| archive missing | UNAVAILABLE, zero coverage/evidence |
| worker error | FAILED, error code, zero evidence |
| deadline | TIMEOUT, zero evidence |
| duplicate delivery | identical no-op |
| OPEN upsert | stable IDs/t_start, monotonic t_end/duration |
| cancel | final `stream_closed=true`, no later updates |
| crowd | candidates reference distinct tracklets; no accidental merge |

### Clip matrix

| Scenario | Expected |
|---|---|
| cut ready | stable clip, READY+HOLD, artifact/checksum |
| source missing | UNAVAILABLE, no artifact/state |
| confirm/release | exact state and increased revision |
| early decision | command applied after delayed cut |
| ordered export | exact requested order |
| missing/unconfirmed | FAILED with missing IDs |
| duplicate/concurrent | one artifact/semantic record |

### Retention matrix

`tests/test_retention_worker.py` проверяет каждый resource class против
CLOSE/TTL/DELETE, legal hold и его снятие, late registration, already-missing
object, partial backend failure/restart/resume, duplicate delivery в одном и
двух worker instances, multi-location replicas, deferred backup receipt,
allow-listed files, audit tombstone/proof, Main integration и case auto-TTL.
Fake по-прежнему проверяет только policy state; реальные bytes проверяются
через `FileArtifactStore` и allow-listed file backends. Отдельного cloud/object
store в текущем плане нет. Внешний CCTV Archive не удаляется.

### Archive и Site Config matrix

Archive: full/partial/holes/unavailable, clock/DST, corrupt codec, cold tier,
immutable refs. Site: every reference/timing/coordinate validation, assets,
checksums, publish immutability, old version retrieval, concurrent publish/read,
multi-floor render smoke.

## 5. Main Infra integration

```bash
python -m pytest -q \
  tests/test_auth.py \
  tests/test_main_infra.py \
  tests/test_controls_api.py \
  tests/test_scheduler.py \
  tests/test_persistence.py \
  tests/test_main_demo.py
```

Особое внимание:

- mutation endpoints require bearer + RBAC + Idempotency-Key;
- no signup and ROOT-only user administration;
- outbox record создаётся в той же transaction, что domain state;
- worker side effect может повториться после crash и поэтому idempotent;
- Routing compute выполняется вне длинной transaction, commit защищён revision;
- route/live endpoints private;
- close/export/delete дают разные состояния и команды;
- delete cancels future work and purges/redacts case-scoped content;
- audit сохраняет минимальную полезную историю без удалённого payload.

## 6. SQL и PostgreSQL lane

Текущие SQL tests используют SQLite для portability. Перед интеграцией нужен
отдельный CI job с disposable PostgreSQL той же major version, что production:

1. `alembic upgrade head` на пустой DB;
2. upgrade с каждой поддерживаемой предыдущей revision;
3. repository suite;
4. два concurrent job claimers;
5. два outbox claimers;
6. два Routing trigger claimers;
7. parallel idempotent HTTP mutation;
8. CAS conflict/retry;
9. delete/outbox causal ordering;
10. process kill после external side effect и до delivered commit.

Успех SQLite не заменяет этот lane.

## 7. Clean package/install verification

```bash
tmp_dir="$(mktemp -d)"
python -m pip wheel --no-deps -w "$tmp_dir/wheels" .
python -m venv "$tmp_dir/venv"
"$tmp_dir/venv/bin/pip" install "$tmp_dir"/wheels/*.whl
"$tmp_dir/venv/bin/javes-contract-schemas" --out "$tmp_dir/schemas"
"$tmp_dir/venv/bin/javes-routing-demo" --scenario normal --out "$tmp_dir/demo"
"$tmp_dir/venv/bin/javes-main-migrate" \
  --database-url "sqlite:///$tmp_dir/main.sqlite" upgrade
```

Для `javes-main-demo` и HTTP API wheel устанавливается с infra dependencies.
Clean-install lane ловит отсутствующие package data и случайную зависимость от
working directory.

## 8. Simulator и benchmark

```bash
python -m routing.demo --scenario normal --seed 7 --out demo_out
python -m routing.demo --scenario late --seed 7 --out demo_out
python -m routing.demo --scenario reentry --seed 7 --out demo_out
python -m routing.demo --scenario outage --outage cam_2 --out demo_out
python -m routing.sim.bench --scenario ring8_chord --variant clean --seeds 200
python -m routing.sim.bench --scenario ring8_chord --variant mismatch --seeds 200
```

Simulator и алгоритм имеют разные params. Benchmark обязательно публикует
found rate, rounds, smart camera-minutes и explicit batch baseline. Он не должен
использоваться как доказательство качества на реальном видео; следующий gate —
annotated-data shadow run.

## 9. Нефункциональные тесты до production

- API/scheduler load и backpressure;
- Perception GPU saturation и queue priority;
- Archive/Clip throughput и codec corpus;
- large SiteGraph/Routing latency/memory;
- long-lived OPEN subscriptions и reconnect storm;
- filesystem transient errors, disk-full и atomic publish;
- chaos kill/restart каждого process;
- security scan, dependency scan, secret scan, auth abuse and IDOR;
- retention/deletion proof;
- backup restore drill;
- browser accessibility/CSP/XSS tests;
- 24–72h soak на staging.

## 10. Bug acceptance rule

Любая исправленная ошибка получает regression test на самом низком подходящем
уровне и, если ошибка пересекла component boundary, один integration/contract
test. Тест фиксирует observable behavior, а не внутренний порядок вызовов.

