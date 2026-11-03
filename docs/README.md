# JAVES-AI: карта документации

Статус этого репозитория: Routing, Main Infra/Controls, durable Retention,
реальный Perception worker (YOLO26 + ByteTrack + OSNet), API-first transport,
отдельные Routing API и read-only Site Config API реализованы. Настоящий Clip
и пользовательский Dashboard остаются внешними deliverables; fake-адаптеры
используются только старыми unit/integration fixtures. Внешний CCTV Video
Archive не входит в JAVES-AI: его read-only ссылки потребляют Perception/Clip.
Срок хранения данных кейса утверждён как 14 календарных дней; deployment не готов.

## Зафиксированные решения

- исходный CCTV Archive — внешняя read-only система; `archive_id` никогда не
  удаляется и не регистрируется в JAVES-AI Retention;
- отдельного cloud/object storage нет; `object_key` — относительный путь внутри
  private `FileArtifactStore`, заданного через `JAVES_ARTIFACT_ROOT`;
- `CLOSE_CASE` только фиксирует retention anchor; case-owned данные удаляются
  через 14 календарных дней либо раньше по явному `DELETE_CASE`;
- полный append-only security audit `actor/time/action/details` хранится
  бессрочно и не затирается при DELETE/TTL.

## Что читать и в каком порядке

1. [`JAVES-AI.pdf`](../JAVES-AI.pdf) — утверждённая командой продуктовая
   архитектура и главный источник требований.
2. [`architecture.md`](architecture.md) — что реально находится
   в коде, как идут данные и где проходят границы ответственности.
3. [`contracts.md`](contracts.md) —
   краткие wire-контракты между компонентами.
4. [`components.md`](components.md) —
   подробная спецификация Perception, Clip, Retention, Site Config и Dashboard,
   а также read-only границы с внешним CCTV Archive.
5. [`deployment.md`](deployment.md) — окружения,
   процессы, миграции, секреты, выпуск и откат.
6. [`testing.md`](testing.md) — тестовая пирамида,
   покрытие требований и contract-acceptance procedure.

## Где что находится

| Путь | Содержимое |
|---|---|
| `routing/` | алгоритм Routing, SiteGraph, simulator и SVG renderer |
| `javes_main/` | Case Manager, Controls API, IAM, scheduler, persistence и adapters |
| `javes_retention/` | policy, durable inventory/journal, deletion backends и Retention worker |
| `javes_perception/` | Perception worker: YOLO26, ByteTrack, OSNet, оценка Re-ID на Market-1501 |
| `perception.yaml` | камеры Perception (видеофайл + UTC старт) и пороги |
| `docs/perception.md` | как устроен Perception, точность и запуск |
| `javes_contracts/` | публичные модели, JSON Schema и acceptance checks |
| `javes_routing_api/` | stateless HTTP shell над чистым Routing |
| `javes_site_config/` | immutable filesystem-backed Site Config API |
| `javes_workers/` | общий HTTP pull/lease client |
| `javes_clip/` | Clip contract boundary и explicit unavailable runner |
| `javes_fakes/` | legacy contract doubles только для тестов |
| `tests/` | unit, integration, API, persistence и contract tests |
| `examples/` | канонические JSON/YAML примеры |
| `docs/architecture.md` | фактическая архитектура всего реализованного кода |
| `docs/components.md` | устройство и I/O оставшихся real components |
| `docs/contracts.md` | краткие межкомпонентные wire-контракты |
| `docs/testing.md` | текущие acceptance tests |
| `docs/deployment.md` | production deployment и Site Config publishing |
| `docs/reference/` | дополнительные действующие handoff и verification документы |

## Нормативность документов

При конфликте применяется следующий порядок:

1. [`JAVES-AI.pdf`](../JAVES-AI.pdf);
2. опубликованные модели `javes_contracts` и их JSON Schema;
3. `architecture.md`, `contracts.md`, `components.md`;
4. остальные действующие документы из этого каталога.

Новый публичный контракт не добавляется только через Markdown: сначала меняется
версионированная модель в `javes_contracts`, генерируется Schema, добавляются
acceptance-тесты, затем обновляются документы. Общие контракты не меняются
односторонне и требуют согласования владельцев компонентов.

## Специализированные документы

- [`../README.md`](../README.md) — быстрый старт и публичные команды.
- [`reference/verification.md`](reference/verification.md) — краткая таблица
  реализовано/не реализовано.
- [`reference/perception-clip.md`](reference/perception-clip.md) —
  расширенный handoff для этих двух воркеров.
- [`reference/auth-and-iam.md`](reference/auth-and-iam.md) — закрытая
  модель аккаунтов без signup.
- [`reference/fakes.md`](reference/fakes.md) — свойства test doubles.
- [`reference/demo.md`](reference/demo.md) — сценарии Routing demo.

## Машиночитаемые источники

```bash
javes-contract-schemas --out schemas
python -m javes_fakes.demo
python -m pytest -q
```

`schemas/` генерируется и не хранится как исходник. Канонические JSON-примеры
лежат в `../examples/`. Импорт внешних компонентов должен идти из
`javes_contracts`, а не из `javes_fakes` и не из внутренних модулей
`javes_main`.

