#!/bin/sh
# Writes the touchscreen controller registers its kernel driver does not keep, and reads each one back.
# Run as root by armada-trackpad-firmware.service, at boot and after every resume, from the copy that
# scripts/system-files.sh installs in /etc/armada-trackpad (docs/design.md, "Touchscreen firmware"):
#
#   firmware.sh REGISTERS
#   firmware.sh --check REGISTERS
#
# REGISTERS has one "DRIVER REGISTER VALUE" line per register, such as "edt_ft5x06 0x85 64": the
# register gets the value, a byte, on the one i2c device bound to DRIVER. "#" starts a comment line.
# The script always exits 0, so it never fails a boot or a resume; problems are logged as warnings.
# --check only validates REGISTERS, and exits 1 if the helper would refuse it; the root step runs it
# before it installs anything.
set -u

i2cget=/usr/bin/i2cget
i2cset=/usr/bin/i2cset
drivers=/sys/bus/i2c/drivers
# After a reset, edt_ft5x06 gives the controller 300 ms before it writes registers; so does this.
settle_s=0.3
# The driver binds before multi-user.target in practice; at most 50 x 0.2 s = 10 s for a slow boot.
bind_tries=50
write_tries=5

say() { printf '%s\n' "$*"; }
# The <4> prefix gives the line warning priority in the journal.
warn() { printf '<4>%s\n' "$*" >&2; }

# Succeeds for the fields of a line this helper writes: the Nova touchscreen's driver, its touch (0x80)
# or move (0x85) threshold, and a decimal value 0..255. Anything else is refused, so a changed registers
# file cannot make root write to another chip or register.
valid() {
    [ $# -eq 3 ] || return 1
    [ "$1" = edt_ft5x06 ] || return 1
    case $2 in 0x80 | 0x85) ;; *) return 1 ;; esac
    case $3 in [0-9] | [1-9][0-9] | 1[0-9][0-9] | 2[0-4][0-9] | 25[0-5]) ;; *) return 1 ;; esac
}

# Prints the one i2c device bound to driver $1 (such as 1-0038) once its /dev/i2c-N node exists.
# Returns 1 while none is bound, 2 if several are, 3 while the node is missing.
bound_device() {
    set -- "$drivers/$1"/[0-9]*-[0-9a-f][0-9a-f][0-9a-f][0-9a-f]
    [ -e "$1" ] || return 1
    [ $# -eq 1 ] || return 2
    name=${1##*/}
    case ${name%-*} in *[!0-9]*) return 1 ;; esac
    [ -e "/dev/i2c-${name%-*}" ] || return 3
    say "$name"
}

# Waits for driver $1's device, then writes value $3 to register $2 until it reads back.
apply() {
    driver=$1 register=$2 value=$3
    tries=0
    while :; do
        device=$(bound_device "$driver") && break
        found=$?
        if [ "$found" -eq 2 ]; then
            warn "several i2c devices are bound to $driver; $register not written"
            return
        fi
        tries=$((tries + 1))
        if [ "$tries" -ge "$bind_tries" ]; then
            if [ "$found" -eq 3 ]; then
                warn "no /dev/i2c node for $driver's bus (is i2c-dev loaded?); $register not written"
            else
                warn "no i2c device is bound to $driver; $register not written"
            fi
            return
        fi
        sleep 0.2
    done
    bus=${device%-*}
    address=$(printf '0x%02x' "0x${device#*-}")
    want=$(printf '0x%02x' "$value")
    sleep "$settle_s"
    was=$("$i2cget" -f -y "$bus" "$address" "$register" b) || was=unreadable
    tries=0
    while [ "$tries" -lt "$write_tries" ]; do
        tries=$((tries + 1))
        "$i2cset" -f -y "$bus" "$address" "$register" "$value" b
        now=$("$i2cget" -f -y "$bus" "$address" "$register" b) || now=unreadable
        if [ "$now" = "$want" ]; then
            say "$device ($driver): $register $was -> $now"
            return
        fi
        sleep 0.2
    done
    warn "$device ($driver): $register reads $now after $write_tries writes of $want (was $was)"
}

check_only=no
if [ "${1:-}" = --check ]; then
    check_only=yes
    shift
fi

# Stops on a registers file the helper refuses: a warning and exit 0 for the unit, exit 1 for --check.
refuse() {
    if [ "$check_only" = yes ]; then
        printf 'firmware.sh: %s\n' "$*" >&2
        exit 1
    fi
    warn "$*; nothing written"
    exit 0
}

registers=${1:-}
[ -r "$registers" ] || refuse "cannot read registers file '$registers'"

# Every line is checked before the first write, so a bad file writes nothing.
todo=
while IFS= read -r line || [ -n "$line" ]; do
    set -f
    # shellcheck disable=SC2086 # split the line into its fields, with globbing off
    set -- $line
    set +f
    case ${1-} in '' | '#'*) continue ;; esac
    valid "$@" || refuse "$registers: bad line '$line'"
    todo="$todo$1 $2 $3
"
done <"$registers"

[ "$check_only" = no ] || exit 0
if [ -z "$todo" ]; then
    say "no registers in $registers"
    exit 0
fi
printf '%s' "$todo" | while read -r driver register value; do
    # The i2c tools must not read the list.
    apply "$driver" "$register" "$value" </dev/null
done
exit 0
