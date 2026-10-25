# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's private vulnerability reporting: the
**Report a vulnerability** button on the repository's
[Security tab](https://github.com/nikitautiu/armada-trackpad-daemon/security). Do not open a public
issue for them. Include what an attacker could do, the steps to reproduce it, and the version or commit.

This is a project maintained in spare time, so there is no fixed response time, but reports are taken
seriously and fixed before anything else.

## What runs as root

The daemon itself runs as your user. Root is involved only here:

- **The root step,** `sudo ./scripts/system-files.sh install` (and `uninstall`). You run it once, from
  your clone, and it runs code from that clone as root. Run it only from a clone that nothing but you
  has changed. It copies InputPlumber configuration to `/etc/inputplumber`, and the device's root units
  and their helpers to `/etc/systemd/system` and `/etc/armada-trackpad`, after checking every unit and
  refusing symlinks.
- **Two oneshot units on the Retroid Pocket Nova,** run from the root-owned copies in
  `/etc/armada-trackpad`, never from your clone, each inside a systemd sandbox:
  - `armada-trackpad-keyboard.service`, at boot and after each InputPlumber restart, restores the
    keyboard output of the device's InputPlumber composite over the system bus.
  - `armada-trackpad-firmware.service`, at boot and after each resume, writes two touchscreen
    registers over i2c. Its helper accepts only the touchscreen's driver and those two registers, and
    the unit can open no device but the i2c nodes.

There is no root daemon, timer or setuid binary. [docs/design.md](docs/design.md#security-model)
describes the full model, including what the daemon reads and how its control socket is protected.

## Supported versions

Only the latest release gets security fixes.
