FROM python:3.12-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af AS runtime

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl --fail --silent --show-error --location \
        --output /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && printf '%s\n' \
        'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install --no-install-recommends --yes postgresql-client-18 \
    && apt-get purge --auto-remove --yes curl \
    && rm -rf /var/lib/apt/lists/* \
        /etc/apt/sources.list.d/pgdg.list \
        /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && pip install --no-cache-dir uv==0.9.28 \
    && uv sync --locked --no-dev --no-install-project
COPY alembic alembic
COPY alembic.ini ./
COPY src src
RUN uv sync --locked --no-dev \
    && useradd --system --uid 10001 --create-home cookops \
    && mkdir -p /var/lib/cookops/receipts \
    && chown -R cookops:cookops /app /var/lib/cookops

USER cookops
EXPOSE 8000

CMD ["sh", "-c", ": \"${COOKOPS_TRUSTED_PROXY_IPS:?COOKOPS_TRUSTED_PROXY_IPS must name the Apache proxy}\"; case \"$COOKOPS_TRUSTED_PROXY_IPS\" in *\\*) echo 'COOKOPS_TRUSTED_PROXY_IPS must not contain *' >&2; exit 64 ;; esac; exec uvicorn cookops.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"$COOKOPS_TRUSTED_PROXY_IPS\""]
