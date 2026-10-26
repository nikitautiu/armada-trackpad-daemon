# armada-trackpad-daemon

armada-trackpad-daemon turns a handheld's touchscreen into Steam Deck trackpads. The left and right halves of the
screen each act as one of the Deck's trackpads, sized like the real 32.5 mm pad, and Steam Input maps them to a
mouse, joystick, scroll wheel or radial menu. A tap clicks. A key or a controller chord switches back to the
normal touchscreen.

It runs as a systemd user service. The Decky plugin
[decky-armada-trackpad](https://github.com/nikitautiu/decky-armada-trackpad) controls it from Steam's Quick Access
menu, and `armada-trackpadctl` from a terminal.

> **Supported:** Retroid Pocket Nova on Armada OS (game mode). It's tested on that device only, so other devices
> and setups may not work, and the installer stops on any other device. The one-time root step writes
> InputPlumber configuration, two root units and two touchscreen registers. Run it only from a clone that
> nobody but you has changed. `sudo ./scripts/system-files.sh uninstall` removes all of it.

## Install

On the device, as your normal user:

```sh
git clone https://github.com/nikitautiu/armada-trackpad-daemon
cd armada-trackpad-daemon
./install.sh                              # the user service, no root
sudo ./scripts/system-files.sh install    # one time: InputPlumber configuration and root units
```

Add `--no-firmware` to the second command to keep the touchscreen's own touch and move thresholds.
[docs/usage.md](docs/usage.md#installed-files) lists every file and unit the two steps install.

## Use

1. Install the [Decky plugin](https://github.com/nikitautiu/decky-armada-trackpad), or use `armada-trackpadctl`.
2. In a game's controller layout, bind a button to the key **Insert** (on the **Numpad** tab of Steam's key
   picker) and set **Left Trackpad** and **Right Trackpad** to what the game needs. Without a binding, press
   L1 + R1 + L3 + R3 together to switch.
3. Press the button in the game to switch between trackpad mode and the touchscreen.

```sh
armada-trackpadctl status                 # mode, availability, pads, toggle key and chord
armada-trackpadctl toggle                 # or: on / off
armada-trackpadctl check                  # check and repair the InputPlumber wiring
```

[docs/usage.md](docs/usage.md) has the Steam Input tips, every command and setting, troubleshooting and the control
socket. [docs/design.md](docs/design.md) explains how the daemon works.

## Update

```sh
git pull
./install.sh
sudo ./scripts/system-files.sh install    # add --no-firmware again if you used it
```

## Uninstall

```sh
./uninstall.sh
sudo ./scripts/system-files.sh uninstall
```

Then delete the clone. The touchscreen registers return to the panel's own values at the next suspend or reboot.

## Reporting a problem

Open an [issue](https://github.com/nikitautiu/armada-trackpad-daemon/issues) with the device, the Armada OS
version, what you did and what you expected, plus:

- `armada-trackpadctl --json status`
- `journalctl --user -u armada-trackpadd -b`
- for the root units: `journalctl -b -u armada-trackpad-keyboard -u armada-trackpad-firmware`

The logs never contain touch positions or typed keys. Report security problems as [SECURITY.md](SECURITY.md) says.

[Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · MIT [license](LICENSE) · Large language models (LLMs)
assisted the development of this project. The maintainer reviews and tests every change.
