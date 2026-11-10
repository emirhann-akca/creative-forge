#!/bin/sh
# creative-forge installer for macOS (Apple Silicon) and Linux (x86_64). No sudo, no system Python needed.
#
#   curl -LsSf https://github.com/emirhann-akca/creative-forge/releases/latest/download/install.sh | sh
#
# What it does, all inside your home directory:
#   1. downloads uv (pinned version, SHA-256 checked against the value in this file) into the creative-forge data dir;
#   2. `uv tool install creative-forge[ocr]` (uv downloads Python if needed) -> the `forge` command in ~/.local/bin;
#   3. `forge setup ffmpeg` (pinned ffmpeg 9.0.2, SHA-256 checked) and `forge doctor`.
#
# Settings (environment variables):
#   FORGE_SOURCE          what to install instead of the release wheel: a wheel path or URL, or a git+https URL
#   FORGE_PYTHON          Python version for the tool environment (default 3.13)
#   FORGE_NO_MODIFY_PATH  1 = do not add ~/.local/bin to your shell profile
set -eu

FORGE_VERSION="0.2.0"
UV_VERSION="0.13.7"
REPO="emirhann-akca/creative-forge"
PYTHON="${FORGE_PYTHON:-3.13}"
SOURCE="${FORGE_SOURCE:-https://github.com/$REPO/releases/download/v$FORGE_VERSION/creative_forge-$FORGE_VERSION-py3-none-any.whl}"

say() { printf 'forge-install: %s\n' "$*"; }
fail() {
    printf 'forge-install: error: %s\n' "$*" >&2
    exit 1
}

download() { # url file
    if command -v curl >/dev/null 2>&1; then
        curl --proto '=https' --tlsv1.2 -fsSL --retry 3 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --https-only -O "$2" "$1"
    else
        fail "need curl or wget"
    fi
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d ' ' -f 1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d ' ' -f 1
    else
        fail "need sha256sum or shasum to verify downloads"
    fi
}

main() {
    os=$(uname -s)
    arch=$(uname -m)
    case "$os/$arch" in
        Darwin/arm64)
            target="aarch64-apple-darwin"
            uv_sha="81b811b57e4624df7ed3aac148e0359100d9962f3d9dce00aec533dc2e086731"
            data_dir="$HOME/Library/Application Support/creative-forge"
            ;;
        Linux/x86_64)
            target="x86_64-unknown-linux-gnu"
            uv_sha="c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83"
            data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/creative-forge"
            ;;
        Darwin/x86_64) fail "Intel Macs are not supported by this installer (no pinned ffmpeg build); see the README" ;;
        *) fail "unsupported platform $os/$arch (supported: macOS arm64, Linux x86_64; Windows: install.ps1)" ;;
    esac
    if [ -n "${CREATIVE_FORGE_HOME:-}" ]; then
        data_dir="$CREATIVE_FORGE_HOME/data"
    fi

    uv_dir="$data_dir/uv/$UV_VERSION"
    uv="$uv_dir/uv"
    if [ -x "$uv" ] && "$uv" --version 2>/dev/null | grep -q "^uv $UV_VERSION"; then
        say "using uv $UV_VERSION from $uv_dir"
    else
        say "downloading uv $UV_VERSION ($target)"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$target.tar.gz" "$tmp/uv.tar.gz"
        got=$(sha256_of "$tmp/uv.tar.gz")
        [ "$got" = "$uv_sha" ] || fail "uv download SHA-256 mismatch (got $got, expected $uv_sha); nothing was installed"
        tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
        mkdir -p "$uv_dir"
        cp "$tmp/uv-$target/uv" "$uv_dir/uv"
        chmod 755 "$uv_dir/uv"
    fi

    case "$SOURCE" in
        /*) spec="creative-forge[ocr] @ file://$SOURCE" ;;
        *) spec="creative-forge[ocr] @ $SOURCE" ;;
    esac
    say "installing creative-forge from $SOURCE (Python $PYTHON; uv downloads it if missing)"
    "$uv" tool install --force --compile-bytecode --python "$PYTHON" "$spec"

    bin=$("$uv" tool dir --bin)
    if [ "${FORGE_NO_MODIFY_PATH:-0}" != "1" ] && [ "${UV_NO_MODIFY_PATH:-0}" != "1" ]; then
        "$uv" tool update-shell >/dev/null 2>&1 || say "could not update your shell profile; add $bin to PATH"
    fi
    forge="$bin/forge"
    [ -x "$forge" ] || fail "forge was not installed into $bin"

    say "setting up ffmpeg"
    "$forge" setup ffmpeg || fail "forge setup ffmpeg failed (see above)"
    say "checking the installation"
    "$forge" doctor || fail "forge doctor found a blocking problem (see above)"

    say "done. Open a NEW terminal and run:  forge demo   (or: forge ui)"
    case ":$PATH:" in
        *":$bin:"*) ;;
        *) say "until then, run it as: \"$forge\"" ;;
    esac
}

main "$@"
