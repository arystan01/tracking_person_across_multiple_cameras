FROM python:3.11-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY javes_contracts javes_contracts
COPY javes_main javes_main
COPY javes_routing javes_routing
COPY javes_site_config javes_site_config
COPY javes_workers javes_workers
COPY javes_retention javes_retention
COPY javes_perception javes_perception
COPY javes_clip javes_clip
COPY javes_archive javes_archive
COPY javes_release javes_release
COPY javes_telemetry javes_telemetry
COPY javes_security javes_security
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels-infra '.[infra]'

FROM builder AS perception-builder
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels-perception '.[infra,perception]'

FROM python:3.11-slim AS runtime-base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install --no-install-recommends -y postgresql-client && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 javes && useradd --system --uid 10001 --gid javes --home /nonexistent javes
COPY --from=builder /wheels-infra /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=2)"]
ENTRYPOINT ["javes-main-api"]
CMD ["--host", "0.0.0.0", "--port", "8080"]

FROM runtime-base AS perception-runtime
USER root
COPY --from=perception-builder /wheels-perception /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels
USER 10001:10001

FROM runtime-base AS clip-runtime
USER root
RUN apt-get update && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD ["sh", "-c", "kill -0 1"]
ENTRYPOINT ["javes-clip-worker"]

FROM runtime-base AS runtime
