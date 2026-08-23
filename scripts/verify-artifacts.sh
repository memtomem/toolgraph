#!/usr/bin/env bash
# Verify the built distributions before anything publishes them.
#
#   scripts/verify-artifacts.sh [dist-dir] [version]
#
# Checks, in order:
#   1. `dist/` holds exactly the two files the release is supposed to upload,
#      under exactly the names they must have. Everything in that directory is
#      handed to the publisher, and the filenames travel into later steps, so a
#      surprising name is rejected here rather than carried forward.
#   2. the sdist carries what the allowlist in pyproject.toml intends and
#      nothing else -- as rules, so adding a module or a contract needs no edit
#      here -- and every member is a plain file with no duplicates.
#   3. the sdist rebuilds the wheel, byte for byte, in this environment.
#
# On (3): this is a same-runner comparison. It shows the published source
# archive is a faithful build input -- exactly what an include allowlist can
# silently break -- not that the build is reproducible across machines.
set -euo pipefail
shopt -s nullglob dotglob

dist="${1:-dist}"
version="${2:-}"

if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d' ' -f1; }
else
  sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
fi

die() { echo "$*" >&2; exit 1; }

# --- (1) exactly the two expected files -------------------------------------
if [ -z "$version" ]; then
  # Derived only when the caller did not say. The workflow always says, so the
  # release path never trusts a filename to tell it what the version is.
  candidates=("$dist"/*.tar.gz)
  [ "${#candidates[@]}" -eq 1 ] || die "cannot derive a version: ${#candidates[@]} sdists in $dist"
  base="$(basename "${candidates[0]}")"
  version="${base#toolgraph-}"
  version="${version%.tar.gz}"
fi

sdist="$dist/toolgraph-$version.tar.gz"
wheel="$dist/toolgraph-$version-py3-none-any.whl"
[ -f "$sdist" ] || die "missing $sdist"
[ -f "$wheel" ] || die "missing $wheel"

# `uv build` writes dist/.gitignore; it is a build-tool artifact, not a
# distribution, and is never uploaded. Anything else here is refused.
for f in "$dist"/*; do
  case "$(basename "$f")" in
    "toolgraph-$version.tar.gz"|"toolgraph-$version-py3-none-any.whl"|.gitignore) ;;
    *) die "unexpected file in $dist, which is what gets published: $f" ;;
  esac
  # -f follows symlinks, so a link named like a distribution would pass.
  [ ! -L "$f" ] || die "symlink in $dist: $f"
  [ -f "$f" ] || die "not a regular file: $f"
done

echo "sdist: $sdist"
echo "wheel: $wheel"

# --- (2) sdist contents -----------------------------------------------------
roots="$(tar tzf "$sdist" | cut -d/ -f1 | sort -u)"
[ "$roots" = "toolgraph-$version" ] || die "sdist root is not toolgraph-$version: $roots"

listing="$(tar tvzf "$sdist")"
# `d` is allowed (tar archives may carry directory entries); everything else
# -- `l`, `h`, `c`, `b`, `p`, `s` -- is refused.
# Deliberately not `grep -v … | grep -q .`: under `pipefail` the second grep
# exits on its first match, the first grep takes SIGPIPE, and the pipeline
# reports 141 -- so an `if` on it reads "clean" precisely when there are many
# offending members. Capture, then test the string.
set +e
irregular="$(grep -vE '^[-d]' <<<"$listing")"
status=$?
set -e
[ "$status" -le 1 ] || die "grep failed while inspecting the sdist member types"
if [ -n "$irregular" ]; then
  echo "sdist contains non-regular members (symlink, device, hard link):" >&2
  printf '%s\n' "$irregular" >&2
  exit 1
fi

entries="$(tar tzf "$sdist" | sed 's#^[^/]*/##' | grep -v '^$' | grep -v '/$')"
dupes="$(sort <<<"$entries" | uniq -d)"
[ -z "$dupes" ] || die "sdist has duplicate members:"$'\n'"$dupes"

for name in pyproject.toml PKG-INFO README.md LICENSE CHANGELOG.md SECURITY.md; do
  grep -qxF "$name" <<<"$entries" || die "sdist is missing $name"
done
grep -qxF 'toolgraph/__init__.py' <<<"$entries" || die "sdist is missing the package"

# Mirrors [tool.hatch.build.targets.sdist] in pyproject.toml. Keep the two in
# step: this is a second statement of the same intent, and the release depends
# on it being the same intent.
allowed='^(toolgraph/|contracts/|README\.md$|LICENSE$|CHANGELOG\.md$|SECURITY\.md$|pyproject\.toml$|PKG-INFO$|\.gitignore$)'
# .gitignore is tolerated, not allowed: hatchling ships the VCS ignore file
# unconditionally and an exclude entry for it has no effect.
set +e
unexpected="$(grep -vE "$allowed" <<<"$entries")"
status=$?
set -e
[ "$status" -le 1 ] || die "grep failed while checking the sdist manifest"
if [ -n "$unexpected" ]; then
  echo "sdist carries paths outside the allowlist:" >&2
  printf '  %s\n' "$unexpected" >&2
  exit 1
fi
set +e
grep -q '^contracts/fixtures/' <<<"$entries"
status=$?
set -e
[ "$status" -ne 0 ] || die "sdist carries contracts/fixtures/, which is test data"
[ "$status" -le 1 ] || die "grep failed while checking for test fixtures"
echo "sdist manifest: $(grep -c . <<<"$entries") entries, all allowlisted"

# --- (3) rebuild the wheel from the sdist -----------------------------------
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tar xzf "$sdist" -C "$work"

srcdirs=("$work"/*/)
[ "${#srcdirs[@]}" -eq 1 ] || die "sdist does not unpack to a single directory (${#srcdirs[@]})"
( cd "${srcdirs[0]}" && uv build --wheel --out-dir "$work/out" >/dev/null )

rebuilt=("$work"/out/*.whl)
[ "${#rebuilt[@]}" -eq 1 ] || die "rebuild produced ${#rebuilt[@]} wheels"

published_digest="$(sha256 "$wheel")"
rebuilt_digest="$(sha256 "${rebuilt[0]}")"
if [ "$published_digest" != "$rebuilt_digest" ]; then
  echo "the wheel rebuilt from the sdist differs from the built wheel" >&2
  echo "  built:   $published_digest" >&2
  echo "  rebuilt: $rebuilt_digest" >&2
  exit 1
fi
echo "wheel rebuilds from the sdist in this environment: $published_digest"
