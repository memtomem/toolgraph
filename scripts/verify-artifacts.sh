#!/usr/bin/env bash
# Verify the built distributions before anything publishes them.
#
# Three properties, none of which the build itself checks:
#   1. exactly one sdist and one wheel are present;
#   2. the sdist carries what the allowlist in pyproject.toml intends and
#      nothing else -- checked as rules, not as a file list, so adding a module
#      or a contract does not require editing this script;
#   3. the sdist can rebuild the wheel, and rebuilds it byte for byte.
#
# (3) is the one that matters most: it proves the published source archive is
# a usable build input rather than a decorative copy, which is exactly what an
# include allowlist can silently break.
set -euo pipefail

dist="${1:-dist}"

if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d' ' -f1; }
else
  sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
fi

shopt -s nullglob
sdists=("$dist"/*.tar.gz)
wheels=("$dist"/*.whl)
others=()
for f in "$dist"/*; do
  case "$f" in
    *.tar.gz|*.whl) ;;
    *) others+=("$f") ;;
  esac
done
shopt -u nullglob

if [ "${#sdists[@]}" -ne 1 ] || [ "${#wheels[@]}" -ne 1 ]; then
  echo "expected exactly one sdist and one wheel in $dist" >&2
  printf 'found: %s\n' "${sdists[@]}" "${wheels[@]}" >&2
  exit 1
fi
if [ "${#others[@]}" -ne 0 ]; then
  # Everything in this directory is uploaded to the index. A stray file here
  # is a stray file on PyPI.
  echo "unexpected files in $dist -- they would be published:" >&2
  printf '  %s\n' "${others[@]}" >&2
  exit 1
fi

sdist="${sdists[0]}"
wheel="${wheels[0]}"
echo "sdist: $sdist"
echo "wheel: $wheel"

# --- (2) sdist contents -----------------------------------------------------
entries="$(tar tzf "$sdist" | sed 's#^[^/]*/##' | grep -v '^$')"

required="pyproject.toml PKG-INFO README.md LICENSE CHANGELOG.md SECURITY.md"
for name in $required; do
  if ! grep -qxF "$name" <<<"$entries"; then
    echo "sdist is missing $name" >&2
    exit 1
  fi
done
if ! grep -q '^toolgraph/__init__\.py$' <<<"$entries"; then
  echo "sdist is missing the package" >&2
  exit 1
fi

# .gitignore is not in the allowlist; hatchling ships the VCS ignore file
# unconditionally, so it is tolerated rather than allowed.
unexpected="$(grep -vE '^(toolgraph/|contracts/|README\.md$|LICENSE$|CHANGELOG\.md$|SECURITY\.md$|pyproject\.toml$|PKG-INFO$|\.gitignore$)' <<<"$entries" || true)"
if [ -n "$unexpected" ]; then
  echo "sdist carries paths outside the allowlist:" >&2
  printf '  %s\n' $unexpected >&2
  exit 1
fi
if grep -q '^contracts/fixtures/' <<<"$entries"; then
  echo "sdist carries contracts/fixtures/, which is test data" >&2
  exit 1
fi
echo "sdist manifest: $(wc -l <<<"$entries" | tr -d ' ') entries, all allowlisted"

# --- (3) rebuild the wheel from the sdist -----------------------------------
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tar xzf "$sdist" -C "$work"
srcdir="$(find "$work" -mindepth 1 -maxdepth 1 -type d)"
( cd "$srcdir" && uv build --wheel --out-dir "$work/out" >/dev/null )

rebuilt="$(find "$work/out" -name '*.whl')"
if [ "$(sha256 "$rebuilt")" != "$(sha256 "$wheel")" ]; then
  echo "the wheel rebuilt from the sdist differs from the published wheel" >&2
  echo "  published: $(sha256 "$wheel")" >&2
  echo "  rebuilt:   $(sha256 "$rebuilt")" >&2
  exit 1
fi
echo "wheel rebuilds from the sdist byte for byte: $(sha256 "$wheel")"
