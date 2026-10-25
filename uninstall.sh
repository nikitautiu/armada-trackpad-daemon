#!/bin/sh
# Remove armada-trackpad-daemon for the current user (reverse of install.sh). Leaves uv alone.
# The system files are removed separately: sudo scripts/system-files.sh uninstall
set -eu
dest=${XDG_DATA_HOME:-$HOME/.local/share}/armada-trackpad-daemon
units=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
unit=armada-trackpadd.service

[ "$(id -u)" != 0 ] || { echo "uninstall.sh: run as your normal user, not root" >&2; exit 1; }
if [ -e "$units/$unit" ]; then
    systemctl --user disable --now "$unit"
    rm -f "$units/$unit"
fi
systemctl --user daemon-reload
for cmd in armada-trackpadd armada-trackpadctl; do
    link=$HOME/.local/bin/$cmd
    if [ -L "$link" ]; then rm -f "$link"; fi
done
rm -rf "$dest"
echo "removed. If the system files are installed: sudo $(dirname "$(readlink -f "$0")")/scripts/system-files.sh uninstall"
