#!/usr/bin/env bash
# Install the Renode portable (self-contained) Linux tarball. No system mono/dotnet
# required — the portable bundle ships its own runtime.
#
# Usage: install_renode.sh [version]
# Installs to $RENODE_HOME (default: $HOME/.renode/<version>) and prints the
# path to the `renode` launcher on the last line.
set -euo pipefail

VERSION="${1:-1.16.1}"
RENODE_ROOT="${RENODE_ROOT:-$HOME/.renode}"
DEST="$RENODE_ROOT/$VERSION"
LAUNCHER="$DEST/renode"

if [ -x "$LAUNCHER" ]; then
  echo "renode $VERSION already installed" >&2
  echo "$LAUNCHER"
  exit 0
fi

mkdir -p "$RENODE_ROOT"
# Prefer the self-contained "portable" build (bundles mono). Fall back to the
# dotnet portable build if the mono one is unavailable for the version.
TARBALL="renode-${VERSION}.linux-portable.tar.gz"
URL="https://github.com/renode/renode/releases/download/v${VERSION}/${TARBALL}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# curl, wget, or python3 -- the zmkfirmware/zmk-build-arm:stable CI container
# has NEITHER curl nor wget, but always has python3 (west needs it).
fetch() {
  if command -v curl >/dev/null 2>&1; then
    curl -fSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$1" "$2" <<'PYEOF'
import sys, urllib.request
url, dest = sys.argv[1], sys.argv[2]
try:
    urllib.request.urlretrieve(url, dest)
except Exception as err:
    print(f"download failed: {err}", file=sys.stderr)
    sys.exit(1)
PYEOF
  else
    echo "ERROR: none of curl/wget/python3 are available" >&2
    return 1
  fi
}

echo "downloading $URL" >&2
if ! fetch "$URL" "$TMP/$TARBALL"; then
  TARBALL="renode-${VERSION}.linux-portable-dotnet.tar.gz"
  URL="https://github.com/renode/renode/releases/download/v${VERSION}/${TARBALL}"
  echo "falling back to $URL" >&2
  fetch "$URL" "$TMP/$TARBALL"
fi

mkdir -p "$DEST"
tar -xzf "$TMP/$TARBALL" -C "$DEST" --strip-components=1

if [ ! -x "$LAUNCHER" ]; then
  # Some bundles name the launcher differently; find it.
  LAUNCHER="$(find "$DEST" -maxdepth 2 -name 'renode' -type f | head -1)"
fi
[ -x "$LAUNCHER" ] || { echo "ERROR: renode launcher not found under $DEST" >&2; exit 1; }

echo "installed renode $VERSION -> $LAUNCHER" >&2
echo "$LAUNCHER"
