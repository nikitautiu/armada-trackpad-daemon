#!/bin/sh
# Install armada-trackpad-daemon for the current user. No root needed; the system files (InputPlumber
# configuration, the device's root units) are a separate, one-time root step (scripts/system-files.sh).
# Stops on a device without a profile (`armada-trackpadd --print-device`).
#
#   ./install.sh [--bootstrap-uv]
#
# --bootstrap-uv  if uv is missing, download a pinned uv release (sha256-verified) to ~/.local/bin
#                 without asking.
set -eu

UV_VERSION=0.11.2
UV_SHA256_aarch64=04792cac761c4a6ba78267f36f2af541b7f92196d42ac55d21d3ff6b0f5ab6a5
UV_SHA256_x86_64=7ac2ca0449c8d68dae9b99e635cd3bc9b22a4cb1de64b7c43716398447d42981

src=$(dirname "$(readlink -f "$0")")
dest=${XDG_DATA_HOME:-$HOME/.local/share}/armada-trackpad-daemon
bin=$HOME/.local/bin
units=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
unit=armada-trackpadd.service
bootstrap=no
[ "${1:-}" = "--bootstrap-uv" ] && bootstrap=yes

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" != 0 ] || die "run as your normal user, not root"

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then UV=$(command -v uv); return; fi
    if [ -x "$bin/uv" ]; then UV=$bin/uv; return; fi
    arch=$(uname -m)
    case $arch in
        aarch64) sum=$UV_SHA256_aarch64 ;;
        x86_64) sum=$UV_SHA256_x86_64 ;;
        *) die "uv not found and no pinned build for $arch; install uv first" ;;
    esac
    if [ "$bootstrap" != yes ]; then
        printf 'uv not found. Download uv %s (sha256-verified) to %s? [y/N] ' "$UV_VERSION" "$bin"
        read -r answer
        case $answer in [yY]*) ;; *) die "uv is required (https://docs.astral.sh/uv/)" ;; esac
    fi
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    name=uv-$arch-unknown-linux-gnu
    curl -fsSL -o "$tmp/uv.tar.gz" "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$name.tar.gz"
    printf '%s  %s\n' "$sum" "$tmp/uv.tar.gz" | sha256sum -c --quiet - || die "uv checksum mismatch"
    tar -xzf "$tmp/uv.tar.gz" -C "$tmp"
    mkdir -p "$bin"
    install -m 0755 "$tmp/$name/uv" "$bin/uv"
    UV=$bin/uv
    say "installed uv $UV_VERSION to $UV"
}

ensure_uv

say "installing to $dest"
mkdir -p "$dest"
rm -rf "$dest/src"
cp -r "$src/src" "$dest/src"
# LICENSE too: pyproject.toml names it, so the build needs it.
for f in pyproject.toml uv.lock README.md LICENSE .python-version; do cp "$src/$f" "$dest/$f"; done
# Use the system Python (3.14 on Armada); never download one.
UV_PYTHON_DOWNLOADS=never "$UV" sync --quiet --frozen --no-dev --no-editable --project "$dest"

# Before the commands and the service: on a device without a profile there is nothing to run.
device=$("$dest/.venv/bin/armada-trackpadd" --print-device) ||
    die "this device is not supported; nothing was enabled (./uninstall.sh removes the copied files)"
device_data=$src/src/armada_trackpad/data/devices/$device
common_data=$src/src/armada_trackpad/data/common

mkdir -p "$bin"
for cmd in armada-trackpadd armada-trackpadctl; do
    ln -sf "$dest/.venv/bin/$cmd" "$bin/$cmd"
done

mkdir -p "$units"
install -m 0644 "$src/systemd/$unit" "$units/$unit"
systemctl --user daemon-reload
# Only the "Created symlink" lines are dropped; errors still show.
systemctl --user enable "$unit" >/dev/null
if systemctl --user is-active --quiet gamescope-session-plus@steam.service; then
    systemctl --user restart "$unit"
    say "started $unit"
else
    say "$unit will start with the next game-mode session"
fi

# Succeeds if a file the root step installs for the device is missing. It installs the device's own
# InputPlumber files and the shared ones, under the same names.
root_step_missing() {
    for data in "$device_data" "$common_data"; do
        for f in "$data"/inputplumber/devices.d/*.yaml "$data"/inputplumber/capability_maps.d/*.yaml; do
            [ -e "$f" ] || continue
            [ -e "/etc/inputplumber/${f#"$data"/inputplumber/}" ] || return 0
        done
    done
    # The firmware unit is left out: `system-files.sh install --no-firmware` skips it on purpose.
    for unit in "$device_data"/system/*.service; do
        [ -e "$unit" ] || continue
        case ${unit##*/} in armada-trackpad-firmware.service) continue ;; esac
        [ -e "/etc/systemd/system/${unit##*/}" ] || return 0
    done
    return 1
}

if root_step_missing; then
    say ""
    say "One-time root step still needed (InputPlumber configuration, keyboard and touchscreen firmware units):"
    say "  sudo $src/scripts/system-files.sh install"
fi
say "done. Try: armada-trackpadctl status"
