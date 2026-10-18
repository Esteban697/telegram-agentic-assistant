"""The image audit against **real layers** — the whiteout case, with its own can-fail companion.

    uv run pytest -m integration tests/integration/test_image_audit.py

`scripts/audit_image.py` used to run `docker create` + `docker export`, which is a container's
*merged* filesystem: a secret `COPY`ed in one layer and deleted in a later one is invisible there
while remaining perfectly pullable from the registry. It now `docker save`s the image and walks the
layer tars. This test builds both shapes for real and asserts the difference — the first half is the
fix, the second half is what the old code did, so a regression to the merged view fails here.

Skipped when Docker is not available; `integration` because it needs a container runtime, which is
neither a credential nor a database.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
AUDIT = REPO / "scripts" / "audit_image.py"
SENTINEL = "123456:AAHwhiteout-sentinel-value-0123456789"


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment dependent
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_works(), reason="docker is not available"),
]


def load_audit():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location("audit_image", AUDIT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def image_with_a_deleted_secret(tmp_path: pathlib.Path) -> str:
    """A real image where the secret is `COPY`ed and then `rm`ed in a later layer."""

    context = tmp_path / "context"
    context.mkdir()
    (context / "Dockerfile").write_text(
        "FROM busybox\n"
        "COPY secret.env /app/secret.env\n"
        "RUN rm /app/secret.env\n"
        "RUN echo 'still here' > /app/main.txt\n",
        encoding="utf-8",
        newline="\n",
    )
    (context / "secret.env").write_text(f"TOKEN={SENTINEL}\n", encoding="utf-8")

    tag = f"taa-audit-whiteout-{uuid.uuid4().hex[:8]}"
    built = subprocess.run(
        ["docker", "build", "-t", tag, str(context)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert built.returncode == 0, built.stderr[-500:]
    try:
        yield tag
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, text=True)


def _save(image: str, destination: pathlib.Path) -> None:
    subprocess.run(["docker", "save", "-o", str(destination), image], check=True, timeout=300)


def _export(image: str, destination: pathlib.Path) -> None:
    """The old method: a container's merged filesystem."""

    created = subprocess.run(["docker", "create", image], capture_output=True, text=True, check=True)
    container = created.stdout.strip()
    try:
        subprocess.run(
            ["docker", "export", "-o", str(destination), container], check=True, timeout=300
        )
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True)


def test_a_secret_deleted_in_a_later_layer_is_still_found(
    image_with_a_deleted_secret: str, tmp_path: pathlib.Path
) -> None:
    """The fix: the audit sees the layer that carried it."""

    audit = load_audit()
    saved = tmp_path / "image.tar"
    _save(image_with_a_deleted_secret, saved)

    findings = audit.scan_image(saved, sentinel=SENTINEL)

    assert any("secret.env" in finding for finding in findings), (
        f"the layer walk missed a secret that is still in the image: {findings}"
    )
    assert any("sentinel" in finding for finding in findings), findings


def test_the_merged_filesystem_view_really_would_have_missed_it(
    image_with_a_deleted_secret: str, tmp_path: pathlib.Path
) -> None:
    """**The companion.** The old route, on the same image, finds nothing — which is why it changed.

    Without this, the assertion above could pass because `secret.env` is simply present in a
    container view, and the layer walk would be unmotivated.
    """

    audit = load_audit()
    exported = tmp_path / "container.tar"
    _export(image_with_a_deleted_secret, exported)

    findings = audit.scan_tar(exported, sentinel=SENTINEL)

    assert findings == [], (
        "precondition: the merged view is supposed to miss the deleted secret, so this test's "
        f"counterpart means nothing: {findings}"
    )
