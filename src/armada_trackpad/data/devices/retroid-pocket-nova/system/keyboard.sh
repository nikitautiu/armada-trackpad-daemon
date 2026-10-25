#!/bin/sh
# Puts the keyboard output back on the device's own InputPlumber composite when it is missing, so keys
# InputPlumber owns (volume up on the Nova) keep working. Run as root by armada-trackpad-keyboard.service,
# after armada-controller-type.service, from the copy that scripts/system-files.sh installs in
# /etc/armada-trackpad (docs/design.md, "Keyboard output"):
#
#   keyboard.sh COMPOSITE
#
# COMPOSITE holds the composite's name on its first line, such as "Retroid Pocket Nova". The script
# always exits 0, so it never fails a boot; problems are logged as warnings.
set -u

service=org.shadowblip.InputPlumber
iface=org.shadowblip.Input.CompositeDevice
# Armada's own tool; the variable is only for the tests, the unit never sets it.
controller_type=${ARMADA_TRACKPAD_CONTROLLER_TYPE:-/usr/libexec/armada/controller-type}
# InputPlumber builds the composite right after it starts: at most 50 x 0.2 s = 10 s.
wait_tries=${ARMADA_TRACKPAD_WAIT_TRIES:-50}

say() { printf '%s\n' "$*"; }
# The <4> prefix gives the line warning priority in the journal.
warn() { printf '<4>%s\n' "$*" >&2; }

# Prints the object path of the composite named $1, if InputPlumber has one. Errors are dropped: at boot
# InputPlumber may not be on the bus yet, and the caller retries, then warns.
find_composite() {
    for path in $(busctl --system --list tree "$service" 2>/dev/null | grep -E '/CompositeDevice[0-9]+$'); do
        got=$(busctl --system get-property "$service" "$path" "$iface" Name 2>/dev/null | sed 's/^s "\(.*\)"$/\1/')
        if [ "$got" = "$1" ]; then
            say "$path"
            return 0
        fi
    done
    return 1
}

name_file=${1:-}
name=
[ -r "$name_file" ] && name=$(head -n 1 "$name_file")
if [ -z "$name" ]; then
    warn "no composite name in '$name_file'; keyboard output not checked"
    exit 0
fi

tries=0
until path=$(find_composite "$name"); do
    tries=$((tries + 1))
    if [ "$tries" -ge "$wait_tries" ]; then
        warn "no InputPlumber composite named '$name'; keyboard output not checked"
        exit 0
    fi
    sleep 0.2
done

# Target paths end in their kind and a number, such as .../target/keyboard0. Armada's unit keeps
# keyboard and mouse as extras, so a mouse stays too.
# Unread outputs are never taken for a missing keyboard: restoring it recreates the controller.
if ! targets=$(busctl --system get-property "$service" "$path" "$iface" TargetDevices); then
    warn "$name: could not read the outputs; keyboard output not checked"
    exit 0
fi
extras=
for target in $(printf '%s\n' "$targets" | grep -o '"[^"]*"' | tr -d '"'); do
    kind=$(basename "$target" | sed 's/[0-9]*$//')
    case $kind in
        keyboard)
            say "$name: keyboard output present"
            exit 0
            ;;
        mouse) extras=mouse ;;
    esac
done

kind=deck-uhid
if [ -x "$controller_type" ]; then
    if configured=$("$controller_type" get) && [ -n "$configured" ]; then
        kind=$configured
    else
        warn "$name: could not read the controller type; using $kind"
    fi
fi
# shellcheck disable=SC2086 # extras is empty or the one word "mouse"
set -- "$kind" keyboard $extras
if busctl --system call "$service" "$path" "$iface" SetTargetDevices as "$#" "$@" >/dev/null; then
    say "$name: keyboard output restored ($*)"
else
    warn "$name: could not restore the keyboard output"
fi
exit 0
