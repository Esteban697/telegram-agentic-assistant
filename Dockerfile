# Reproducible image for the Telegram personal assistant (PLAN.md §11.1, Phase 5 hardening).
#
# One image, four commands (decision 18; §11.1 is the authority for the count):
#
#   uvicorn app.main:app --host 0.0.0.0 --port 8080      # the service (this file's CMD)
#   python -m app.jobs.dispatch_reminders                # Cloud Run Job: due reminders
#   python -m app.jobs.process_documents                 # Cloud Run Job: finish pending ingest
#   python -m app.jobs.resume_work                       # Cloud Run Job: budget-interrupted turns
#
# Design points, per PLAN.md §11.1:
#   * multi-stage: the runtime stage carries no compilers and no test dependencies
#   * the base image is pinned by **digest**, not only by tag (see the note below)
#   * `--no-cache-dir` everywhere, so no wheel cache is baked into a layer
#   * runs as a non-root user (uid 10001, asserted non-zero by the image audit)
#   * no secret and no `.env` in the image, and `.dockerignore` is what keeps them out
#   * exec-form CMD so uvicorn receives SIGTERM directly and can drain — a drain cut mid-turn is
#     exactly the case the resume queue exists for
#   * `migrations/` and `alembic.ini` ship with the image so `scripts/migrate.py upgrade head`
#     can be run as a one-off Cloud Run Job without a checkout on the operator's machine
#
# **Why a digest and not just `python:3.13-slim`.** A tag is a mutable pointer: the same
# Dockerfile built tomorrow can produce a different image, which is the opposite of the
# reproducibility spec §13 asks for. The digest below was resolved on the development machine on
# 2026-10-17 with
#
#     docker pull python:3.13-slim
#     docker image inspect python:3.13-slim --format '{{index .RepoDigests 0}}'
#     -> python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
#
# (`python:3.13-slim`, image id `sha256:70729b46…`, created 2026-10-06T02:05:24Z.) The tag is kept
# alongside the digest so a human can still read which line of Python this is; Docker resolves the
# digest, so the tag cannot drift the build.

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Only what the package build needs. `README.md` is here because `pyproject.toml` names it as the
# long description; nothing else from the repository is copied into the builder.
COPY pyproject.toml README.md ./
COPY app ./app

# Install the application and its pinned dependencies into an isolated prefix, so the runtime
# stage copies exactly one tree and nothing else — no `pip`, no compilers, no build backend.
RUN pip install --no-cache-dir --prefix=/install .

# --------------------------------------------------------------------------- #

FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:${PATH}"

# Non-root runtime user, created before anything is copied. `--no-create-home` keeps the image
# small and gives the process nowhere writable to leave state; the application keeps everything
# in PostgreSQL, Cloud SQL and Cloud Storage precisely so that it needs none.
RUN groupadd --system --gid 10001 appuser \
 && useradd --system --uid 10001 --gid appuser --no-create-home appuser

COPY --from=builder /install /usr/local

# `WORKDIR` comes *before* the copies below so they land in `/app`: with the default workdir the
# relative destinations would resolve to `/alembic.ini` and `/migrations`, and `WORKDIR /app`
# afterwards would then create an empty directory and say nothing about it.
WORKDIR /app

# The migration path, so the one-off migration job can run from this image alone. `scripts/`
# deliberately ships (the Ollama compatibility probe and the migrator), and it carries no secret:
# every credential it needs arrives through the environment at runtime.
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts

USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
