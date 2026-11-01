#!/usr/bin/env python3
"""Extract a TLS certificate from Traefik's acme.json.

This script is intended to run on the Docker host that runs Traefik, for
example as root:

    python3 scripts/extract-traefik-cert.py

It reads the certificate for mx.example.com from Traefik's ACME storage,
writes tls.crt/tls.key to ./dms-tls, verifies that the certificate and key
match, and optionally creates the Kubernetes Secret dms-tls.

No private key material is printed to stdout.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterator

DEFAULT_ACME_JSON = "/path/to/traefik/data/acme.json"
DEFAULT_DOMAIN = "mx.example.com"
DEFAULT_OUT_DIR = "./dms-tls"
DEFAULT_NAMESPACE = "mail"
DEFAULT_SECRET_NAME = "dms-tls"


class ExtractError(RuntimeError):
    pass


def iter_cert_entries(value: Any) -> Iterator[dict[str, Any]]:
    """Yield all certificate-like objects from Traefik's JSON structure."""
    if isinstance(value, dict):
        if "certificate" in value and "key" in value:
            domain = value.get("domain") or value.get("Domains") or {}
            if isinstance(domain, dict):
                main = domain.get("main") or domain.get("Main")
                sans = domain.get("sans") or domain.get("SANs") or []
                if isinstance(main, str):
                    yield {
                        "main": main,
                        "sans": [item for item in sans if isinstance(item, str)],
                        "certificate": value["certificate"],
                        "key": value["key"],
                    }
        for child in value.values():
            yield from iter_cert_entries(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_cert_entries(child)


def domain_match_score(name: str, patterns: list[str]) -> int:
    """2 = exact, 1 = wildcard, 0 = no match."""
    name = name.lower().rstrip(".")
    best = 0
    for pattern in patterns:
        pattern = pattern.lower().rstrip(".")
        if not pattern:
            continue
        if pattern == name:
            best = max(best, 2)
            continue
        if pattern.startswith("*."):
            suffix = pattern[1:]
            if name.endswith(suffix):
                prefix = name[: -len(suffix)]
                if prefix and "." not in prefix:
                    best = max(best, 1)
    return best


def entry_score(domain: str, entry: dict[str, Any]) -> int:
    main_score = domain_match_score(domain, [entry["main"]])
    sans_score = domain_match_score(domain, entry["sans"])
    if main_score == 2:
        return 4
    if sans_score == 2:
        return 3
    if main_score == 1:
        return 2
    if sans_score == 1:
        return 1
    return 0


def decode_pem(value: str, label: str) -> bytes:
    try:
        raw = value.strip()
        raw += "=" * (-len(raw) % 4)
        return base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001 - report the field and stop
        raise ExtractError(f"cannot decode {label} from acme.json: {exc}") from exc


def run_openssl(args: list[str], stdin: bytes | None = None) -> str:
    if shutil.which("openssl") is None:
        raise ExtractError("openssl not found in PATH")
    result = subprocess.run(
        ["openssl", *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise ExtractError(f"openssl {' '.join(args)} failed: {message}")
    return result.stdout.decode("utf-8", "replace")


def cert_san_names(cert_path: Path) -> list[str]:
    text = run_openssl(["x509", "-in", str(cert_path), "-noout", "-ext", "subjectAltName"])
    return re.findall(r"DNS:([^,\s]+)", text)


def cert_end_date(cert_path: Path) -> datetime:
    text = run_openssl(["x509", "-in", str(cert_path), "-noout", "-enddate"]).strip()
    value = text.split("=", 1)[1] if "=" in text else text
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = datetime.strptime(re.sub(r"\s+", " ", value), "%b %d %H:%M:%S %Y %Z")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def verify_cert_key(cert_path: Path, key_path: Path, domain: str) -> None:
    cert_pub = run_openssl(["x509", "-in", str(cert_path), "-pubkey", "-noout"]).strip()
    key_pub = run_openssl(["pkey", "-in", str(key_path), "-pubout"]).strip()
    if cert_pub != key_pub:
        raise ExtractError("certificate and private key do not match")

    san_names = cert_san_names(cert_path)
    if domain_match_score(domain, san_names) == 0:
        raise ExtractError(
            f"certificate does not contain {domain}; SANs: {', '.join(san_names) or '(none)'}"
        )

    end_date = cert_end_date(cert_path)
    remaining = end_date - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        raise ExtractError(f"certificate expired at {end_date.isoformat()}")
    if remaining.days < 30:
        print(
            f"WARNING: certificate expires in {remaining.days} days ({end_date.isoformat()})",
            file=sys.stderr,
        )


def write_atomic(path: Path, data: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def create_secret(cert_path: Path, key_path: Path, namespace: str, name: str) -> None:
    if shutil.which("kubectl") is None:
        raise ExtractError("kubectl not found in PATH; cannot create the Secret")

    namespace_manifest = subprocess.run(
        ["kubectl", "create", "namespace", namespace, "--dry-run=client", "-o", "yaml"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if namespace_manifest.returncode != 0:
        raise ExtractError(
            f"kubectl create namespace failed: {namespace_manifest.stderr.strip()}"
        )
    namespace_applied = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=namespace_manifest.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if namespace_applied.returncode != 0:
        raise ExtractError(f"kubectl apply namespace failed: {namespace_applied.stderr.strip()}")

    manifest = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "create",
            "secret",
            "tls",
            name,
            f"--cert={cert_path}",
            f"--key={key_path}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if manifest.returncode != 0:
        raise ExtractError(f"kubectl create secret failed: {manifest.stderr.strip()}")

    applied = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=manifest.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if applied.returncode != 0:
        raise ExtractError(f"kubectl apply failed: {applied.stderr.strip()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a Traefik ACME certificate for the mail server."
    )
    parser.add_argument(
        "--acme-json",
        default=DEFAULT_ACME_JSON,
        help=f"Path to Traefik acme.json (default: {DEFAULT_ACME_JSON})",
    )
    parser.add_argument(
        "--domain",
        default=DEFAULT_DOMAIN,
        help=f"Certificate domain to extract (default: {DEFAULT_DOMAIN})",
    )
    parser.add_argument(
        "--out-dir",
        default=DEFAULT_OUT_DIR,
        help=f"Output directory for tls.crt/tls.key (default: {DEFAULT_OUT_DIR})",
    )
    parser.add_argument(
        "--secret",
        action="store_true",
        help="Also create/update the Kubernetes TLS Secret",
    )
    parser.add_argument(
        "--namespace",
        default=DEFAULT_NAMESPACE,
        help=f"Kubernetes namespace for --secret (default: {DEFAULT_NAMESPACE})",
    )
    parser.add_argument(
        "--secret-name",
        default=DEFAULT_SECRET_NAME,
        help=f"Kubernetes Secret name for --secret (default: {DEFAULT_SECRET_NAME})",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    acme_path = Path(args.acme_json)
    domain = args.domain.lower().rstrip(".")
    out_dir = Path(args.out_dir)

    if not acme_path.exists():
        print(f"ERROR: {acme_path} does not exist", file=sys.stderr)
        print("Hint: run this on the Traefik Docker host, or pass --acme-json", file=sys.stderr)
        return 1

    try:
        data = json.loads(acme_path.read_text(encoding="utf-8"))
    except PermissionError:
        print(f"ERROR: cannot read {acme_path}; run with sudo", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"ERROR: {acme_path} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    entries = list(iter_cert_entries(data))
    matches = [entry for entry in entries if entry_score(domain, entry) > 0]
    if not matches:
        print(f"ERROR: no certificate for {domain} found in {acme_path}", file=sys.stderr)
        if entries:
            print("Available certificate domains:", file=sys.stderr)
            for entry in entries:
                names = [entry["main"], *entry["sans"]]
                print(f"  - {', '.join(names)}", file=sys.stderr)
        else:
            print("No certificates found in acme.json.", file=sys.stderr)
        return 2

    matches.sort(key=lambda entry: entry_score(domain, entry), reverse=True)
    if len(matches) > 1:
        print(
            f"WARNING: {len(matches)} matching certificates found; using the first/highest-priority one.",
            file=sys.stderr,
        )
    selected = matches[0]

    cert_pem = decode_pem(selected["certificate"], "certificate")
    key_pem = decode_pem(selected["key"], "key")

    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    cert_path = out_dir / "tls.crt"
    key_path = out_dir / "tls.key"

    temp_cert_fd, temp_cert_name = tempfile.mkstemp(prefix=".tls.crt.", dir=str(out_dir))
    temp_key_fd, temp_key_name = tempfile.mkstemp(prefix=".tls.key.", dir=str(out_dir))
    temp_cert = Path(temp_cert_name)
    temp_key = Path(temp_key_name)

    try:
        os.fchmod(temp_cert_fd, 0o600)
        os.fchmod(temp_key_fd, 0o600)
        with os.fdopen(temp_cert_fd, "wb") as handle:
            handle.write(cert_pem)
        with os.fdopen(temp_key_fd, "wb") as handle:
            handle.write(key_pem)

        verify_cert_key(temp_cert, temp_key, domain)

        os.replace(temp_cert, cert_path)
        os.replace(temp_key, key_path)
    finally:
        for path in (temp_cert, temp_key):
            if path.exists():
                path.unlink()

    print(f"Certificate: {cert_path}")
    print(f"Private key: {key_path}")
    print(f"Subject/Issuer/Dates:\n{run_openssl(['x509', '-in', str(cert_path), '-noout', '-subject', '-issuer', '-dates']).strip()}")
    print(f"SANs: {', '.join(cert_san_names(cert_path))}")

    if args.secret:
        try:
            create_secret(cert_path, key_path, args.namespace, args.secret_name)
        except ExtractError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            print(
                "Manual command:\n"
                f"  kubectl -n {args.namespace} create secret tls {args.secret_name} "
                f"--cert={cert_path} --key={key_path} "
                "--dry-run=client -o yaml | kubectl apply -f -",
                file=sys.stderr,
            )
            return 1
        print(f"Secret: {args.namespace}/{args.secret_name} created/updated")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExtractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
