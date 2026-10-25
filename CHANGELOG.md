# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). The control protocol has its own number, `protocol` in the
status ([docs/design.md](docs/design.md#control-protocol), "Versioning"); an entry says when it changes.

## [Unreleased]

## [0.2.0] - 2026-10-25

The first public release. Control protocol 1.

### Added

- Two virtual Steam Deck trackpads from the left and right halves of the touchscreen, physically scaled
  to the Deck's 32.5 mm pad, wired into InputPlumber's virtual Steam Deck controller.
- Pad behaviour per pad: a fixed or floating window (`anchor`), and per-edge `clamp`, `follow`,
  `release` or `recenter` (`edges`), with an edge grace and an option to keep a floating window on the
  screen.
- Tap-click, tap-and-drag, and tap outputs: a tap can press the pad click or any Deck button or trigger.
- Trackpad mode on and off with a key bound in Steam Input (Insert by default), with the device's
  controller chord (L1 + R1 + L3 + R3 on the Nova), or from a client.
- Focus rules: trackpad mode can follow the app in front, by rules a client sets per app id, including
  `off_locked` to keep it off in Steam's own UI.
- A JSON-RPC 2.0 control socket, with state notifications and the `protocol` number in the status,
  and `armada-trackpadctl`, a CLI for the status, the mode, the settings, the wiring check and stats.
- Runtime-only settings with per-pad overrides; the daemon reads and writes no config file.
- A wiring check that repairs the pad composites after an InputPlumber restart, without reconnecting
  the controller on a daemon restart.
- Latency and CPU statistics at the `debug` log level.
- `install.sh` and `uninstall.sh` for the user service, and a one-time root step,
  `scripts/system-files.sh`, for the InputPlumber configuration and two sandboxed root oneshots: the
  keyboard output of the device's composite, and the touchscreen's touch and move thresholds
  (`--no-firmware` leaves them out).
- Device support: Retroid Pocket Nova on Armada OS, tested with InputPlumber 0.82 and 0.83.

[Unreleased]: https://github.com/nikitautiu/armada-trackpad-daemon/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/nikitautiu/armada-trackpad-daemon/releases/tag/v0.2.0
