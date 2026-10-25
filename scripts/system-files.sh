#!/bin/sh
# One-time root step: install or remove the system files the daemon needs.
#
#   sudo scripts/system-files.sh install [--no-firmware] [DEVICE]
#   sudo scripts/system-files.sh uninstall
#
# DEVICE defaults to the detected device (as `armada-trackpadd --print-device` reports it); an unsupported
# device stops the install before anything is written.
#
# install copies the device's files from src/armada_trackpad/data/devices/DEVICE/, and the shared ones
# from src/armada_trackpad/data/common/ that the device does not override with a file of the same name:
# - inputplumber/devices.d/ (one composite per pad; the device's own composite stays stock) and
#   inputplumber/capability_maps.d/ (tap outputs) to /etc/inputplumber, then restarts InputPlumber;
# - every root unit in system/: the unit to /etc/systemd/system, enabled, and the helpers and data files
#   its Exec*= lines name to /etc/armada-trackpad. The keyboard unit is restarted after Armada's controller
#   type; every other unit runs once now. A unit must be named armada-trackpad-[a-z0-9-]*.service, and its
#   Exec*= lines may run and name only files in /etc/armada-trackpad and under /usr (`check_unit`).
# --no-firmware leaves out armada-trackpad-firmware.service (the touchscreen registers), and removes it
# if an earlier install put it there.
#
# uninstall removes these files for every device, and every armada-trackpad-[a-z0-9-]*.service in
# /etc/systemd/system, so nothing is left behind.
set -eu

here=$(dirname "$(readlink -f "$0")")
src=$(readlink -f "$here/../src")
devices_data=$src/armada_trackpad/data/devices
common_data=$src/armada_trackpad/data/common
# Only for the tests, which install into a scratch tree. Ignored under sudo, so a variable left in the
# environment can never send root's writes elsewhere.
destdir=
[ -n "${SUDO_UID:-}" ] || destdir=${ARMADA_TRACKPAD_DESTDIR:-}
etc=$destdir/etc/inputplumber
# The units run as root, so their helpers and data are root-owned copies, never the checkout.
own_etc_path=/etc/armada-trackpad
own_etc=$destdir$own_etc_path
units=$destdir/etc/systemd/system
keyboard_unit=armada-trackpad-keyboard.service
firmware_unit=armada-trackpad-firmware.service

die() { printf 'system-files.sh: %s\n' "$*" >&2; exit 1; }
usage() { die "usage: sudo $0 install [--no-firmware] [DEVICE] | uninstall"; }

# Prints the command line of each Exec*= line of unit file $1 (ExecStart, ExecStartPre, ExecStop...),
# without the command prefixes (- @ : + !) systemd allows before the path.
exec_commands() {
    sed -n 's/^[[:space:]]*Exec[A-Za-z]*=[[:space:]]*//p' "$1" | sed 's/^[-@:+!]*//'
}

# Prints the file names under /etc/armada-trackpad that the Exec*= lines of unit file $1 name. Only
# plain names count, so a path such as ../x can never reach rm or install.
etc_names() {
    set -f
    # shellcheck disable=SC2046 # split into words on spaces and tabs, with globbing off
    set -- $(exec_commands "$1")
    set +f
    for word; do
        name=${word#"$own_etc_path"/}
        [ "$name" != "$word" ] || continue
        case $name in [A-Za-z0-9_]*) ;; *) continue ;; esac
        case $name in *[!A-Za-z0-9._-]*) continue ;; esac
        printf '%s\n' "$name"
    done
}

# Succeeds if $1 is a unit name this script installs and removes: armada-trackpad-[a-z0-9-]*.service.
valid_unit_name() {
    case $1 in armada-trackpad-*.service) ;; *) return 1 ;; esac
    middle=${1#armada-trackpad-}
    case ${middle%.service} in *[!a-z0-9-]*) return 1 ;; esac
}

# Succeeds if $1 is a root-owned path a unit may run or read: a plain file name in /etc/armada-trackpad,
# or a path under /usr.
allowed_path() {
    case $1 in *[!A-Za-z0-9._/-]* | */../* | */..) return 1 ;; esac
    case $1 in
        "$own_etc_path"/*)
            case ${1#"$own_etc_path"/} in [A-Za-z0-9_]*) ;; *) return 1 ;; esac
            case ${1#"$own_etc_path"/} in */*) return 1 ;; esac
            ;;
        /usr/?*) ;;
        *) return 1 ;;
    esac
}

# Stops the install unless unit file $1 has an allowed name and every Exec*= line runs and names only
# allowed paths (`allowed_path`). The format is strict, so that what this reads is what systemd runs:
# KEY=VALUE with no space before "=", absolute commands, and no line continuation, quotes, escapes,
# % specifiers, $ variables or ";".
check_unit() {
    unit_name=${1##*/}
    valid_unit_name "$unit_name" || die "$unit_name: unit names must match armada-trackpad-[a-z0-9-]*.service"
    if grep -q '\\[[:space:]]*$' "$1"; then die "$unit_name: line continuations are not supported"; fi
    if grep '^[[:space:]]*Exec' "$1" | grep -qv '^[[:space:]]*Exec[A-Za-z]*='; then
        die "$unit_name: an Exec line is not KEY=VALUE"
    fi
    if exec_commands "$1" | grep -q "[\"'\\\$%;]"; then
        die "$unit_name: quotes, escapes, specifiers, variables and ';' are not supported in Exec lines"
    fi
    set -f
    while read -r command arguments; do
        [ -n "$command" ] || continue
        case $command in /*) ;; *) die "$unit_name: Exec runs '$command', not an absolute path" ;; esac
        # shellcheck disable=SC2086 # split the arguments into words, with globbing off
        for word in "$command" $arguments; do
            case $word in
                */*) allowed_path "$word" || die "$unit_name: Exec names '$word', outside $own_etc_path/ and /usr/" ;;
            esac
        done
    done <<EOF
$(exec_commands "$1")
EOF
    set +f
}

# Prints the file of device folder $1 installed as /etc/armada-trackpad/$2: system/$2 if there is one,
# else the one file named $2 elsewhere in the folder.
source_of() {
    case $2 in '' | *[!A-Za-z0-9._-]*) die "unit names a bad file '$2'" ;; esac
    if [ -f "$1/system/$2" ]; then
        printf '%s\n' "$1/system/$2"
        return
    fi
    found=$(find "$1" -path "$1/system" -prune -o -type f -name "$2" -print)
    [ -n "$found" ] || die "no file '$2' in $1"
    [ "$(printf '%s\n' "$found" | wc -l)" -eq 1 ] || die "several files named '$2' in $1"
    printf '%s\n' "$found"
}

# Succeeds if unit file $1 is one that install puts in place.
wanted() {
    [ -f "$1" ] || return 1
    [ "$no_firmware" = no ] || [ "${1##*/}" != "$firmware_unit" ]
}

# Prints the journal lines of the latest run of unit $1.
show_run() {
    run=$(systemctl show --property=InvocationID --value "$1")
    # The journal lines are only shown for information: a journal that cannot be read must not fail the install.
    [ -z "$run" ] || journalctl --quiet --no-pager --output=cat "_SYSTEMD_INVOCATION_ID=$run" || true
}

# Disables and removes unit $1 if it is there, with the /etc/armada-trackpad files named by it and by any
# other unit file given as $2..., except the names in $keep_names, which installed units still use.
remove_unit() {
    unit=$1
    shift
    names=
    for file in "$units/$unit" "$@"; do
        if [ -f "$file" ]; then names="$names $(etc_names "$file")"; fi
    done
    if [ -e "$units/$unit" ]; then
        # One unit that cannot be disabled must not leave the others in place.
        systemctl disable --quiet --now "$unit" || echo "warning: could not disable $unit; removing it anyway" >&2
        rm -f "$units/$unit"
        systemctl daemon-reload
    fi
    for name in $names; do
        case " $keep_names " in *" $name "*) continue ;; esac
        rm -f "${own_etc:?}/$name"
    done
}

# Prints the id of the detected device. devices.py needs only the standard library, so it runs on the
# system Python from the checkout; -I keeps the environment and the user's site-packages out of a root
# process, and -S every other site-packages too.
detect_device() {
    python3 -I -S "$src/armada_trackpad/devices.py" || die "this device is not supported; nothing was installed"
}

# Restarting InputPlumber also restarts Armada's controller-type unit and the keyboard unit, which both
# require it; the keyboard unit is restarted once more after the controller type, so this waits for it.
restart() {
    systemctl restart inputplumber
    if [ -e "$units/$keyboard_unit" ]; then
        systemctl restart "$keyboard_unit" || echo "warning: $keyboard_unit failed; see journalctl -u $keyboard_unit" >&2
        show_run "$keyboard_unit"
    fi
}

# The restart leaves the daemon "unavailable" until a client runs `check`, so run it as the invoking
# user, best-effort: the install never fails here, and the user is told why and what to run if it did not
# pass. setsid gives the check a session without root's terminal and stdin, so nothing it runs can type
# into that terminal. `command -v` only asks whether the commands exist, so its output is dropped.
check_daemon() {
    retry="as your user, run: armada-trackpadctl check"
    if [ -z "${SUDO_USER:-}" ] || [ -z "${SUDO_UID:-}" ] || ! command -v runuser >/dev/null 2>&1 ||
        ! command -v setsid >/dev/null 2>&1; then
        echo "note: could not run the check for you; $retry" >&2
        return 0
    fi
    home=$(getent passwd "$SUDO_USER" | cut -d: -f6)
    status=0
    setsid -w runuser -u "$SUDO_USER" -- env "XDG_RUNTIME_DIR=/run/user/$SUDO_UID" \
        "PATH=$home/.local/bin:$PATH" armada-trackpadctl check </dev/null || status=$?
    # The exit statuses are armada-trackpadctl's: 1 the check ran and failed, 3 no daemon to ask.
    case $status in
        0) ;;
        1) echo "note: the check did not pass (see above); once that is fixed, $retry" >&2 ;;
        3) echo "note: the daemon is not running yet; it starts with the game-mode session, then $retry" >&2 ;;
        *) echo "note: could not run the check for you (exit $status); $retry" >&2 ;;
    esac
}

# Prints the files of InputPlumber folder $2 (devices.d or capability_maps.d) for device folder $1: each
# *.yaml of the device's own folder, and each shared one the device has no file of that name for.
inputplumber_files() {
    for f in "$common_data/inputplumber/$2"/*.yaml; do
        [ -e "$f" ] || continue
        [ -e "$1/inputplumber/$2/${f##*/}" ] || printf '%s\n' "$f"
    done
    for f in "$1/inputplumber/$2"/*.yaml; do
        [ -e "$f" ] || continue
        printf '%s\n' "$f"
    done
}

# Stops the install if data folder $1 is reached through a symlink or holds one: root copies these
# files, and a link could make it copy any file on the system.
check_no_symlinks() {
    [ "$(readlink -f "$1")" = "$1" ] || die "$1 is a symlink or inside one; nothing was installed"
    link=$(find "$1" -type l -print | head -n 1)
    [ -z "$link" ] || die "$link is a symlink; nothing was installed"
}

install_device() {
    device_data=$devices_data/$1
    check_no_symlinks "$device_data"
    if [ -d "$common_data" ]; then check_no_symlinks "$common_data"; fi
    # Every unit is checked and every file found before the first one is written, so a broken device
    # folder changes nothing.
    for unit in "$device_data"/system/*.service; do
        [ -e "$unit" ] || continue
        check_unit "$unit"
        wanted "$unit" || continue
        for name in $(etc_names "$unit"); do
            source_of "$device_data" "$name" >/dev/null
        done
    done
    # The firmware helper refuses a registers file only when it runs as root at boot; checking it now
    # makes a bad file stop the install instead.
    if wanted "$device_data/system/$firmware_unit"; then
        registers=$(source_of "$device_data" registers)
        /bin/sh "$device_data/system/firmware.sh" --check "$registers" || die "bad registers file; nothing was installed"
    fi
    for dir in devices.d capability_maps.d; do
        while read -r f; do
            [ -n "$f" ] || continue
            install -D -m 0644 -o root -g root "$f" "$etc/$dir/${f##*/}"
        done <<EOF
$(inputplumber_files "$device_data" "$dir")
EOF
    done
    installed=
    for unit in "$device_data"/system/*.service; do
        wanted "$unit" || continue
        for name in $(etc_names "$unit"); do
            mode=0644
            case $name in *.sh) mode=0755 ;; esac
            install -D -m "$mode" -o root -g root "$(source_of "$device_data" "$name")" "$own_etc/$name"
            keep_names="$keep_names $name"
        done
        install -D -m 0644 -o root -g root "$unit" "$units/${unit##*/}"
        installed="$installed ${unit##*/}"
    done
    systemctl daemon-reload
    for unit in $installed; do
        systemctl enable --quiet "$unit"
    done
    if [ "$no_firmware" = yes ]; then
        remove_unit "$firmware_unit"
    fi
    restart
    for unit in $installed; do
        if [ "$unit" = "$keyboard_unit" ]; then continue; fi
        systemctl start "$unit" || echo "warning: $unit failed; see journalctl -u $unit" >&2
        show_run "$unit"
    done
    echo "installed the InputPlumber configuration and the root units for $1:$installed"
}

uninstall_all() {
    for device_data in "$common_data" "$devices_data"/*/; do
        for dir in devices.d capability_maps.d; do
            for f in "$device_data/inputplumber/$dir"/*.yaml; do
                [ -e "$f" ] || continue
                rm -f "$etc/$dir/$(basename "$f")"
            done
        done
    done
    # Only our folders, and only when left empty: others hold files that are not ours, and
    # /etc/inputplumber itself belongs to InputPlumber.
    rmdir "$etc/devices.d" "$etc/capability_maps.d" 2>/dev/null || true
    # While the keyboard unit is still there, so this restart keeps the keyboard output.
    restart
    echo "removed the InputPlumber configuration"
    for unit in "$units"/armada-trackpad-*.service "$devices_data"/*/system/*.service; do
        [ -f "$unit" ] || continue
        if ! valid_unit_name "${unit##*/}"; then
            echo "warning: ${unit##*/} is not a name this script installs; left alone" >&2
            continue
        fi
        remove_unit "${unit##*/}" "$devices_data"/*/system/"${unit##*/}"
    done
    if [ -d "$own_etc" ]; then
        rmdir "$own_etc" || echo "warning: $own_etc_path still holds files that no unit names" >&2
    fi
    echo "removed the root units; the touchscreen registers return to their defaults at the next suspend or boot"
}

keep_names=
action=${1:-}
[ $# -gt 0 ] && shift
no_firmware=no
device=
for arg; do
    case $arg in
        --no-firmware) [ "$action" = install ] || usage; no_firmware=yes ;;
        -*) usage ;;
        *) [ -z "$device" ] && [ "$action" = install ] || usage; device=$arg ;;
    esac
done

case $action in
    install)
        [ "$(id -u)" = 0 ] || die "run with sudo"
        [ -n "$device" ] || device=$(detect_device)
        case $device in *[!a-z0-9-]*) die "invalid device id '$device'" ;; esac
        [ -d "$devices_data/$device" ] || die "no data for device '$device'"
        install_device "$device"
        check_daemon
        ;;
    uninstall)
        [ "$(id -u)" = 0 ] || die "run with sudo"
        uninstall_all
        ;;
    *)
        usage
        ;;
esac
