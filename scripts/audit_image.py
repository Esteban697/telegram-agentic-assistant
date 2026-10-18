"""Audit a built image for secrets — PLAN.md §9.5, §11.1, acceptance (c).

    python scripts/audit_image.py --image telegram-assistant:phase5
    python scripts/audit_image.py --self-test        # no docker needed: proves the detector works

What it does: `docker save` the image and walk **every layer** — names *and* contents — looking for
three kinds of thing:

* **files that must not be in the image at all**: `.env`, `*.env`, service-account JSON names,
  `id_rsa*`; and private-key **material** in anything else, since a `.pem` may legitimately be a
  public certificate (the base image ships a whole trust store of them);
* **content shapes that are credentials**: a PEM block with a real key body, a service-account JSON
  with `private_key`/`private_key_id`, and — on our own files — the same bearer/hex/base64 shapes
  `app.logging_config` scrubs from log lines;
* **the `--sentinel` value**, if given, anywhere.

**Layers, not the merged filesystem.** The first version used `docker create` + `docker export`,
which is what a *container* would see: a secret `COPY`ed in one layer and `rm`ed in a later one was
invisible to the audit while remaining pullable from the registry. `scan_image` walks the layer tars
instead, so a whiteout-deleted secret is still found — which is where it actually is.

The sentinel is how a test makes this non-vacuous: build an image whose context contains
`--sentinel`'s value and the audit must fail. `--self-test` does the same thing without docker, on
synthetic tars it builds in memory, and is what the credential-free unit suite runs.

Exit codes: 0 = clean, 1 = findings, 2 = the audit could not run (docker missing, image absent).
"""

from __future__ import annotations

import argparse
import io
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile

PLACEHOLDER_FREE = "no findings"

# Names that are unambiguous on their own (PLAN.md §9.5 "No-secret-in-image test").
#
# `.pem`, `.key` and `.p12` are deliberately **not** name rules. The first version had them, and on
# the real image it produced 174 findings — Mozilla's CA bundle (`etc/ssl/certs/*.pem`), certifi's
# `cacert.pem`, and the *source* of `cryptography`/`google-auth`, which necessarily contain the
# strings "-----BEGIN … PRIVATE KEY-----" because parsing keys is their job. An audit that cries
# wolf 174 times is an audit nobody reads, so a key is now detected by its **material** (below)
# rather than by its file extension, and a public certificate is not a finding at all.
FORBIDDEN_NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)\.env(\..+)?$"),
    re.compile(r"(^|/)service[-_]?account[^/]*\.json$", re.IGNORECASE),
    re.compile(r"(^|/)id_rsa[^/]*$"),
)

# Content shapes, and each requires the **substance** of a credential, not a mention of one:
#
#  * a PEM header must be followed by a real base64 key body (64+ characters). A library that
#    formats PEM has the header as a string constant and nothing after it;
#  * a service-account JSON must carry the identifier of a key as well as the type;
#  * `"(private_key|…)"\s*:` alone is not a rule: `google-auth`'s own source contains it.
FORBIDDEN_CONTENT_PATTERNS: tuple[re.Pattern[bytes], ...] = (
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,200}?[A-Za-z0-9+/]{64,}"),
    re.compile(
        rb'"type"\s*:\s*"service_account"'
        rb'(?=[\s\S]{0,4000}?"(private_key|private_key_id)"\s*:)'
    ),
)

# Credential *shapes* in text members — the third layer, added after review pointed out that the
# name and material rules alone could miss a token pasted into a source file.
#
# `MINIMAL` is applied everywhere and is deliberately near-false-positive-free: a Telegram bot
# token (`<6+ digits>:<30+ chars>`) is a shape nothing legitimate in an image has. `OURS` adds the
# noisier rules — a PEM block, a service-account body, and `app.logging_config`'s own pattern list
# (bearer tokens, long hex runs, base64-looking runs) — because those *do* appear in vendor code
# (`cryptography` parses PEM; every library has a 32-character hex constant) and would bury a real
# finding under false ones. `_is_ours` decides which set applies.
SHAPE_PATTERNS_MINIMAL: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{30,}\b"), "a Telegram bot token"),
)


def _ours_shapes() -> tuple[tuple[re.Pattern[str], str], ...]:
    """The broad set, including `app.logging_config`'s, loaded defensively."""

    patterns: list[tuple[re.Pattern[str], str]] = [
        *SHAPE_PATTERNS_MINIMAL,
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a PEM private key"),
        (re.compile(r'"type"\s*:\s*"service_account"'), "a service-account JSON"),
    ]
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from app.logging_config import _SECRET_PATTERNS  # noqa: PLC0415 - optional, best effort

        patterns.extend((pattern, "a credential (logging_config pattern)") for pattern in _SECRET_PATTERNS)
    except Exception:  # noqa: BLE001 - the audit must work even without the application importable
        pass
    return tuple(patterns)


SHAPE_PATTERNS_OURS: tuple[tuple[re.Pattern[str], str], ...] = _ours_shapes()


# Directories that legitimately contain `.pem` files: the base image's own CA trust store. Flagging
# them would bury a real finding under two hundred lines of Mozilla's root certificates — the first
# run of this audit did exactly that, and an audit nobody reads is not an audit.
TRUST_STORE_PREFIXES = (
    "etc/ssl/certs/",
    "etc/pki/",
    "etc/ca-certificates/",
    "usr/share/ca-certificates/",
    "usr/local/share/ca-certificates/",
    "usr/lib/ssl/certs/",
)

# The auditor itself contains the *patterns* it looks for, so scanning it finds them. Excluded by
# name rather than by weakening the patterns: the patterns are the value here.
SELF_EXEMPT_SUFFIXES = ("scripts/audit_image.py",)


def _is_trust_store(name: str) -> bool:
    stripped = name.lstrip("./")
    return any(stripped.startswith(prefix) for prefix in TRUST_STORE_PREFIXES)


def scan_tar(path: pathlib.Path, *, sentinel: str = "") -> list[str]:
    """Return one human-readable finding per problem in the plain tar at `path`."""

    with tarfile.open(path, "r:*") as archive:
        return _scan_members(archive, label=path.name, sentinel=sentinel)


def _is_exempt(name: str) -> bool:
    stripped = name.lstrip("./")
    return any(stripped.endswith(suffix) for suffix in SELF_EXEMPT_SUFFIXES)


def _is_ours(name: str) -> bool:
    """Our own artefacts, as opposed to a third-party library's internals.

    The shape rules below are noisy on vendor code (`cryptography`'s source quotes PEM headers;
    every library has 32-character hex constants), and quiet where it matters: a secret we shipped
    is in our own code, in our own config or data files, or in the image workdir.
    """

    stripped = name.lstrip("./")
    if "/site-packages/" in stripped or "/dist-packages/" in stripped:
        # Vendor code and vendor metadata only: `cryptography` parses PEM, and every wheel ships an
        # SBOM whose `*.json` is full of sha256 hashes. Those are two of the three false-positive
        # classes this rule produced on a real image; our own installed package is still ours.
        return "/site-packages/app/" in stripped
    if stripped.startswith(("app/", "scripts/", "usr/local/bin/")):
        return True
    return stripped.endswith(CONFIG_SUFFIXES)


CONFIG_SUFFIXES = (".json", ".env", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt", ".conf")


def _looks_binary(body: bytes) -> bool:
    return b"\x00" in body[:4096] or len(body) > 1_048_576


def _scan_shapes(name: str, body: bytes, *, own: bool | None = None) -> list[str]:
    """Credential shapes in a text member. Precise everywhere, thorough on our own files."""

    if _looks_binary(body):
        return []
    text = body.decode("utf-8", errors="replace")
    ours = _is_ours(name) if own is None else own
    patterns = SHAPE_PATTERNS_OURS if ours else SHAPE_PATTERNS_MINIMAL
    return [
        f"content: {name} matches the shape of {label}"
        for pattern, label in patterns
        if pattern.search(text)
    ]


def _scan_text(
    name: str, body: bytes, *, sentinel: str = "", own: bool | None = None
) -> list[str]:
    findings: list[str] = []
    if not _is_trust_store(name):
        for pattern in FORBIDDEN_NAME_PATTERNS:
            if pattern.search(name):
                findings.append(f"name: {name} matches {pattern.pattern}")
    if _is_exempt(name):
        return findings
    for pattern in FORBIDDEN_CONTENT_PATTERNS:
        if pattern.search(body):
            findings.append(f"content: {name} matches {pattern.pattern!r}")
    if sentinel and sentinel.encode("utf-8") in body:
        findings.append(f"content: {name} contains the sentinel value")
    findings.extend(_scan_shapes(name, body, own=own))
    return findings


def scan_image(path: pathlib.Path, *, sentinel: str = "") -> list[str]:
    """Walk a `docker save` archive: the outer tar, then every layer tar inside it.

    Both on-disk layouts are handled without parsing `manifest.json`: the classic
    `<hash>/layer.tar` and the OCI `blobs/sha256/<hash>` (a gzipped tar, which `tarfile` sniffs).
    Any member that opens as a tar is scanned as one; members that do not are skipped.
    """

    findings: list[str] = []
    with tarfile.open(path, "r:*") as outer:
        for member in outer:
            if not member.isfile():
                continue
            handle = outer.extractfile(member)
            if handle is None:  # pragma: no cover - a tar member with no body
                continue
            payload = handle.read()
            try:
                with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as layer:
                    findings.extend(_scan_members(layer, label=member.name, sentinel=sentinel))
            except tarfile.TarError:
                # `manifest.json`, `index.json`, `config.json`: image *metadata*, not layers. Scanned
                # for the sentinel and for forbidden names, but with the minimal shape set — their
                # sha256 digests would otherwise trip the broad "long hex run" rule, which is how
                # this audit managed to report two findings about its own image metadata.
                findings.extend(_scan_text(member.name, payload, sentinel=sentinel, own=False))
    return findings


def _scan_members(archive: tarfile.TarFile, *, label: str, sentinel: str) -> list[str]:
    findings: list[str] = []
    for member in archive:
        if not member.isfile():
            continue
        handle = archive.extractfile(member)
        if handle is None:  # pragma: no cover
            continue
        findings.extend(_scan_text(member.name, handle.read(), sentinel=sentinel))
    return findings


def self_test() -> int:
    """Prove the detector can fail, without docker and without any image.

    A check that cannot fail is worth nothing: this plants the two things an image audit exists to
    catch, and one thing it must *not* flag, and reports whether the detector behaved.
    """

    key_material = (
        b"-----BEGIN PRIVATE KEY-----\n"
        + b"bXlwcmV0ZW5ka2V5Ym9keQ" * 4
        + b"\n-----END PRIVATE KEY-----\n"
    )
    planted = io.BytesIO()
    with tarfile.open(fileobj=planted, mode="w") as archive:
        for name, body in (
            ("app/.env", b"TELEGRAM_BOT_TOKEN=123456:AAHsentinel-value-that-must-be-caught\n"),
            (
                "app/service-account.json",
                b'{"type": "service_account", "private_key_id": "abc", "private_key": "..."}',
            ),
            ("usr/local/lib/key.pem", key_material),
            # Both false-positive shapes the first version got wrong: a CA bundle, and library
            # source that mentions a PEM header without carrying a key.
            ("etc/ssl/certs/ISRG_Root_X1.pem", b"-----BEGIN CERTIFICATE-----\nMIIB\n"),
            (
                "usr/local/lib/python3.13/site-packages/cryptography/serialization/ssh.py",
                b'HEADER = b"-----BEGIN PRIVATE KEY-----"\n',
            ),
            ("app/main.py", b"print('hola')\n"),
        ):
            data = body
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    planted_path = _write_temp(planted.getvalue())

    clean = io.BytesIO()
    with tarfile.open(fileobj=clean, mode="w") as archive:
        for name, body in (
            ("app/main.py", b"print('hola')\n"),
            ("app/config.py", b"BOT_TOKEN_VARIABLE_NAME = 'TELEGRAM_BOT_TOKEN'\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    clean_path = _write_temp(clean.getvalue())

    problems: list[str] = []
    planted_findings = scan_tar(planted_path, sentinel="123456:AAHsentinel-value-that-must-be-caught")
    for expected in (".env", "service-account.json", "key.pem", "sentinel"):
        if not any(expected in finding for finding in planted_findings):
            problems.append(f"the detector missed {expected!r} in the planted tar")

    # And the false positives, which are as important as the detections: a CA bundle is not a key,
    # and a library that quotes a PEM header is not carrying one.
    for unwanted in ("ISRG_Root_X1", "cryptography/serialization/ssh.py", "main.py"):
        if any(unwanted in finding for finding in planted_findings):
            problems.append(f"the detector flagged {unwanted!r}, which is not a secret")

    clean_findings = scan_tar(clean_path)
    if clean_findings:
        problems.append(f"the detector flagged a clean tar: {clean_findings}")

    # **The layer walk**, which is the whole reason this uses `docker save`. A secret copied in one
    # layer and whiteout-deleted in the next is still in the image — and the old
    # `docker create`/`docker export` route (a container's merged view) could not see it.
    layered_findings = scan_image(_write_temp(_synthetic_image_layers()), sentinel=SENTINEL)
    if not any(".env" in finding for finding in layered_findings):
        problems.append("the layer walk missed a secret that a later layer deleted")
    plain_findings = scan_tar(_write_temp(_synthetic_merged_filesystem()), sentinel=SENTINEL)
    if any(".env" in finding for finding in plain_findings):
        problems.append(
            "precondition broken: the merged-filesystem scan is supposed to miss the whiteout case"
        )

    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(f"self-test ok: {len(planted_findings)} planted finding(s), {PLACEHOLDER_FREE} in the clean tar")
    for finding in planted_findings:
        print(f"  detected: {finding}")
    return 0


SENTINEL = "123456:AAHsentinel-value-that-must-be-caught"


def _tar_bytes(entries: tuple[tuple[str, bytes], ...]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, body in entries:
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _synthetic_image_layers() -> bytes:
    """A `docker save`-shaped archive: layer 0 carries the secret, layer 1 whiteouts it."""

    return _tar_bytes(
        (
            ("manifest.json", b'{"Layers": ["layer0.tar", "layer1.tar"]}'),
            ("layer0.tar", _tar_bytes((("app/.env", f"TOKEN={SENTINEL}\n".encode()),))),
            ("layer1.tar", _tar_bytes((("app/.wh..env", b""),))),
        )
    )


def _synthetic_merged_filesystem() -> bytes:
    """What the *old* method saw: the container's filesystem, where the secret is already gone."""

    return _tar_bytes((("app/main.py", b"print('hola')\n"),))


def _write_temp(payload: bytes) -> pathlib.Path:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
    handle.write(payload)
    handle.close()
    return pathlib.Path(handle.name)


def export_image(image: str, destination: pathlib.Path) -> None:
    """`docker save`: every **layer**, not the merged filesystem.

    The first version used `docker create` + `docker export`, which is the union filesystem of a
    container — so a secret `COPY`ed in one layer and `rm`ed in a later one was invisible to the
    audit while remaining perfectly pullable from the registry. `docker save` keeps the layers, and
    a leaked-then-deleted secret is found in the layer that carried it, which is where it still is.
    """

    subprocess.run(
        ["docker", "save", "-o", str(destination), image],
        capture_output=True,
        text=True,
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audita una imagen en busca de secretos.")
    parser.add_argument("--image", default="", help="referencia de la imagen (docker)")
    parser.add_argument(
        "--sentinel",
        default="",
        help="valor que NO puede aparecer en la imagen (lo usa la prueba negativa)",
    )
    parser.add_argument("--self-test", action="store_true", help="prueba el detector, sin docker")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.image:
        parser.error("--image es obligatorio (o usa --self-test)")

    with tempfile.TemporaryDirectory() as workdir:
        tar_path = pathlib.Path(workdir) / "image.tar"
        try:
            export_image(args.image, tar_path)
        except FileNotFoundError:
            print("error: docker no está instalado o no está en PATH", file=sys.stderr)
            return 2
        except subprocess.CalledProcessError as exc:
            print(f"error: no se pudo exportar {args.image}: {exc.stderr.strip()[:200]}", file=sys.stderr)
            return 2
        # `scan_image`, not `scan_tar`: the archive is `docker save`'s layered output, and the
        # secrets are inside the layers.
        findings = scan_image(tar_path, sentinel=args.sentinel)

    if findings:
        print(f"AUDIT FAILED: {len(findings)} finding(s) in {args.image}", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print(f"AUDIT OK: {args.image} — {PLACEHOLDER_FREE}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
