# armada-trackpad-daemon

Turns a handheld's touchscreen into Steam Deck trackpads for Steam Input on Armada OS:
- **Two pads:** the left and right halves of the screen each drive a virtual trackpad, physically scaled to the Deck's 32.5 mm pad.
- **Pad behaviour per pad:** a fixed pad, a pad centred under each touch (radial menus), an endless swipe surface, or a pad that follows the finger past its edges. Steam Input decides what each pad drives: mouse, joystick, scroll wheel, radial menu and so on.
- **Tap-click:** a tap clicks, and tap-and-drag can be turned on.
- **Toggle:** a key that Steam Input can bind (Insert by default) switches between trackpad mode and the normal touchscreen. A built-in controller chord (L1 + R1 + L3 + R3 on the Nova) does the same without any binding.

It runs as a systemd user service. The touchscreen stays normal until you toggle trackpad mode on. The Decky plugin [decky-armada-trackpad](https://github.com/nikitautiu/decky-armada-trackpad) controls the daemon from Steam's Quick Access menu; `armada-trackpadctl` controls it from a terminal.

> **Status:** this works only on the **Retroid Pocket Nova** running Armada OS; on any other device the install stops.
> It is a personal project tested on that one device. The one-time root step changes system files (InputPlumber
> configuration, two root units, the touchscreen's touch and move thresholds), so read [Install](#install) first;
> `sudo ./scripts/system-files.sh uninstall` removes all of it. `--no-firmware` leaves the touchscreen's registers
> at the panel's own values, and uninstalling restores them. That step runs as root from your clone, so run it only
> from a clone that nothing but you has changed.

[docs/design.md](docs/design.md) explains how it works and why it is built this way.

## Requirements

| Device | OS | InputPlumber | Status |
|---|---|---|---|
| Retroid Pocket Nova | Armada OS, game mode | 0.82, 0.83 | supported and tested |

- **Device:** only the devices in the table. The install stops on any other, and a daemon started anyway reports `device not supported`. [CONTRIBUTING.md](CONTRIBUTING.md#adding-a-device) explains how to add one.
- **Armada OS** in game mode (gamescope), with the controller type set to **Steam Deck** (`deck-uhid`, Armada's default).
- **InputPlumber** 0.82 or 0.83, as Armada ships it. Other versions are marked `(untested)` in `armada-trackpadctl status`, logged once by the daemon, and may work.
- Python 3.12+ (Armada ships 3.14) and [uv](https://docs.astral.sh/uv/). `install.sh` can fetch a pinned, checksum-verified uv if it is missing.

## Install

On the device, as your normal user:

```sh
git clone https://github.com/nikitautiu/armada-trackpad-daemon
cd armada-trackpad-daemon
./install.sh                              # the user service, no root
sudo ./scripts/system-files.sh install    # one time: InputPlumber configuration and root units
```

To leave the touchscreen registers alone, run `sudo ./scripts/system-files.sh install --no-firmware` instead of the second command. The panel then keeps its stock touch and move thresholds; everything else works the same.

**`./install.sh`** (no root):
- checks the device (`armada-trackpadd --print-device`) and stops on one it does not support;
- installs into `~/.local/share/armada-trackpad-daemon`, with its own venv;
- links `armada-trackpadd` and `armada-trackpadctl` into `~/.local/bin`;
- enables the `armada-trackpadd` user service, which runs with Steam's game-mode session.

**`sudo ./scripts/system-files.sh install [--no-firmware] [DEVICE]`** (root, once). It detects the device unless you name it, and stops on one it does not support. Then it:
- copies InputPlumber configuration into `/etc/inputplumber`: a composite device per pad and the tap-click capability maps. The device's own composite keeps InputPlumber's stock configuration;
- installs the device's root units into `/etc/systemd/system`, and their helpers into `/etc/armada-trackpad`. On the Nova:
  - `armada-trackpad-keyboard.service` runs after Armada's controller-type unit, at boot and after each InputPlumber restart. When that unit dropped the keyboard output from the device's composite, it puts it back, so volume up keeps working;
  - `armada-trackpad-firmware.service` writes two touchscreen registers at boot and after each resume, since the panel loses them on every suspend. The **touch threshold** goes from 50 to 35, so light touches register sooner, also for the normal touchscreen. The **move threshold** goes from 128 to 64, so a swipe starts moving sooner: the hold after a touch-down halves, about 50 to 25 ms. Leave it out with `--no-firmware`, which also removes it if it was installed; the panel then keeps its own values;
- restarts InputPlumber, runs the units once, and prints what they did;
- runs `armada-trackpadctl check` for you.

That is the only step that needs root. Afterwards only the root units run as root, for a moment at boot, after each resume (firmware) and after each InputPlumber restart (keyboard). There is no root daemon.

## Steam Input setup

Steam Input binds the toggle per game, in the game's controller layout:

1. Pick a button that is not part of the chord, such as a back grip (L4/R4), and bind it to the keyboard key **Insert**. In Steam's on-screen key picker, Insert is on the **Numpad** tab.
2. For **hold-to-enable** instead of a toggle, give the same button two activators, **Start Press** and **Release Press**, both bound to Insert. Trackpad mode is then on only while the button is held.
3. Set **Left Trackpad** and **Right Trackpad** in the layout to what the game needs (mouse, joystick, scroll wheel, ...).
4. For the trackpads used as a mouse, lower Steam's trackpad **smoothing** and turn **acceleration** off. The pads then follow the finger more directly.

Steam's own UI keeps the trackpads inactive, so test inside a game. The chord (L1 + R1 + L3 + R3, pressed together) toggles without any binding; a press that reaches both the chord and Insert toggles once.

## Use

```sh
armada-trackpadctl status                     # mode, availability, pads, toggle key and chord, InputPlumber version
armada-trackpadctl toggle                     # or: on / off
armada-trackpadctl watch                      # the state, then one line per change, until Ctrl-C
armada-trackpadctl get                        # the settings (`get size` prints one value, `get --defaults` the device defaults)
armada-trackpadctl set --size 1.2 --center-y 0.4 --pads both
armada-trackpadctl set --no-tap
armada-trackpadctl set --right-edges recenter   # right pad: an endless surface for mouse and camera
armada-trackpadctl set --left-anchor float      # left pad: every touch starts at the centre, for radial menus
armada-trackpadctl set --right-edges clamp,clamp,follow,release   # per edge: top, bottom, inner, outer
armada-trackpadctl set --tap-drag             # tap, then touch again to drag with the tap output held
armada-trackpadctl set --toggle-key Pause     # or `none` to disable the key
armada-trackpadctl set --no-toggle-chord      # stop L1+R1+L3+R3 from toggling
armada-trackpadctl set --right-tap-output r5 --right-tap-drag   # taps press R5; bind R5 to Left Mouse Click in Steam Input
armada-trackpadctl check                      # check and repair the InputPlumber wiring
armada-trackpadctl reset                      # back to the device defaults (or: reset size side)
armada-trackpadctl --json status              # machine-readable; --json goes before the command
```

**Settings are runtime only.** The daemon starts from built-in device defaults every time; it reads and writes no config file. `armada-trackpadctl set --help` lists every setting, and [docs/design.md](docs/design.md#settings) explains them.

**Per-pad settings:** the pad behaviour, size, position, `tap`, `tap_drag` and `tap_output` each have a `--left-...` and a `--right-...` form, such as `--left-size`. A pad without its own value uses the shared one. A pad's behaviour is its `anchor` (`fixed`, the default, or `float`) and its `edges` (`clamp`, the default, `follow`, `release` or `recenter`); [docs/design.md](docs/design.md#pad-behaviour) describes each.

Exit codes: 0 ok, 1 the daemon refused (e.g. `unavailable`), 2 usage error, 3 daemon not running (or it went away).

## Troubleshooting

**The pads are unavailable** (`status` says `available  no: ...`, and trackpad mode will not turn on). Run:

```sh
armada-trackpadctl check
```

It re-checks the InputPlumber wiring, repairs it, and prints the result. The daemon only checks when asked, so run it again after restarting InputPlumber yourself. If it still fails, the reason says why:
- **"controller type is ..."**: set the controller type back to Steam Deck in Armada's settings.
- **"composite 'Armada Trackpad Left' not found"**: the root step is missing; run `sudo ./scripts/system-files.sh install`.
- **"InputPlumber call failed"**: InputPlumber is not running.
- **"device not supported: ..."**: this device has no profile; nothing is grabbed or wired.

**Volume up does nothing:** the keyboard output of the device's composite is missing. Check the keyboard unit's log:

```sh
journalctl -b -u armada-trackpad-keyboard
```

**Other logs:**
- the daemon: `journalctl --user -u armada-trackpadd` (an untested InputPlumber version is logged here, and `armada-trackpadctl --json status` shows `inputplumber_version`);
- the touchscreen registers: `journalctl -b -u armada-trackpad-firmware`.

**Diagnostics:** `armada-trackpadctl set --log-level debug` collects frame latency and CPU use; `armada-trackpadctl stats` shows them (`stats --reset` clears them), and the journal gets a summary every 10 s. Set `--log-level info` again when done.

## Talking to the daemon

`armada-trackpadctl` is one client of the daemon's socket. Other clients, such as the [Decky plugin](https://github.com/nikitautiu/decky-armada-trackpad) or a per-game script, can talk to it directly.

The socket is `$XDG_RUNTIME_DIR/armada-trackpad.sock`, mode 0600. It speaks [JSON-RPC 2.0](https://www.jsonrpc.org/specification), one JSON object per line. With `socat`:

```sh
echo '{"jsonrpc":"2.0","id":1,"method":"status"}' | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/armada-trackpad.sock
```

| Method | Params | Result |
|---|---|---|
| `status` | none | mode, availability, pads, toggle key, toggle chord and its node, `protocol`, versions, `instance` |
| `get_settings` / `get_defaults` | none | the settings, or the device defaults |
| `set_settings` | `{"changes": {"size": 1.2, "right.edges": "recenter"}}` | the new settings (all or nothing) |
| `reset_settings` | `{"keys": ["size"]}`, or none for all | the new settings |
| `set_mode` | `{"on": true}` | the new mode, `"on"` or `"off"` |
| `toggle` | none | the new mode |
| `subscribe` | none | the status; then a `state_changed` notification on each change |
| `check` | none | null; the result arrives as a status with a new `checked_at` |
| `get_stats` | none, or `{"reset": true}` | the latency and CPU stats |

For client authors:
- `status` has `protocol`, the control protocol's number (currently 1). It changes only on an incompatible change (a method, param or field removed, renamed or changed in meaning), so check it rather than `version`, the package's version. `inputplumber_version` is InputPlumber's, or null when it could not be read.
- Params are always by name. Errors use the JSON-RPC codes, plus -32000 when mode on is not possible right now (`unavailable`, with the reason as the message).
- To follow the state, keep one connection open: call `subscribe`, then read `state_changed` notifications. Replies and notifications can arrive on the same connection, so match replies by `id`.
- `instance` changes every time the daemon starts. A new value means the daemon is back on its device defaults with mode off.
- The settings object has every key, with `null` for a per-pad override that is not set. Sending it back as `changes` restores it exactly.
- On an unsupported device, `status` answers with `available: false` and the reason, and every other method with `unavailable`.

[docs/design.md](docs/design.md#control-protocol) has the full protocol: framing, every error code and what a status contains.

## Update

From your clone, as your normal user:

```sh
git pull
./install.sh                              # copies the new code; restarts the service if game mode is running
sudo ./scripts/system-files.sh install    # again, so the system files match the new version
```

Running the root step again is safe: it replaces the files it installed, and restarts InputPlumber, so the controller reconnects once. Use `--no-firmware` again if you used it before. [CHANGELOG.md](CHANGELOG.md) lists what changed in each version.

## Uninstall

```sh
./uninstall.sh                                # the user service, commands and venv
sudo ./scripts/system-files.sh uninstall      # the InputPlumber configuration and every root unit
```

The root step's uninstall removes every file it installed, and InputPlumber goes back to its stock configuration. The touchscreen registers return to the panel's own values at the next suspend or reboot. Then delete the clone.

## Reporting a problem

Open an issue at <https://github.com/nikitautiu/armada-trackpad-daemon/issues> with:
- the device, the Armada OS version, and what you did and expected;
- `armada-trackpadctl --json status`;
- the daemon's log: `journalctl --user -u armada-trackpadd -b`;
- for a problem with the root units: `journalctl -b -u armada-trackpad-keyboard -u armada-trackpad-firmware`.

The logs never contain touch positions or typed keys. For a security problem, see [SECURITY.md](SECURITY.md) instead.

## Development

[CONTRIBUTING.md](CONTRIBUTING.md) has the development setup, the checks, the conventions and how to add a device.

Development of this project was assisted by large language models (LLMs); all changes are reviewed and tested by
the maintainer.

## License

MIT, see [LICENSE](LICENSE).
