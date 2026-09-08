#!/usr/bin/env python3
"""Read-only verification of the exact two published release distributions."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import urlopen

INDEXES = {"testpypi": "https://test.pypi.org", "pypi": "https://pypi.org"}
MAX_BYTES = 20 * 1024 * 1024


def fetch(url: str) -> bytes:
    with urlopen(url, timeout=30) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("index response or distribution exceeds 20 MiB verification bound")
    return body


def verify(version: str, dist: Path, metadata: dict, download=fetch) -> dict:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("expected a final three-part release version")
    if metadata.get("info", {}).get("name") != "toolgraph" or metadata.get("info", {}).get("version") != version:
        raise ValueError("index project/version differs from the candidate")
    expected = {f"toolgraph-{version}-py3-none-any.whl", f"toolgraph-{version}.tar.gz"}
    rows = metadata.get("urls", [])
    if len(rows) != 2 or {row.get("filename") for row in rows} != expected:
        raise ValueError("index must contain exactly the expected wheel and sdist")
    actual = {p.name for p in dist.iterdir() if p.name != ".gitignore"}
    if actual != expected:
        raise ValueError("local distribution directory differs from expected files")
    evidence = {}
    for row in rows:
        name = row["filename"]
        if row.get("yanked"):
            raise ValueError("published release is yanked")
        url = urlsplit(row["url"])
        if (url.scheme != "https" or not (url.hostname or "").endswith(".pythonhosted.org")
                or url.username is not None or url.password is not None or url.port not in (None, 443)):
            raise ValueError("distribution URL is not an HTTPS Python package host")
        digest = hashlib.sha256((dist / name).read_bytes()).hexdigest()
        if row.get("digests", {}).get("sha256") != digest:
            raise ValueError(f"published digest differs from local candidate: {name}")
        if hashlib.sha256(download(row["url"])).hexdigest() != digest:
            raise ValueError(f"downloaded bytes differ from published digest: {name}")
        evidence[name] = digest
    return {"status": "pass", "version": version, "sha256": evidence}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", choices=INDEXES, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", args.version):
        parser.error("expected a final three-part release version")
    try:
        metadata = json.loads(fetch(f"{INDEXES[args.index]}/pypi/toolgraph/{args.version}/json"))
        result = verify(args.version, args.dist, metadata)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Release verification failed: {exc}\n")
    result["index"] = args.index
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
