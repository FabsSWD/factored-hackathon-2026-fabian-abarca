# The API and the database bootstrap (M19). Build context: the repository root.
#
# The application runs from its source tree (/srv): configuration, migrations and the roles
# SQL are read relative to it. Runtime dependencies come from pyproject.toml, pinned by
# constraints.txt to the versions the test suite runs with.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/srv

WORKDIR /srv

COPY pyproject.toml docker/api/constraints.txt ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" > requirements.txt \
    && pip install -r requirements.txt -c constraints.txt \
    && rm requirements.txt

COPY alembic.ini ./
COPY app ./app
COPY config ./config
COPY migrations ./migrations
COPY scripts/serve.py scripts/bootstrap_db.py ./scripts/
COPY scripts/sql ./scripts/sql

# Unprivileged runtime user; data/core is bind-mounted read-only by the migrate service.
RUN useradd --system --uid 10001 --home-dir /srv --no-create-home app \
    && mkdir -p data/core && chown -R app:app /srv
USER app

EXPOSE 8000

# One worker: conversation state is kept in memory (scripts/serve.py). Proxy headers from the
# frontend's nginx give the real client IP to the per-IP rate limits (FORWARDED_ALLOW_IPS).
CMD ["python", "scripts/serve.py", "--host", "0.0.0.0", "--port", "8000"]
