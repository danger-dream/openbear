# One source/image for controller and managed roles. Release ZIPs may contain
# prebuilt web/dist only; source checkouts build that same frontend here.
FROM node:22-bookworm-slim AS frontend
WORKDIR /source
COPY pyproject.toml ./
COPY app/__init__.py ./app/__init__.py
COPY web/ ./web/
RUN if [ -f web/package.json ] && [ -d web/src ]; then \
      cd web && npm ci && npm run build; \
    else test -s web/dist/index.html; fi

FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/openbear
WORKDIR /opt/openbear
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git openssh-client tini && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md ./
COPY app/ ./app/
COPY prompts/ ./prompts/
COPY benchmark-package/ ./benchmark-package/
COPY --from=frontend /source/web/dist/ ./web/dist/
COPY scripts/container-entrypoint.sh /usr/local/bin/openbear-entrypoint
# The repository is a virtual uv project: run its source via PYTHONPATH and
# install only the production packages/versions/hashes from the existing lock.
RUN pip install --no-cache-dir uv==0.9.30 \
    && uv export --frozen --no-dev --no-emit-project --output-file /tmp/requirements.txt \
    && uv pip install --system --no-cache --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && printf '#!/bin/sh\nexec python -m app.cli "$@"\n' > /usr/local/bin/openbear \
    && chmod 755 /usr/local/bin/openbear-entrypoint /usr/local/bin/openbear
EXPOSE 18961
# No role default, credentials, Docker socket or host mounts baked into the image.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/openbear-entrypoint"]
CMD ["python", "-m", "app.main"]
