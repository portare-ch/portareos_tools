#!/usr/bin/env bash
# Install from a checkout or with curl .../install.sh | bash.
set -eo pipefail
work=''
staged=''

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: bash install.sh [--prefix DIR] [--no-deps]

Install nova-push and portare-chd into ~/.local/bin.
Missing dependencies are installed using Homebrew (macOS), apt-get
(Debian/Ubuntu), dnf (Fedora), or pacman (Arch Linux).

  --prefix DIR  Install into DIR/bin instead (DIR must be an absolute path).
  --no-deps     Skip dependency checks and package installation.
  -h, --help    Show this help.
EOF
}

python_ready() {
    command -v python3 >/dev/null 2>&1 &&
        python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1
}

missing_tools() {
    if ! python_ready; then printf '%s\n' python3; fi
    for tool in chdman ffmpeg ssh rsync; do
        if ! command -v "$tool" >/dev/null 2>&1; then printf '%s\n' "$tool"; fi
    done
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        fail "sudo is needed to install dependencies. Install them manually, then use --no-deps."
    fi
}

install_dependencies() {
    local missing manager tool package
    local packages=()
    missing=$(missing_tools)
    [ -n "$missing" ] || return 0

    if [ "$platform" = Darwin ]; then
        command -v brew >/dev/null 2>&1 ||
            fail "Install Homebrew from https://brew.sh and follow its PATH setup instructions, then rerun this installer. Or install dependencies manually and use --no-deps."
        manager=brew
    elif command -v apt-get >/dev/null 2>&1; then
        manager=apt-get
    elif command -v dnf >/dev/null 2>&1; then
        manager=dnf
    elif command -v pacman >/dev/null 2>&1; then
        manager=pacman
    else
        fail "No supported package manager found. Install Python 3.10+, chdman, FFmpeg, SSH and rsync manually, then use --no-deps."
    fi

    # Only install missing tools; keep any working user-managed dependencies.
    for tool in $missing; do
        case "$manager:$tool" in
            brew:python3 | pacman:python3) package=python ;;
            brew:chdman) package=rom-tools ;;
            *:chdman) package=mame-tools ;;
            apt-get:ssh) package=openssh-client ;;
            dnf:ssh) package=openssh-clients ;;
            *:ssh) package=openssh ;;
            dnf:ffmpeg) package=ffmpeg-free ;;
            *) package=$tool ;;
        esac
        packages+=("$package")
    done

    printf 'Installing missing dependencies with %s: %s\n' "$manager" "${packages[*]}"
    case "$manager" in
        brew) brew install "${packages[@]}" ;;
        apt-get)
            as_root apt-get update
            as_root apt-get install -y "${packages[@]}"
            ;;
        dnf) as_root dnf install -y "${packages[@]}" ;;
        pacman) as_root pacman -S --needed --noconfirm "${packages[@]}" ;;
    esac
    hash -r
    missing=$(missing_tools)
    [ -z "$missing" ] ||
        fail "Dependencies are still unavailable: $(printf '%s' "$missing" | tr '\n' ' '). Python must be 3.10 or newer. Check your PATH, then rerun the installer."
}

download() {
    local url=$1 destination=$2
    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --silent --show-error "$url" --output "$destination"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$destination" "$url"
    else
        fail "curl or wget is required to download the tools. Or run install.sh from a full checkout."
    fi
    [ -s "$destination" ] || fail "Download was empty: $url"
}

main() {
    local prefix=${HOME:?HOME must be set}/.local no_deps=false
    local platform bin source_dir='' script_path tool
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --prefix)
                [ "$#" -ge 2 ] && [ -n "$2" ] || fail "--prefix requires a directory."
                prefix=$2
                shift 2
                ;;
            --no-deps) no_deps=true; shift ;;
            -h | --help) usage; return 0 ;;
            *) fail "Unknown option: $1. Use --help for usage." ;;
        esac
    done
    case "$prefix" in
        /*) ;;
        *) fail "--prefix must be an absolute path." ;;
    esac
    platform=$(uname -s)
    case "$platform" in
        Darwin | Linux) ;;
        *) fail "Unsupported platform: $platform. This installer supports macOS and Linux." ;;
    esac
    bin=${prefix%/}/bin
    for tool in nova-push portare-chd; do
        [ ! -d "$bin/$tool" ] || fail "Cannot replace a directory: $bin/$tool"
    done

    # BASH_SOURCE is empty for curl | bash. Never use files from the caller's
    # current directory as a substitute for the downloaded tools.
    script_path=${BASH_SOURCE[0]:-}
    if [ -n "$script_path" ] && [ -f "$script_path" ]; then
        source_dir=$(cd -- "$(dirname -- "$script_path")" && pwd)
    fi
    work=$(mktemp -d "${TMPDIR:-/tmp}/portare-tools.XXXXXX")
    trap 'rm -rf -- "$work"; if [ -n "$staged" ]; then rm -rf -- "$staged"; fi' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if [ -n "$source_dir" ] && [ -f "$source_dir/nova-push" ] && [ -f "$source_dir/portare_chd.py" ]; then
        cp -- "$source_dir/nova-push" "$work/nova-push"
        cp -- "$source_dir/portare_chd.py" "$work/portare-chd"
    else
        printf 'Downloading PortareOS tools...\n'
        download https://raw.githubusercontent.com/portare-ch/portareos_tools/main/nova-push "$work/nova-push"
        download https://raw.githubusercontent.com/portare-ch/portareos_tools/main/portare_chd.py "$work/portare-chd"
    fi

    if [ "$no_deps" = true ]; then
        printf 'Skipping dependency checks and installation (--no-deps).\n'
    else
        install_dependencies
    fi

    mkdir -p -- "$bin"
    staged=$(mktemp -d "$bin/.portare-install.XXXXXX")
    for tool in nova-push portare-chd; do
        install -m 755 "$work/$tool" "$staged/$tool"
    done
    for tool in nova-push portare-chd; do
        mv -f -- "$staged/$tool" "$bin/$tool"
    done
    printf '\nInstalled nova-push and portare-chd in %s\n' "$bin"
    case ":$PATH:" in
        *:"$bin":*) ;;
        *)
            printf '\nAdd this line to ~/.zshrc or ~/.bashrc, then open a new terminal:\n'
            printf '  export PATH=%q:"$PATH"\n' "$bin"
            ;;
    esac
    printf '\nGet started:\n  nova-push --set-host root@device\n  nova-push --help\n  portare-chd --help\n'
}

main "$@"
