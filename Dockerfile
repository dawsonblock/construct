# Pinned by digest — see docs/PINNED_VERSIONS.md
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Release label. Kept in sync with VERSION / pyproject.toml / __version__.
ARG APP_VERSION=0.5.0-rc5.dev0
LABEL org.opencontainers.image.title="construction-ai-ops" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="Evidence-first construction operations AI control plane"

# The lock is a complete transitive closure, so --no-deps makes the install
# exactly reproducible: pip resolves nothing.
COPY requirements.lock.txt requirements-dev.lock.txt ./
RUN pip install --no-deps -r requirements.lock.txt

# Dev/test deps are a separate layer so production images can stop above.
ARG INSTALL_DEV=0
RUN if [ "$INSTALL_DEV" = "1" ]; then pip install --no-deps -r requirements-dev.lock.txt; fi

COPY . .

RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /app/object_store \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=6 \
  CMD python -c "import os,urllib.request,sys; p=os.getenv('HEALTH_PATH','/health'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:8000{p}',timeout=4).status==200 else 1)"

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
