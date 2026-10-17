# Reproducible image for the Telegram personal assistant (Phase 1).
#
# Design points, per PLAN.md §11.1:
#   * multi-stage build; runtime carries no compilers and no test dependencies
#   * the base tag is pinned, and its digest is recorded in the build report
#   * runs as a non-root user
#   * no secret, no .env, nothing from the repository that is not the application itself
#   * exec-form CMD so uvicorn receives SIGTERM directly and can drain

FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY app ./app

# Install the application and its pinned dependencies into an isolated prefix, so the
# runtime stage copies exactly one tree and nothing else.
RUN pip install --prefix=/install .

# --------------------------------------------------------------------------- #

FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:${PATH}"

# Non-root runtime user. `id -u` inside the container is 10001 (A8 asserts it is not 0).
RUN groupadd --system --gid 10001 appuser \
 && useradd --system --uid 10001 --gid appuser --no-create-home appuser

COPY --from=builder /install /usr/local

# The probe is shipped so an operator can run the model-compatibility check against a real
# key without rebuilding the image.
COPY scripts /app/scripts

WORKDIR /app
USER appuser

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
