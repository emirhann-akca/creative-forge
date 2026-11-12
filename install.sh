#!/bin/sh
# creative-forge installer for macOS (Apple Silicon) and Linux (x86_64). No sudo, no system Python, no git needed.
#
#   curl -LsSf https://raw.githubusercontent.com/emirhann-akca/creative-forge/main/install.sh | sh
#
# What it does, all inside your home directory:
#   1. downloads uv (pinned version, SHA-256 checked against the value in this file) into the creative-forge data dir;
#   2. installs creative-forge[ocr] with `uv tool install` (uv downloads Python if needed) -> `forge` in ~/.local/bin.
#      Source: the release wheel of this installer's version; if that release does not exist (HTTP 404), the source
#      archive of the main branch, which uv builds locally (no git needed). The installer prints which one it used;
#   3. `forge setup ffmpeg` (pinned ffmpeg 9.0.2, SHA-256 checked) and `forge doctor`.
#
# Settings (environment variables):
#   FORGE_SOURCE          install this instead: a wheel path or URL, a source archive URL (.zip / .tar.gz)
#   FORGE_RELEASE_URL     the release wheel to try first (default: this version's wheel on GitHub Releases)
#   FORGE_ARCHIVE_URL     the source archive used when the release wheel is missing (default: the main branch)
#   FORGE_PYTHON          Python version for the tool environment (default 3.13)
#   FORGE_NO_MODIFY_PATH  1 = do not add ~/.local/bin to your shell profile
#   FORGE_NO_SHORTCUT     1 = do not add ~/Applications/Creative Forge.app (macOS) or the app-menu entry (Linux)
set -eu

FORGE_VERSION="0.2.0"
UV_VERSION="0.13.7"
REPO="emirhann-akca/creative-forge"
PYTHON="${FORGE_PYTHON:-3.13}"
WHEEL="creative_forge-$FORGE_VERSION-py3-none-any.whl"
RELEASE_URL="${FORGE_RELEASE_URL:-https://github.com/$REPO/releases/download/v$FORGE_VERSION/$WHEEL}"
ARCHIVE_URL="${FORGE_ARCHIVE_URL:-https://github.com/$REPO/archive/refs/heads/main.zip}"

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

# Prints the HTTP status of downloading $1 into $2 (000 when the server was not reached).
fetch_status() { # url file
    if command -v curl >/dev/null 2>&1; then
        curl --proto '=https' --tlsv1.2 -sSL --retry 3 -o "$2" -w '%{http_code}' "$1" || true
    elif command -v wget >/dev/null 2>&1; then
        if wget -q --https-only -O "$2" "$1"; then echo 200; else
            rc=$?
            if [ "$rc" -eq 8 ]; then echo 404; else echo 000; fi # 8: the server answered with an error
        fi
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

    wheel_dir=""
    if [ -n "${FORGE_SOURCE:-}" ]; then
        source="$FORGE_SOURCE"
        say "source: FORGE_SOURCE=$source"
    else
        wheel_dir=$(mktemp -d)
        status=$(fetch_status "$RELEASE_URL" "$wheel_dir/$WHEEL")
        case "$status" in
            200)
                source="$wheel_dir/$WHEEL"
                say "source: release wheel v$FORGE_VERSION ($RELEASE_URL)"
                ;;
            404)
                source="$ARCHIVE_URL"
                say "no release wheel for v$FORGE_VERSION (HTTP 404 at $RELEASE_URL)"
                say "source: main branch source archive ($source); uv builds it, no git needed"
                ;;
            *) fail "could not download $RELEASE_URL (HTTP $status); check the connection and retry" ;;
        esac
    fi
    case "$source" in
        /*) spec="creative-forge[ocr] @ file://$source" ;;
        *) spec="creative-forge[ocr] @ $source" ;;
    esac
    say "installing creative-forge (Python $PYTHON; uv downloads it if missing)"
    # --refresh-package: a branch archive keeps its URL while its contents change
    "$uv" tool install --force --refresh-package creative-forge --compile-bytecode --python "$PYTHON" "$spec"
    if [ -n "${wheel_dir:-}" ]; then
        rm -rf "$wheel_dir"
    fi

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

    launcher=""
    if [ "${FORGE_NO_SHORTCUT:-0}" != "1" ]; then
        if "$forge" app --install-shortcut; then
            launcher=1
        else
            say "could not add the app launcher; 'forge app' opens the same window"
        fi
    fi
    if [ -n "$launcher" ] && [ "$(uname -s)" = "Darwin" ]; then
        say "done. Open 'Creative Forge' from Launchpad, Spotlight or ~/Applications (or run: forge app)"
    elif [ -n "$launcher" ]; then
        say "done. Open 'Creative Forge' from your applications menu (or run: forge app)"
    else
        say "done. Open a NEW terminal and run:  forge demo   (or: forge app)"
    fi
    case ":$PATH:" in
        *":$bin:"*) ;;
        *) say "until then, run it as: \"$forge\"" ;;
    esac
}

main "$@"
