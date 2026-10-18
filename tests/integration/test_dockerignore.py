"""`.dockerignore` against **Docker's own matcher** — the guard that can actually fail.

    uv run pytest -m integration tests/integration/test_dockerignore.py

The unit-level guard (`tests/unit/test_deploy_artifacts.py`) can only assert that certain strings
appear in the file; it cannot observe what Docker does with them, which is where both previous
versions of `.dockerignore` were wrong. This test builds real contexts:

1. the repository's `.dockerignore` plus a set of probe files that must be excluded — and a control
   file that must ship;
2. **a stripped variant with the root-level patterns only**, which must let the probes through.
   Without this half the test could pass for the wrong reason (a build context that failed, a
   pattern list that matched nothing, a Docker version that ignores `.dockerignore` entirely).

Skipped when Docker is not available; `integration` because it needs a container runtime, not a
database, and it is not a credential.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DOCKERIGNORE = REPO / ".dockerignore"

pytestmark = pytest.mark.integration

# Exactly the shapes the review found shipping: everything under `scripts/`, which the image
# deliberately keeps, plus a key. `scripts/notes.txt` is the control — it must survive.
PROBES: dict[str, str] = {
    "scripts/build/artefact.txt": "build residue\n",
    "scripts/dist/art.txt": "dist residue\n",
    "scripts/venv/lib.py": "vendored environment\n",
    "scripts/htmlcov/index.html": "<html></html>\n",
    "scripts/.coverage": "coverage data\n",
    "scripts/id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----\n",
    "scripts/sub/private.pem": "-----BEGIN PRIVATE KEY-----\n",
    "scripts/__pycache__/cached.pyc": "bytecode\n",
}
CONTROL = "scripts/notes.txt"


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment dependent
        return False
    return result.returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_works(), reason="docker is not available"),
]


def _build_context(ignore: str, workdir: pathlib.Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "Dockerfile").write_text(
        "FROM busybox\nCOPY . /x\n", encoding="utf-8", newline="\n"
    )
    (workdir / ".dockerignore").write_text(ignore, encoding="utf-8", newline="\n")
    for relative, body in PROBES.items():
        target = workdir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="\n")
    control = workdir / CONTROL
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text("this file must ship\n", encoding="utf-8", newline="\n")


def _files_in_image(workdir: pathlib.Path) -> list[str]:
    tag = f"taa-dockerignore-{uuid.uuid4().hex[:8]}"
    try:
        built = subprocess.run(
            ["docker", "build", "-q", "-t", tag, str(workdir)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert built.returncode == 0, built.stderr[-500:]
        listed = subprocess.run(
            ["docker", "run", "--rm", tag, "sh", "-c", "find /x -type f"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert listed.returncode == 0, listed.stderr[-500:]
        return [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, text=True)


def test_the_real_dockerignore_excludes_every_probe(tmp_path: pathlib.Path) -> None:
    workdir = tmp_path / "context"
    _build_context(DOCKERIGNORE.read_text(encoding="utf-8"), workdir)

    present = _files_in_image(workdir)

    assert f"/x/{CONTROL}" in present, (
        f"the control file did not ship, so the build did not do what this test assumes: {present}"
    )
    leaked = [path for path in present if any(path.endswith(probe.split("/", 1)[1]) and
              f"/{probe.split('/', 1)[0]}/" in path for probe in PROBES)]
    assert leaked == [], f"these files were excluded by the pattern list but shipped anyway: {leaked}"


def test_the_check_can_fail_without_the_depth_independent_patterns(tmp_path: pathlib.Path) -> None:
    """**The companion.** Strip the `**/` prefixes and the same probes must reach the image.

    This is what the two previous versions of `.dockerignore` did, and it is why the assertion
    above is worth anything: the harness really can see a leak.
    """

    stripped = "\n".join(
        line
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("**/")
    )
    workdir = tmp_path / "context-stripped"
    _build_context(stripped, workdir)

    present = _files_in_image(workdir)

    leaked = [path for path in present if any(path.endswith(probe.split("/", 1)[1]) for probe in PROBES)]
    assert leaked, (
        "removing the **/ patterns changed nothing, so this harness cannot observe the matcher "
        f"and the assertion above proves nothing: {present}"
    )
