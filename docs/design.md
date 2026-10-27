# armada-trackpad-daemon: design

## Purpose

Make a handheld's touchscreen behave like Steam Deck trackpads in Steam Input, on Armada OS (Fedora bootc, gamescope, InputPlumber). The first target is the Retroid Pocket Nova.
- **Halves:** the left and right halves of the screen each drive one virtual trackpad, physically scaled to the Deck's 32.5 mm pad.
- **Pad behaviour:** each pad can be fixed or float under the finger, and its edges can clamp, follow, release or recenter (see [Pad behaviour](#pad-behaviour)).
- **Tap-click:** a tap clicks.
- **Toggle:** a key that Steam Input can bind switches between *trackpad mode* and the normal touchscreen.
- **Focus rules:** the mode can follow the app in front, by rules a client writes per app id (see [Focus](#focus)).

### Goals
- A systemd **user** service with no persistent root.
- Built-in per-device defaults, with every setting overridable at runtime over a socket using **JSON-RPC 2.0**. The daemon doesn't read or write a config file. Its one runtime file, the wiring state (see State), is a cache that a restart may use, never configuration.
- A well-designed CLI (`armada-trackpadctl`) for settings, state and toggling.
- Device specifics isolated as constants, so other devices can be added.
- Install and uninstall scripts that run from a git checkout: one for the user service, one root step for the system files.

### Non-goals
- pad layouts other than the two halves
- rumble feedback
- persisted settings
- a rewrite in a faster language
- distro packages

## How it works

```
ft5x06 touchscreen ──(grab in trackpad mode)──► daemon ── tracker → mapper: which half, pad window, edges, tap or drag
                                              │
             ┌────────────────────────────────┴───────────────────────────────┐
             ▼ uinput "Armada Trackpad Left"                                  ▼ uinput "Armada Trackpad Right"
   composite B ("Armada Trackpad Left")                         composite C ("Armada Trackpad Right")
     source: left pad; no targets of its own                      source: right pad; no targets of its own
     profile: touchscreen → LeftPad                               profile: touchscreen → RightPad
     capmap:  KEY_F24 → LeftPad press, ABS_MISC → force,          capmap: the same for the right pad
              KEY_MACRO*/trigger axes → tap outputs
             │                                                                │
             └──────────► deck-uhid "Steam Deck controller" ◄─────────────────┘  (AttachTargetDevice)
                                     ▲  owned by
                       composite A ("Retroid Pocket Nova"): stock config and profile,
                       sources gamepad + gpio-keys, attached last (haptics)
                                              │
                                              ▼
                                  Steam Input (Left/Right Trackpad)
```

- **Trackpad mode on:** the daemon grabs the touchscreen exclusively (`EVIOCGRAB`). Touches go only to the daemon and come back out as pad touches.
- **Trackpad mode off:** the grab is released and gamescope gets normal touch again. The virtual pads still exist but are idle.
  - Toggling never creates or destroys InputPlumber devices, so it never makes Steam see a controller reconnect.
- **Toggle key:** Steam Input delivers a bound keyboard key to the focused Xwayland server (`:1` in game, `:0` in Steam's UI) as an ordinary key. There is no evdev path for it.
  - The daemon grabs that key with `XGrabKey` on each display. The game never sees it.
  - It toggles on the XInput2 *raw* key-down, which has no autorepeat.
  - Default key: Insert. It is in Steam's on-screen key picker (Numpad tab) and has no lock state. Num Lock and Caps Lock would flip a lock on every toggle even while grabbed. The picker has no Scroll Lock, Pause or F13 to F24, and a listened F13 is saved as Print Screen.

## Why it is built this way (InputPlumber 0.82 constraints)

These facts come from InputPlumber's source and tests on the Nova. Each file:line refers to InputPlumber 0.82.0.

**Virtual devices**
- **Virtual devices on the Bluetooth bus:** InputPlumber manages a virtual evdev device only if it is on the Bluetooth bus (udev `ID_BUS=bluetooth` or bustype `0005`) or if its exact name is in `VIRT_DEVICE_WHITELIST` (`src/input/manager.rs:1443-1472`). That list is kept for Sunshine streaming.
  - Our pads declare `BUS_BLUETOOTH` under their own names, "Armada Trackpad Left/Right". On the Nova, InputPlumber adopts such a device and ignores a bus-0x06 control.
  - Virtual hidraw (uhid) devices are rejected unless they match a connected Bluetooth device.
- **No profile routing by region or finger:** a profile maps by capability only. `touchscreen.region` is an unimplemented TODO (`src/config/mod.rs`), and the Deck target ignores the touch index.
  - So one source device can only ever feed one pad, and the pads need two source devices in **two composites**.
- **Capability maps can't send positions:** they produce only Bool/Float/Vector values (`translator.rs`), and DBus `SendEvent` can't send touch values either.
  - Only a touch source can give the Deck pad an x/y. Capability maps drive only the click and force.
- **Tap-click order:** Steam needs pad **force** (`*TouchpadForce`) with the press, sent *while* the touch is still down. A press without force, or one sent after the lift, is ignored.

**Sharing one controller between composites**
- **Feeding:** `AttachTargetDevice(target, composite)` lets composites B and C feed A's Deck controller. InputPlumber doesn't check ownership.
- **Reconnects:** every attach recreates the uhid device. Steam then sees a ~1 s reconnect with the same serial, XInput slot and config set.
- **Haptics:** the controller sends haptics/rumble only to the composite it was attached to **last**. So a wiring attaches to B, then C, then re-attaches to A: three attaches. B and C keep their handles and still write their pads.
- **`persist: true` is required on B and C:** a composite that loses its last source stops its targets, which would kill the shared controller when the daemon exits.

**Config loading**
- **Ordering, not shadowing:** device configs from `/etc/inputplumber/devices.d` and `/usr/share/inputplumber/devices` are all loaded and sorted by filename. The first config that matches a device claims it.
  - Our `49-armada_trackpad_{left,right}.yaml` only claim the pad devices, by name. The stock `50-retroid_pocket_nova.yaml` still claims the Nova controller, untouched, with InputPlumber's default profile.
- **`CreateCompositeDevice` over DBus is a stub**, so composites B and C need root-installed config files.

**Armada**
- **Targets reset on restart:** `armada-controller-type.service` requires InputPlumber, so an InputPlumber restart re-runs it. About 2 s later it resets the targets of every composite.
- **`controller-type apply` hits every composite:** it calls `SetTargetDevices` on all of them. A pad composite (B or C) that isn't attached could get a controller of its own. Before attaching, the daemon removes it (`SetTargetDevices []`) from each such pad composite. It never removes the device composite's controller.
- **Suspend is a non-event:** Armada never calls InputPlumber's sleep hooks.
- **Profile loads reset state:** loading a profile resets all target state.
- **Steam UI hides the pads:** Steam's own UI layout for these handhelds keeps the right trackpad inactive. Test in a game.

## Components (`src/armada_trackpad/`)

| Module | Responsibility |
|---|---|
| `trackpad.py` | `Trackpad`, the synchronous domain object: settings, mode and availability. It turns touch events into pad writes and reports `status()` to clients. It calls the `on_*` hooks its owner wires, and it doesn't import asyncio. |
| `daemon.py` | `armada-trackpadd`. `run()` serves a `Trackpad` on one asyncio loop and is the only place that registers loop work (see the table below). `main()` detects the device and opens the touchscreen. Then it creates the pads and calls `run()`. On hardware without a device profile it calls `run_unsupported()`, which serves only the status. |
| `output.py` | The `PadOutput` protocol (the output interface) and the types it takes (`PadState`, `Side`, `Availability`, `PAD_MAX`). |
| `uinput_output.py` | `UinputOutput`, the only `PadOutput` backend. It manages the uinput pad devices and their InputPlumber wiring, and it waits for InputPlumber to adopt new devices. `pad_frames` turns a state change into evdev frames. |
| `tracker.py` | `Tracker`: multitouch protocol B decoding. It turns slot and tracking id events into per-frame contacts and ignores fingers that were down when mode turned on or settings changed. It resyncs after `SYN_DROPPED` and hands each frame to its `Mapper`. No I/O. |
| `mapper.py` | `Mapper`: per-frame contacts in, `(side, PadState)` transitions out. Finger ownership by landing half, each touch's window (anchor), the edge modes, taps and their click timing, and tap-and-drag. `TapConfig` holds the tap thresholds. No I/O. |
| `evdev.py` | Real touchscreen access: discovery (`find_touchscreen`), coordinate ranges, non-blocking reads, the grab, and `mt_state` (the kernel's slot state, read with `EVIOCGMTSLOTS`). |
| `togglekey.py` | `ToggleKey`: the X11 key grab and the XInput2 raw listener (python-xlib). It takes the loop's callbacks in `start()` and doesn't import asyncio. Its owner calls `reconnect()` periodically. |
| `focus.py` | Trackpad mode on focus ([Focus](#focus)): `FocusWatcher` reads gamescope's focused app in a thread of its own and hands each value to the loop; `FocusModes`, the `focus_modes` setting's value; and `FocusRules`, which applies it on each focus change, with the asymmetric debounce. The X reading is thin (`focused_app` decodes the property); the decisions take the trackpad and a `call_later`, so they are tested without X or a loop. |
| `chord.py` | The controller chord: `BUTTONS`, the button bits of the Steam Deck state report (the same on every device, since the controller is InputPlumber's virtual Deck); `Chord`, the hold timing; `find_deck_hidraw`; and `ChordReader`, which reads the node in a thread and hands completed chords to the loop. |
| `control.py` | The control socket: the JSON-RPC 2.0 server (`serve`), the methods (`methods(trackpad)`), `Subscribers` for `state_changed`, and the synchronous `Client`. |
| `inputplumber.py` | `InputPlumber`, a `busctl` client for the InputPlumber DBus API, and `Wiring`, which checks and repairs the pad composites' wiring and checks the controller type. |
| `settings.py` | `Settings` with its per-pad `PadOverrides`, the device defaults (`defaults_for`), the field schema `FIELDS` (validation, range, help). Validation, the socket and the CLI flags all come from it. `Settings.pad(side)` resolves a pad's effective `PadSpec`. |
| `devices.py` | A `DeviceProfile` per supported device has its detection (devicetree `compatible` / DMI), touchscreen name, panel size in mm, X displays and InputPlumber main composite name. The profile `id` names its data directory, `data/devices/<id>/`, which overrides the shared `data/common/` file by file (`data_file`, see [Install layout](#install-layout)). |
| `geometry.py` | The pad layout in mm (`halves` gives a `Zone` per pad from its `PadSpec`: its half, which is its start area while both pads are idle, window, mode) and the conversion to touchscreen units on `Screen`. |
| `modes.py` | Pad behaviour data (`Anchor`, `EdgeMode`, `Edges`, and `PadMode`, a pad's whole behaviour) and the pure per-axis window functions `anchor_axis`, `edge_axis` and `to_pad`. |
| `linux_input.py` | Constants and ioctl numbers from the kernel's input and uinput headers. |
| `errors.py` | `Unavailable`, raised by the trackpad when mode on is not possible and mapped to an error code at the socket. |
| `cli.py` | `armada-trackpadctl`. |

There is no shared constants module. Each constant lives with the code that owns it: the pad device names in `uinput_output.py`, `DECK_PAD_MM` in `geometry.py`, the default toggle key in `settings.py`.

### The event loop

The daemon runs on one asyncio loop, and nothing on the loop waits for another process. Threads run the blocking `PadOutput.check`. They also connect the toggle key's X displays, read the toggle chord and watch the focused app. Only `daemon.py` and `control.py` import asyncio. `run()` registers every source of work:

| Source | Registered as | Calls |
|---|---|---|
| Touchscreen fd | `loop.add_reader` | `Trackpad.on_touch` with the loop time, then moves the tick timer |
| Tap click ends | one `loop.call_at` timer at `Trackpad.next_deadline` (`_Tick`) | `Trackpad.on_tick` |
| Toggle key | `loop.add_reader` per X display, handed over in `ToggleKey.start` | `Trackpad.toggle`. `Unavailable` is only logged |
| Display reconnects | a task: `every(RECONNECT_S, toggle.reconnect)`, every 5 s | `ToggleKey.reconnect`, which starts a thread per missing display; the thread passes the connection back with `loop.call_soon_threadsafe` |
| Focused app | a `focus :0` thread with its own connection to `:0`, blocked in `poll` on the display's fd and a stop pipe; a task `every(RECONNECT_S, focus.reconnect)` starts it again after it ended | at connect and on each change it reads the property and hands the value to the loop with `loop.call_soon_threadsafe`, then `FocusRules.focus_changed` |
| Focus rule `on` | one `loop.call_later(FOCUS_SETTLE_S)` timer, held by `FocusRules` | `Trackpad.set_mode(True, "focus")`. `Unavailable` is only logged |
| Toggle chord | a `chord-reader` thread blocked in `poll` on the Deck controller's hidraw node and a stop pipe, only while `toggle_chord` is on | per report (250/s, idle included) it compares the type and button bytes, skipping the frame counter in bytes 4-7, and calls `Chord.feed` only on a change; a completed chord goes to the loop with `loop.call_soon_threadsafe`, then `Trackpad.toggle`. `Unavailable` is only logged. Measured on the Nova: a blocking thread costs ~28 µs per report, against ~52-70 µs through asyncio's `add_reader` |
| Toggle key + chord | both call `_Toggles.press` | One press read by both sources toggles once. A toggle within `TOGGLE_MERGE_S` (1.5 s) of a toggle from the *other* source is dropped, as when a chord button is bound to the toggle key in Steam Input. Repeats from one source always toggle |
| Chord node reconnects | a task: `every(RECONNECT_S, chord.reconnect)` | `ChordReader.reconnect` opens the node again after a read error, or when InputPlumber recreated the controller under another node |
| Control socket | `control.serve`, one task per client | `control.methods(trackpad)`, plus `subscribe` |
| Wiring checks | a task, run at start, when `apply` adds or removes a side, and on a `check` request (never on a timer) | `read_version` (InputPlumber's version) then `PadOutput.check`, each in a worker thread (`asyncio.to_thread`), then `Trackpad.set_inputplumber_version` and `Trackpad.set_availability` |
| SIGTERM, SIGINT | `loop.add_signal_handler` | stop |

`run()` also wires the trackpad's hooks: `on_state_changed` publishes to the socket's subscribers, `on_sides_changed` and `on_check_requested` wake the check task, `on_toggle_key_changed` calls `ToggleKey.set_key`, `toggle_key_displays` reports `ToggleKey.connected`, `on_toggle_chord_changed` calls `ChordReader.set_chord`, and `toggle_chord_device` reports the node being read. The focus watcher reports to `FocusRules.focus_changed`, which records the app with `Trackpad.set_focused_app`.

- **X servers:** python-xlib waits for a reply with no timeout, so an Xwayland that accepts a connection but never answers would stall whoever waits. Each display connects in its own daemon thread, and moving the grab to a new key only sends requests (a `BadAccess` is logged when it is read). A thread stuck on a silent server ends when that server goes away, and never holds up exit. Because reading a property is a round trip, the focus watcher's display is used only from its own thread, never from the loop.
- **Errors that repeat:** a failing wiring check or reconnect logs one traceback, then a one-line warning each time the same error comes back. A pad write that keeps failing warns once per side until the error changes.
- **Stopping:** a signal or a failed touchscreen read stops `run()`. A failed read (including the slot state read after an overflow) gives exit status 1, so systemd restarts the daemon. Both paths then shut down in this order:
  1. The loop work stops. `run()` cancels a check still running in its worker thread (`PadOutput.cancel_checks`) and removes the signal handlers and the touchscreen reader. It cancels the tick and a waiting focus rule, then cancels the check and reconnect tasks and waits for them.
  2. The socket and its client connections close.
  3. Mode turns off (`Trackpad.close`).
  4. The toggle key, focus watcher, chord reader, output and touchscreen close.
- **Startup:** `main()` takes a lock file next to the socket (`armada-trackpad.lock`), so only one daemon runs. An error while opening the touchscreen or creating the pads is logged as one `cannot start:` line.

### The output interface

`PadOutput` (`output.py`) is the boundary between the trackpad and whatever moves the controller's pads. The tracker emits `(side, PadState)` transitions. Each `PadState` is a complete, level-triggered state (touching, x and y in 0..`PAD_MAX`, pressed). `Trackpad` performs each transition by calling `output.write`, and the output works out the evdev events itself.

| Method | Runs | Does |
|---|---|---|
| `set_sides(sides)` | on the loop | creates devices for new sides, releases and removes the others; on `OSError` nothing has changed |
| `write(side, state)` | on the loop | puts the pad in `state`; an unchanged state or a side without a device is a no-op |
| `check(sides)` | in a worker thread, blocking | checks and repairs what the pads need, and returns an `Availability` |
| `cancel_checks()` | on the loop, at shutdown | makes a running `check` return at its next step, and every later one at once |
| `close()` | on the loop | releases every side and frees the devices |

`UinputOutput` is the only backend. It manages both the pad devices and their InputPlumber wiring (`Wiring`), because the wiring only makes sense for those devices. Its `check` first waits until `ADOPT_S` (1.5 s) after the latest device creation, so InputPlumber has adopted the new devices, then calls `Wiring.ensure`. A check reads InputPlumber's state with one `GetManagedObjects` call on `/org/shadowblip/InputPlumber` (InputPlumber provides `org.freedesktop.DBus.ObjectManager` there; on the Nova it costs busctl ~16 ms of CPU and returns ~108 KB of JSON with every composite's `Name`, `ProfileName` and `TargetDevices` and every target's `DeviceType`). It doesn't run `busctl` once per property. Only repairs make single calls. `cancel_checks` cuts that wait short and calls `Wiring.stop`, which ends `ensure` before its next InputPlumber call. The one exception is a re-wire that has already attached the controller to a pad composite: it skips the other pad but still attaches it back, so the controller's haptics never stay on a pad composite. Without this, `asyncio.run` would wait for the thread at exit (up to 10 s per busctl call), and a late re-wire could reconnect the controller after the pads were gone. A future backend, such as an InputPlumber touch injector, would replace both together.

## Settings

All settings have device defaults and can be changed at runtime. Changes are validated as a whole, then applied atomically.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `pads` | `none` / `left` / `right` / `both` | `both` | Which virtual pads exist |
| `size` | float 0.3-3.0 | 1.0 | Pad side as a multiple of the Deck pad (32.5 mm); shrinks to fit its half and is always square |
| `center_y` | float 0-1 | 0.5 | Pad centre from the top, as a fraction of the screen height (clamped so the pad stays on screen) |
| `side` | float 0-1 | 0.0 | Position within the free space of the half: 0 = flush to the outer edge, 1 = flush to the centre line (mirrored) |
| `panel_mm` | `[w, h]` mm | device (Nova 91×68) | Physical panel size used for scaling |
| `anchor` | `fixed` / `float` | `fixed` | Where the pad window sits when a finger touches down ([Pad behaviour](#pad-behaviour)) |
| `edges` | one edge mode, or `[top, bottom, inner, outer]` | `clamp` | The action of each window edge when the finger moves past it: `clamp`, `follow`, `release` or `recenter`. Always reported as a list of four |
| `edge_grace_mm` | float 0-10 | 2.0 | How far past a `release` edge the finger may go before it acts; `recenter` ignores it |
| `float_keep_inside` | bool | false | Keep a floating window on the screen |
| `tap` | bool | true | Tap-to-click |
| `tap_drag` | bool | false | Tap, then touch again within `tap_drag_ms`, to drag with the tap output held |
| `tap_output` | `pad`, or a Deck button or trigger (see Tap outputs) | `pad` | What a tap and a tap-drag press |
| `tap_max_ms` | int 50-1000 | 180 | Longest touch that still counts as a tap |
| `tap_max_mm` | float 0-20 | 3.0 | Most travel that still counts as a tap |
| `click_ms` | int 10-500 | 40 | Length of the synthesized click |
| `tap_drag_ms` | int 50-1000 | 200 | How long after a tap the second touch of a tap-and-drag may land |
| `toggle_key` | X keysym or `none` | `Insert` | Key that toggles trackpad mode |
| `toggle_chord` | bool | true | Toggle trackpad mode with the device's built-in controller chord, read from the controller itself. On the Nova it is L1 + R1 + L3 + R3, all held, with no hold time (`DeviceProfile.toggle_chord`); a combination games and Moonlight (Select + Start + L1 + R1) leave alone |
| `log_level` | `info` / `debug` | `info` (the daemon starts at `debug` with `--log-level DEBUG`) | Daemon log verbosity. `debug` also collects latency and CPU stats, read with `armada-trackpadctl stats` and logged every 10 s |
| `focus_modes` | `{"apps": {"<app id>": rule, …}, "default": rule}`, each rule `on` / `off` / `off_locked` / `keep` | `{"apps": {}, "default": "keep"}` | The trackpad mode each focused app sets ([Focus](#focus)). The object has exactly `apps` and `default`. App ids are decimal strings of integers 0..2^32-1, with no sign, space or leading zero (`"0"` is the one id that starts with 0); at most 256 apps |

The defaults give both pads a fixed window with clamped edges.

**Per-pad overrides:**
- `size`, `center_y`, `side`, `anchor`, `edges`, `edge_grace_mm`, `float_keep_inside`, `tap`, `tap_drag` and `tap_output` each have a `left.<key>` and a `right.<key>` override, such as `left.size`.
- An override takes the shared key's type, or `null`. `null` is the default and means the pad uses the shared value.
- Resetting an override sets it back to `null`.
- Both pads share the other settings, including the tap thresholds `tap_max_ms`, `tap_max_mm`, `click_ms` and `tap_drag_ms`.

**The settings object** (`get_settings`, and the reply of `set_settings` and `reset_settings`) has every key, overrides included: `null` for an override that is not set, `panel_mm` as `[w, h]` and `edges` as a list of four. Sent back as `changes`, it is accepted and doesn't change anything, so a client can save and restore it as it is. Every `set_settings` still re-applies the settings, a no-op round trip included: it releases the touched pads, as described under Ownership below.

**Ownership:**
- **Which pad a new touch starts** is decided in the first frame it appears (by slot and tracking id), from which pads are busy then. A pad is busy while a finger is down on it. A pad with only a pending click or tap is idle.
  - Both pads idle: the pad of the half where the touch comes down. A touch on the centre line belongs to the right half.
  - One pad busy, or off (`pads=right` / `pads=left`): the idle pad, wherever the touch comes down.
  - Both busy: no pad. The touch is ignored until it lifts, even if a pad frees up meanwhile: there is no handoff.
  - Touches landing in one frame are decided in slot order, so two fingers landing in one half get one pad each.
- **Down means free:** a touch keeps its pad until it lifts, wherever it moves. Only its window border (through the edge mode) and the screen border affect it, never the centre line.
- A touch landing on a pad whose tap click is pending takes it over: the click is released, then the new touch starts. The one exception is a touch landing in the frame a delayed tap-drag click starts. That touch waits for the click to end, then touches. The click is never cut short. A tap lift and a new landing on the same pad in one frame can still give a one-frame click. That is accepted.
- Past the pad window, each edge acts by its edge mode ([Pad behaviour](#pad-behaviour)). With the default `clamp`, the position pins to the pad edge per axis.
- Fingers already down when trackpad mode turns on are ignored until lifted.
- Applying settings releases every touched pad and every pending click, and drops a tap still waiting for its drag without clicking. Fingers down at that moment are ignored until lifted. The next touch then uses the new layout and mode.

## Pad behaviour

Each pad maps a finger to pad coordinates through a **window**: a square of 32.5 mm × `size`, shrunk to fit its half as described under `size`. A pad's **behaviour** is its anchor and its edge modes (below). They move the window and never scale it, so crossing the whole pad takes the same physical distance in every behaviour. On each axis, the pad position is (finger − window origin) / window size, clamped to 0..`PAD_MAX`.

A pad's **start area** decides which pad a new touch belongs to (see Ownership above): its half of the screen while both pads are idle, the whole screen while the other pad is busy or off. The window sets where on the pad the touch is, and is placed again for each touch.

**Anchor:** where the window sits when a finger touches down.

| `anchor` | Window |
|---|---|
| `fixed` | where `size`, `center_y` and `side` put it; a landing outside shifts it just far enough that the finger is on its nearest edge, or its corner, until the finger lifts |
| `float` | centred on the landing point. Every touch starts at the pad's centre |

`float_keep_inside` decides what happens near the screen border. It is off by default, and then every floating touch starts at the pad's centre. When on, a floating window that would stick out of the screen is shifted back onto it, and the landing is then off centre. It is held on the screen, not in the pad's half. A window may overhang the centre line. A touch crosses that line anyway, since it keeps its pad. When off, the window is always centred on the finger and can hang off the screen, and its off-screen part cannot be reached.

**Edges:** what each window edge does when the finger moves past it. The edges are `top`, `bottom`, `inner` (towards the centre line) and `outer` (towards the screen's side edge), so one `edges` value mirrors between the two pads.

| Edge mode | Past the edge |
|---|---|
| `clamp` | the position pins to the edge on that axis until the finger comes back |
| `follow` | the window moves with the finger, which stays on the edge, so reversing responds at once |
| `release` | the pad lifts until the finger is past no `release` or `recenter` edge, then touches again; `follow` edges keep moving the window meanwhile |
| `recenter` | the pad lifts. At the finger's next move the window is re-anchored on both axes as `float` would place it, whatever the anchor. Then the pad touches again |

- **Grace:** `release` acts only once the finger is more than `edge_grace_mm` past the edge, converted to touchscreen units per axis, so a finger resting on the edge does not flap the pad. Within the grace the position clamps. `recenter` and `follow` ignore the grace and act as soon as the finger is past the edge: on the Nova, a grace visibly stalled a `recenter` cursor at each edge. After a `release` the pad touches again as soon as the finger is back on the window.
- **The recenter lift:** Steam sees the pad lift between the old and the new position. Without the lift, Steam's mouse mode would see a jump. A finger that stops right after crossing leaves the pad lifted, and its lift then doesn't add a transition. A re-anchored window obeys `float_keep_inside` too, so it is held on the screen and never in the half: once a finger is down, the centre line never stops it.
- **Both in one frame:** when a `release` and a `recenter` edge trigger together, `release` wins.
- **Landing:** a landing always starts inside its window or on its border, never past an edge. Because of that, every landing touches at once under every edge mode. The edges act from the finger's next move on, at the border of the window placed at landing.

### Taps and tap-and-drag

- On a pad whose `tap` is on, a touch that lifts within `tap_max_ms` and travels less than `tap_max_mm` is a tap. It presses the tap output for `click_ms`: the pad click with the finger still on the pad, any other output without a touch. With `float`, a tap clicks at the pad's centre, because the touch started at the window's centre.
- A touch that an edge lifted at any point is never a tap.
- **Tap-and-drag** (`tap_drag`), only on pads where it is on:
  - A tap lifts the pad, and its click waits `tap_drag_ms`. On these pads only, every tap click comes that much later.
  - A second touch landing on the pad within that window drags with the pad pressed until it lifts. If no second touch comes, the click comes when the window ends.
  - A second touch that is itself a tap clicks again at its lift, so a double tap still gives two clicks.
  - A second touch always lands inside its window or on its border, never past an edge, so it drags with either output.
  - The drag uses the pad's own `edges`. What happens at an edge lift depends on the tap output:
    - **`pad`:** a `release` or `recenter` lift ends the drag, because Steam drops a pad click without a touch. A later touch on the pad doesn't resume it and starts unpressed.
    - **Any other output:** the button is held for as long as the edge has lifted the pad, and the pad touches again as its edges say. A drag on `recenter` edges goes on across every recenter until the finger lifts.
- **Tap outputs** (`tap_output`): `pad` is the pad's own click with force. The others are Deck buttons: `a b x y`, `dpad_up dpad_down dpad_left dpad_right`, `l1 r1 l3 r3`, the back grips `l4 l5 r4 r5`, `view menu steam qam`, and the triggers `l2_soft l2_full r2_soft r2_full`.
  - A soft pull sets the analog trigger to 200/255 without its digital click. With a full pull, the trigger goes to the maximum and its digital button is pressed too.
  - Each output has its own code on the pad device: `KEY_MACRO1` onwards for the buttons, `ABS_VOLUME` and `ABS_PROFILE` for the left and right trigger. The capability maps turn each code into its Deck input (`tap_output.py` holds the table, and a test keeps the maps in sync). Joystick-range codes are avoided, so udev never tags a pad as a joystick.
  - A held output is always released before another can press. It is also released on every way out (applying settings, mode off, removing the pad and stopping).
  - The game's Steam Input layout decides what the button does, such as R5 bound to Left Mouse Click. On the Nova, L5 and R5 are free: its two top buttons are L4 and R4.

## State

- `mode`: `on` / `off`. Starts `off`.
- `mode_by`: what made the latest mode change. `start` until the first change, then `client` (a `set_mode` or `toggle` call), `key` (the toggle key), `chord` (the toggle chord), `focus` (a [focus rule](#focus)) or `unavailable` (the pads stopped working or were removed, or the daemon is stopping). Asking for the mode it already has doesn't change the mode or the reason.
- `available`: `true` only when the main composite's gamepad target is `deck-uhid` and the wiring is in place.
  - **When unavailable:** turning mode on with `set_mode` or `toggle` fails with the `unavailable` error and a reason (e.g. `controller type is xbox-elite, needs deck-uhid`). The touchscreen is not grabbed. If the pads become unavailable while mode is on, mode drops to off. A state change is logged once.
  - **Wiring checks:** the daemon checks at start and right after a pad is added or removed. It also checks on `armada-trackpadctl check` (RPC `check`). Each check looks at the controller type and wiring again and re-wires if needed. Checks on a timer cost ~4% of a core, and the daemon doesn't use one, because InputPlumber restarts are rare (boot, updates, a controller type change). After one, a client runs `check`.
- `focused_app`: the id of the app gamescope has in front, or null when unknown ([Focus](#focus)).
- **Wiring state:** each re-wire saves the controller's sysfs identity to `$XDG_RUNTIME_DIR/armada-trackpad.wired` (next to the socket) as `{"version": 1, "controllers": [...]}`.
  - At start, a saved identity that still matches the live controller, with every pad composite in use attached, means no re-wire. A daemon restart then doesn't cause a Steam reconnect.
  - It is cache, not configuration. A missing, malformed or unsafe file only means the first check re-wires.
- **Unsupported hardware** (no device profile matches): the daemon still starts, so a client can tell why nothing works. It doesn't grab a touchscreen, create pads or wire anything. `status` reports mode `off`, `available: false`, `reason: "device not supported: <model>"` (the first devicetree `compatible` string, else the DMI product name), `device` empty, no pads, toggle key and chord `none`. The other methods return the `unavailable` error with that reason.
- **Startup errors** (an unknown `--device`, a missing touchscreen, no permission for it or `/dev/uinput`): exit status 78 (`EX_CONFIG`). The unit's `RestartPreventExitStatus=78` keeps systemd from restart-looping.

## Control protocol

The socket speaks **JSON-RPC 2.0** over newline-delimited JSON. It was chosen over MessagePack-RPC because:
- it needs only the standard library, so the daemon has no `msgpack` dependency;
- it can be debugged by hand with `socat`;
- a client in any language, such as the Python backend of a Decky plugin, doesn't need an extra library;
- notifications have a standard form.

**Transport and framing**
- A Unix stream socket at `$XDG_RUNTIME_DIR/armada-trackpad.sock`, mode 0600.
- One UTF-8 JSON object per line, in both directions. A line is at most 64 KiB (`MAX_LINE`) before its newline, and a longer one disconnects the client.
- A line that is not JSON (or not UTF-8, or that contains `NaN`/`Infinity`) is answered with a parse error, then the connection is closed.

**Requests and responses**

```
-> {"jsonrpc":"2.0","id":1,"method":"set_mode","params":{"on":true}}
<- {"jsonrpc":"2.0","id":1,"result":"on"}
<- {"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"controller type is xbox-elite, needs deck-uhid"}}
```

- `params` is an object (by name) or left out. An array gets `invalid_params`.
- Batches are not supported.
- Top-level members other than `jsonrpc`, `id`, `method` and `params` are rejected with `invalid_request`. So a misspelt `id` gets an error instead of turning the call into a silent notification.
- A valid request without an `id` is a JSON-RPC notification: the daemon runs it and doesn't reply. An invalid request is always answered, with `"id": null` when it has no valid id.

| Method | Params | Result |
|---|---|---|
| `status` | none | the status object (below) |
| `get_settings` | none | the settings object ([Settings](#settings)) |
| `get_defaults` | none | the device's default settings object |
| `set_settings` | `{"changes": {key: value, …}}` | new settings object (all or nothing) |
| `reset_settings` | `{"keys": [key, …]}`; no params or `"keys": null` resets all | new settings object |
| `set_mode` | `{"on": true}` or `{"on": false}` | the new mode, `"on"` or `"off"` |
| `toggle` | none | the new mode |
| `subscribe` | none | the status object; the connection then receives `state_changed` |
| `check` | none | null at once; the check runs, and its result arrives as a `state_changed` with a new `checked_at` |
| `get_stats` | none, or `{"reset": true}` to clear after reading | `enabled` (the `log_level` is `debug`), `frames`, `latency_ms`, `processing_ms` and `interval_ms` (each `{p50, p95, max}` or null), and `cpu` (`user_s`, `system_s`, `children_s`, `voluntary_switches`, `involuntary_switches`) |

Any param a method does not take is an `invalid_params` error.

The **status object** has:
- `mode`: `"on"` or `"off"`, and `mode_by`: what made its latest change (see [State](#state));
- `available` and `reason` (empty when available);
- `device`: the device profile id, empty on hardware without a profile;
- `pads`: the sides that have a pad, sorted;
- `toggle_key`: a keysym or `"none"`, and `toggle_key_displays`: the X displays it is grabbed on;
- `toggle_chord`: the device's chord (such as `"l1+l3+r1+r3"`) or `"none"` when it is off. `toggle_chord_device`: the hidraw node it is read from, or null when it is not open;
- `version`: the package version (such as `0.2.0`) for people and bug reports. Clients do not check it.
- `protocol`: the control protocol's number as an integer, currently `1` (see Versioning below).
- `instance`: a random hex id that changes every time the daemon starts.
- `checked_at`: the time the latest wiring check finished in Unix seconds, or null before the first. Every finished check publishes a `state_changed`, even when the availability did not change.
- `focused_app`: the id of the app in front as an integer (`769` is Steam's own UI), or null when unknown. It is unknown when there is no display or property, or when the connection is down ([Focus](#focus)).
- `inputplumber_version`: InputPlumber's version, such as `"0.83.0"`, read with each wiring check (on unsupported hardware, once at start), or null when it could not be read. It is the `Version` property of `org.shadowblip.InputManager` on `/org/shadowblip/InputPlumber/Manager` (`src/dbus/interface/manager.rs`), and polkit allows any caller. The tested releases are `TESTED_INPUTPLUMBER` (0.82 and 0.83); another version is logged as a warning, once per version for the daemon's life. A changed version is published with the check's result, as one `state_changed`.

| Code | Name (as the CLI prints it) | When |
|---|---|---|
| -32700 | `parse_error` | the line is not JSON; the connection is then closed |
| -32600 | `invalid_request` | not an object, a batch, a wrong `jsonrpc`, `id` or `method`, or unknown members |
| -32601 | `unknown_method` | no such method |
| -32602 | `invalid_params` | params missing, unknown or malformed, or the settings are invalid |
| -32603 | `internal` | a method failed unexpectedly; the daemon logs the traceback |
| -32000 | `unavailable` | mode on is not possible; the message says why |

**Versioning**
- `protocol` in the status is the control protocol's number. It changes on any incompatible change to the protocol. Such a change removes or renames a method, param, status field or settings field, or changes what one means.
- Additive changes keep the number. These are a new method, optional param, status field or setting, or a new value of an existing one. A client ignores fields it does not know.
- Clients check `protocol`, not `version`: a client written for protocol 1 works with every daemon that reports 1.

**State notifications**
- After `subscribe`, the daemon sends `{"jsonrpc":"2.0","method":"state_changed","params":{…status…}}` on that connection whenever the mode (with its `mode_by`), the availability, the settings, the focused app or InputPlumber's version change. A focus rule's `on` arrives in a later notification than the focus change that set it off, 0.25 s after it. `mode_by` tells it from a manual change. A change in `toggle_key_displays` or `toggle_chord_device` alone sends none. Both are diagnostics that clients read on demand with `status`. The journal logs every change of node.
- The connection can still take calls, so replies and notifications can interleave. Match replies by `id`.
- A subscriber with more than 64 KiB (`MAX_BUFFER`) of unread notifications is disconnected, so a client that stops reading never blocks the daemon.

**Notes for client authors**
- The main client is the Decky plugin, [decky-armada-trackpad](https://github.com/nikitautiu/decky-armada-trackpad). Any program can be one.
- The socket is mode 0600 in the session user's runtime directory (normally `/run/user/<uid>`). A client must run as that user to reach it. A Decky plugin's backend does.
- One connection is enough. Call `subscribe` and show its result, then follow `state_changed`.
- When the daemon restarts, the connection closes. Reconnect and subscribe again. A new `instance` means a daemon that started from the device defaults with mode off, so a client that keeps settings should push them again.
- `control.Client` is the reference client (`call`, `notifications`). The CLI and `tests/device/e2e_hid.py` use it.

## CLI

```
armada-trackpadctl [--socket PATH] [--json] COMMAND
  status
  on | off | toggle
  watch
  get [KEY ...] [--defaults] [--all]
  set [--pads both] [--size 1.2] [--center-y 0.4] [--side 0] [--panel-mm 91x68]
      [--anchor float] [--edges clamp,clamp,follow,release] [--edge-grace-mm 2]
      [--float-keep-inside | --no-float-keep-inside] [--tap | --no-tap] [--tap-drag | --no-tap-drag]
      [--tap-output pad] [--tap-max-ms 180] [--tap-max-mm 3] [--click-ms 40] [--tap-drag-ms 200]
      [--toggle-key KEY|none] [--toggle-chord | --no-toggle-chord] [--log-level info|debug]
      [--focus-modes '{"apps": {"769": "off"}, "default": "keep"}']
      [--left-size 1.2] [--right-anchor float] [--left-tap | --no-left-tap] ...
  check [--timeout 30]
  stats [--reset]
  reset [KEY ...]
```

**Stats** (`log_level` `debug` only, nothing is recorded at `info`):
- Recorded in trackpad mode only.
- `latency_ms`: from the kernel's timestamp of a touch frame (the touchscreen is set to `CLOCK_MONOTONIC`) to after the daemon's pad writes. That is the daemon's whole share of the latency, wake-up included.
- `processing_ms`: from reading the events to after the writes.
- `interval_ms`: the gaps between touch frames under 100 ms, i.e. the panel's report rate.
- `cpu`: process totals since start. `children_s` is the `busctl` runs of the wiring checks.
- At `debug`, the same appears in the journal every 10 s while frames arrive.

- `--json` prints JSON instead of text. It goes before the command.
- `status` shows the mode with what changed it last, such as `on (focus)`, the focused app (`unknown` while it is null), and InputPlumber's version, marked `(untested)` when it is not a tested release.
- `watch` prints the state, then one line per change (e.g. `14:02:11 mode on (key)` or `14:02:12 focus 413150`), until Ctrl-C or until the daemon goes away. In that case it exits with status 3. When the reader of its output goes away (`watch | head`), it exits quietly with status 0, not the shell's 141 for SIGPIPE. The reader asked for no more output, which is not a failure, and a `pipefail` script does not fail on it. With `--json` it prints one status object per line.
- `get` with one key prints the bare value, for scripts. `--defaults` shows the device defaults.
- `get` with no keys leaves out the per-pad overrides that are not set, which would only read `inherit`. `get --all` shows them too. Named keys and `--json` always show what was asked for, nulls included.
- The `set` flags are generated from the settings schema: each key with `.` and `_` turned into `-`, such as `--right-edges` and `--left-float-keep-inside`/`--no-left-float-keep-inside`. Bool settings get a `--no-` form.
- `--focus-modes` takes the JSON a client sends, and `get focus_modes` prints it in the same form.
- `--edges` takes one mode, such as `recenter`, or four separated by commas, in the order top, bottom, inner, outer. `get` shows edges the same way, and an override that is not set as `inherit`. `reset left.size` sets an override back to inheriting.
- Exit codes are 0 for ok, 1 for a daemon error, 2 for usage and 3 when the daemon is not reachable (or lost).

## Focus

Trackpad mode can follow the app in front. The daemon has no Steam-specific code. A client writes the rules as data in the `focus_modes` setting, such as "769 (Steam's UI) turns the mode off, 413150 (a game's appid) turns it on". The daemon applies them.

**The focused app**
- gamescope sets `GAMESCOPE_FOCUSED_APP` (a 32-bit `CARDINAL`) on the root window of its first X display, `:0`. Its first value is the focused app's id: `769` for Steam's own UI (the QAM, the Steam menu, the library), the game's appid while a game is in front.
- The daemon selects `PropertyChangeMask` on that root window and reads the property at connect and on each `PropertyNotify` for the atom. Measured on the Nova with `xprop -root -spy`, a game launch went `769 → game → 769 (177 ms) → game`. Opening and closing the QAM gives one change each way, and changes 0.3 s apart were all delivered.
- The watcher runs in a thread of its own, which has its own connection to `:0`, separate from the toggle key's: the toggle key disconnects every display when it is set to `none`, and connects `:1` too. Reading a property waits for the server's reply, and python-xlib waits with no timeout, so the display is never used on the loop (see [The event loop](#the-event-loop)).
- `focused_app` is null while it is unknown. It is unknown before the first read, while `:0` cannot be reached, while the root has no such property (or one of another type), and once the connection goes down. A gamescope restart closes the connection; the reconnect task connects again within 5 s (`RECONNECT_S`), and the app read then counts as a change.
- Every change of `focused_app` publishes a `state_changed`.

**The rules**
- When the focused app changes, the mode is set to `apps[str(appid)]` if there is one, else to `default`. `off_locked` turns the mode off like `off`, and locks it (below). `keep` does nothing. A null app doesn't apply a rule.
- The rules act only on a change of the focused app. A manual change (the toggle key, the chord, `set_mode`, `toggle`) holds until the next focus change, also one made while an `on` rule waits for its app to settle. Writing `focus_modes` acts on nothing by itself: a new rule for the app in front waits for the next focus change.
- **`off_locked`:** the rule sets the mode off at once, like `off`. The toggle key and the controller chord do nothing while the focused app's rule is `off_locked`. Each press logs one info line with its source and doesn't leave merge state. `set_mode` and `toggle` over the socket (`mode_by` `client`) are not blocked, so the CLI can still change the mode. Writing `off_locked` for the app already in front doesn't change the mode (a new `focus_modes` holds), but toggles are refused from then on. A client picks which apps are locked, e.g. Steam's own UI. The daemon has no list of special apps.
- **Asymmetric debounce:** a rule that turns the mode `off` applies at once, so Steam's UI gets the touchscreen back right away. A rule that turns it `on` applies only once the same app has held focus for `FOCUS_SETTLE_S` (0.25 s), and another focus change before then cancels it. So the 177 ms flicker back to `769` at a game launch never grabs the screen, and the launch turns the mode on once.
- An `on` rule while mode on is not possible (the pads are unavailable, or the touchscreen is busy) leaves the mode off and logs a warning, once per reason. It doesn't send an error to anyone.
- A change made by a rule reports `mode_by` `focus`.

## Install layout

**User side** (`install.sh`, no root):
- uv: uses the one on PATH, or fetches a pinned release into `~/.local/bin` after a sha256 check.
- Code: the project goes to `~/.local/share/armada-trackpad-daemon`, with a venv from `uv sync --frozen --no-dev`.
- Device: `armada-trackpadd --print-device` from that venv. On a device without a profile, install stops before linking the commands or installing the service.
- Commands: `~/.local/bin/armada-trackpadd` and `armada-trackpadctl` are symlinks into the venv.
- Service: `~/.config/systemd/user/armada-trackpadd.service`:
  - `PartOf=` and `WantedBy=gamescope-session-plus@steam.service`, the same pattern as Armada's own `armada-powerbuttond`;
  - `Restart=on-failure`.

**Device data:** files every device uses are in `src/armada_trackpad/data/common/`. Everything device-specific is in one folder per device, `src/armada_trackpad/data/devices/<device>/`, named by the profile `id`. A device's file replaces the shared file of the same path (`devices.data_file`, and the root step resolves the InputPlumber folders the same way). A new device only adds what differs.
- `common/inputplumber/`: `capability_maps.d/` (the tap outputs, installed by the root step) and `profiles/` (loaded by the daemon). Neither mentions the device.
- `devices/<device>/inputplumber/`: `devices.d/`, the pad composites' configs, whose `matches` block names the device's hardware (installed by the root step), and `composite`, the name of the device's own composite (read by the keyboard unit). Both repeat what the `DeviceProfile` says, and tests keep them in agreement: the `matches` values must be the profile's devicetree `compatible` strings or DMI product names, and `composite` must be its `inputplumber_composite`. They are checked-in files rather than generated at install, so root copies only files that can be read and reviewed in the checkout;
- `firmware/`, if the device has any: `registers`, the touchscreen registers to write at boot and after each resume ([Touchscreen firmware](#touchscreen-firmware-nova));
- `system/`: the device's root units (`*.service`) and their helpers (`*.sh`). Each device has its own, because they are validated only on that device. On the Nova: `armada-trackpad-keyboard.service` with `keyboard.sh` ([Keyboard output](#keyboard-output)) and `armada-trackpad-firmware.service` with `firmware.sh` ([Touchscreen firmware](#touchscreen-firmware-nova)).

**Root side** (`sudo scripts/system-files.sh install [--no-firmware] [DEVICE]`; `sudo scripts/system-files.sh uninstall`):
- DEVICE defaults to the detected device: `python3 -I -S src/armada_trackpad/devices.py` on the system Python, from the checkout, prints what `armada-trackpadd --print-device` prints. `devices.py` needs only the standard library for this (a test checks its imports), `-I` keeps the environment and user site-packages out of the root process, and `-S` the system site-packages. An unsupported device stops the install before anything is written;
- copies `inputplumber/devices.d/*.yaml` and `capability_maps.d/*.yaml` into `/etc/inputplumber/`: the device's own, and each shared one the device has no file of that name for;
- for every unit in `system/`: copies the files its `Exec*=` lines name under `/etc/armada-trackpad/` there, root-owned (`*.sh` 0755, data 0644). Each is `system/<name>` if the folder has one, else the one file of that name elsewhere in the device folder, such as `inputplumber/composite` and `firmware/registers`. It then copies the unit to `/etc/systemd/system/`, reloads systemd and enables it;
- before the first file is written, the device folder is checked and every file found. Neither the device folder, the shared folder nor anything in them may be a symlink, so root copies only the checkout's own files. A unit must be named `armada-trackpad-[a-z0-9-]*.service`, and its `Exec*=` lines (`ExecStart`, `ExecStartPre`, `ExecStop`..., with or without the `-@:+!` prefixes) may run and name only files in `/etc/armada-trackpad/` and under `/usr/`. The format is kept strict so the check reads what systemd runs: `KEY=VALUE`, absolute commands, no line continuations, quotes, escapes, `%` specifiers, `$` variables or `;`;
- `--no-firmware` leaves out `armada-trackpad-firmware.service`, and removes it if an earlier install put it there, with its files but those the installed units still name; the touchscreen registers then keep the driver's values;
- restarts InputPlumber, which also restarts Armada's `armada-controller-type.service` and the keyboard unit (both `Requires=` it), then restarts the keyboard unit once more, after the controller type, starts every other unit once, and prints their journal lines;
- runs `armada-trackpadctl check` as the invoking user, in a new session without root's terminal (`setsid -w`, stdin from `/dev/null`), and tells a check that failed (exit 1) or did not find a daemon (exit 3) from one that could not run. Uninstall doesn't run a check: the CLI is usually gone by then.

Uninstall removes the InputPlumber files of every device and the shared ones, restarts InputPlumber while the keyboard unit is still there, then disables and removes every unit any device includes and every `/etc/systemd/system/armada-trackpad-[a-z0-9-]*.service`, with the `/etc/armada-trackpad` files their `Exec*=` lines name, so nothing is left behind whatever the device. A unit that cannot be disabled is a warning, and is removed anyway. `/etc/inputplumber` itself stays. Its `devices.d` and `capability_maps.d` go when nothing else is left in them. The registers return to their defaults at the next suspend or boot.

### Touch threshold (Nova)

`devices/retroid-pocket-nova/firmware/registers` sets register 0x80 to 5, from the firmware's 50, with the [firmware unit](#touchscreen-firmware-nova).
- **Panel:** a FocalTech FT5452 on i2c `1-0038`. Chip ID registers 0xA3/0x9F = 0x54/0x52, and Retroid's own Nova device tree says `focaltech,ft5452`. Armada's says `focaltech,ft5426`, so `edt_ft5x06` runs it as `GENERIC_FT`.
- **Register:** the sysfs `threshold` attribute for `GENERIC_FT` is register 0x80, the touch-detect threshold (mainline `edt-ft5x06.c`, `EDT_ATTR(threshold, ...)` with `M09_REGISTER_THRESHOLD`).
- **A raw write, not the sysfs attribute,** so every register is in one place: on a power-off resume, `edt_ft5x06_restore_reg_parameters` writes the driver's cached `threshold` back to 0x80, so the register is at the firmware's 50 for the ~0.4 s until the firmware unit writes 5 again. The sysfs `threshold` attribute keeps showing the driver's cache, 50, not the register.
- **Why 5:** light touches register sooner than at the firmware's 50. With nothing on the panel, the Nova's touch interrupt count stays at 0 at both 5 and 50.
- The touch-down motion hold (0x85, move threshold) has no sysfs attribute: see [Touchscreen firmware](#touchscreen-firmware-nova).

### Touchscreen firmware (Nova)

`devices/retroid-pocket-nova/firmware/registers` sets two registers of the FT5452: 0x80, the touch threshold, to 5 ([Touch threshold](#touch-threshold-nova)), and 0x85, the move threshold (THDIFF), to 1, from the firmware's 128.
- **Why 1:** after a touch-down the panel holds motion back until the finger has moved a set distance, then sends it in one jump. 0x85 sets that distance. At 128 the hold is about 50 ms at common swipe speeds, at 64 about 25 ms. At 1 a swipe starts moving after the panel's own floor of 2-3 reports (17-25 ms), and fast swipes as soon as 8 ms. A finger at rest shows no more jitter than at 128. It is meant for Steam Input's smoothing lowered and acceleration off, as the Steam Input setup in [usage.md](usage.md#steam-input-setup) says.
- **Power:** in a game, 5 and 1 measured the same as 50 and 128 (4.76 vs 4.82 W, within the sample spread).
- **Why it is lost:** the driver has no attribute for 0x85. It is written raw over i2c, and the driver doesn't restore it. The Nova's device tree has `reset-gpios`, so `edt_ft5x06` uses `EDT_PMODE_POWEROFF`: every suspend (the Nova suspends to idle) asserts reset and cuts the controller's power. Resume (`edt_ft5x06_restore_reg_parameters`, after a 300 ms settle) writes back only 0x80, 0x92 and 0x93. A reboot resets the chip too. So 0x85 needs a write at boot and after every resume, and 0x80 too once the driver's cache is not ours.
- **Why it is a system unit:** `/usr` is read-only (bootc), `/usr/lib/systemd/system-sleep` holds the OS's own hooks and `/etc/systemd/system-sleep` does not exist. `/etc/systemd/system` is read on every boot.
- **The unit,** `armada-trackpad-firmware.service`:
  - `Type=oneshot` without `RemainAfterExit`: it is inactive again after each run. That lets the next sleep start it once more;
  - `WantedBy=` `multi-user.target` and the sleep targets (`suspend`, `hibernate`, `hybrid-sleep`, `suspend-then-hibernate`), and `After=` the sleep targets, so it runs at boot and once the system is back;
  - `TimeoutStartSec=30`, and the sandbox both root units share ([Security model](#security-model)), plus `DevicePolicy=closed` with `DeviceAllow=char-i2c rw`: it can open only the `/dev/i2c-N` nodes.
- **The helper,** `ExecStart=/etc/armada-trackpad/firmware.sh /etc/armada-trackpad/registers`:
  - checks every line first. A line must have the driver `edt_ft5x06`, the register `0x80` or `0x85` and a decimal value 0..255. Any other driver or register is refused. The file cannot make root write to another chip or register, and a bad line stops all writes. The root step runs the same check (`firmware.sh --check`) before it installs anything. A bad file then stops the install instead;
  - finds the controller from the driver binding rather than a fixed bus: the one `<bus>-<addr>` entry in `/sys/bus/i2c/drivers/<driver>/`, with its `/dev/i2c-<bus>` (i2c-dev). It waits up to 10 s for them at boot, and writes nothing if several devices are bound;
  - waits 0.3 s (the driver's settle), then reads the register. It writes it with `/usr/bin/i2cset -f -y <bus> <addr> <register> <value> b` and reads it back with `/usr/bin/i2cget -f -y <bus> <addr> <register> b`, up to 5 tries 0.2 s apart. `-f` is needed because the driver has claimed the address; each call is one SMBus byte transfer, which does not race the driver's own reads;
  - logs what it did to the journal (`1-0038 (edt_ft5x06): 0x85 0x80 -> 0x01`) and logs problems as warnings. It always exits 0, so it never fails a boot or a resume. Logs: `journalctl -u armada-trackpad-firmware`.
- **Why `/etc/armada-trackpad`:** a root unit must not run anything the user can change. For this reason the helper and its data are root-owned copies and never the checkout in the user's home. `/etc` is the tree Armada documents as writable and kept across image updates; `/usr/local` on a bootc system is a link into `/var`, which was not checked on Armada. Every other file of the root step is in `/etc` too.

### Keyboard output

Keys InputPlumber owns, such as volume up on the Nova, need the keyboard output on the device's own composite.
- **What drops it:** `armada-controller-type.service` (`/usr/libexec/armada/controller-type apply`, a oneshot `After=` and `Requires=inputplumber.service`) sets every composite's outputs to `[controller type] + extras`, where the extras are the `keyboard` and `mouse` outputs it finds already there. At boot it can read before InputPlumber has created the keyboard output. It then sets the controller type alone (seen on the Nova: `[xbox-elite, keyboard]`, then `[deck-uhid]` in the same second). Being `Requires=`, it re-runs on every InputPlumber restart, with the same race.
- **The unit,** `armada-trackpad-keyboard.service`, in the Nova's `system/`: a oneshot `After=` `inputplumber.service` and `armada-controller-type.service`, `Before=display-manager.service`, `Requires=inputplumber.service` and `WantedBy=multi-user.target`, with `RemainAfterExit=yes` so an InputPlumber restart restarts it too. At boot it is done before Steam and the daemon start, so no controller is recreated under them.
- **The helper,** `ExecStart=/etc/armada-trackpad/keyboard.sh /etc/armada-trackpad/composite`: finds the composite by name (waiting up to 10 s). If it has no keyboard output, it sets `[controller type, keyboard]` plus a `mouse` it had, with the type from `controller-type get` (else `deck-uhid`). That recreates the controller, so it does nothing when the keyboard is there, and nothing when the outputs cannot be read (a warning). A controller type that cannot be read falls back to `deck-uhid` with a warning. It logs what it did and always exits 0. Logs: `journalctl -u armada-trackpad-keyboard`.
- After a mid-session InputPlumber restart the pads need a `check`, as after any InputPlumber restart.

## Security model

- **No persistent root:** the daemon runs as the session user. Root is used once, by the user, to copy static files into `/etc/inputplumber`, `/etc/armada-trackpad` and `/etc/systemd/system`; the two root oneshots it installs run for a moment at boot, after each resume and after each InputPlumber restart. There are no root daemons, timers or setuid binaries.
  - **The root step runs code from the checkout it is started from:** `system-files.sh` itself, device detection (`devices.py`) and the registers check (`firmware.sh --check`), and it copies the units, helpers and data from there. Run it from a fresh clone or a release tag, never from a checkout someone else could have changed.
  - Afterwards the root oneshots run, both as root, from the root-owned copies in `/etc/armada-trackpad`; neither talks to a user process, file or socket.
  - The firmware oneshot ([Touchscreen firmware](#touchscreen-firmware-nova)) writes the touchscreen registers, for about a second at boot and after each resume. It reads only its registers file and sysfs. Its commands, each argument validated (bus and address from the driver binding, driver and register from an allowlist, value from the file): `/usr/bin/i2cget -f -y <bus> <addr> <register> b`, `/usr/bin/i2cset -f -y <bus> <addr> <register> <value> b`, and `sleep`. On the Nova: `i2cset -f -y 1 0x38 0x85 1 b`.
  - The keyboard oneshot ([Keyboard output](#keyboard-output)) runs at boot and after each InputPlumber restart. It reads its `composite` file, reads the composite's outputs over the system bus, and when the keyboard output is missing runs Armada's `controller-type get` and calls InputPlumber's `SetTargetDevices`.
- **Socket access:** the socket is in the user's runtime dir with mode 0600. Only the user (and root) can talk to the daemon. A client such as a Decky plugin's backend runs as that user.
  - Socket input is validated against the request format and then the settings schema, with a line size limit.
- **Wiring state file:** written atomically (temporary file, then rename) with mode 0600 in the same runtime dir.
  - It is read with `O_NOFOLLOW` and used only if it is a regular file owned by the user, at most 4 KiB, with the expected JSON structure and entries matching the uhid controller pattern (`XXXX:28DE:XXXX.XXXX`). Anything else is ignored.
  - Its contents never reach `busctl`: they are only compared with the live controller identity.
- **Input handling:** the daemon reads the touchscreen and the X key stream. On `:0` it also reads one root window property, the focused app id.
  - It never logs touch coordinates.
  - To see its toggle key, it selects XInput2 `RawKeyPress` for all devices on each X display. That raw stream contains every key press on the display, including typing into Steam's login on `:0`.
  - The daemon drops every other key event without logging it. Any X client of the same user can already read this stream, so this gives the daemon nothing new.
  - It reads every report of the Steam Deck controller's hidraw node while `toggle_chord` is on. The node is opened read-only and never grabbed. Only the held buttons are kept and compared with the chord, and nothing from the reports is logged. The user's `input` group membership already allows the read.
- **Prerequisites:** the session user must already be able to write `/dev/uinput` and read the touchscreen's event node. The daemon uses that access. It does not grant it.
- **D-Bus:** the daemon calls InputPlumber with the user's own D-Bus rights.
  - It trusts InputPlumber's replies: object paths read from them become `busctl` arguments. InputPlumber runs as root.
  - Composite and gamepad target paths must match InputPlumber's object tree from the first character, so a reply value that starts with `-` never reaches busctl as an option.
- **Root unit sandbox:** both root units set `NoNewPrivileges`, `PrivateNetwork`, `PrivateTmp`, `ProtectHome`, `ProtectSystem=strict`, `RestrictAddressFamilies=AF_UNIX`, `ProtectKernelTunables`, `ProtectKernelModules`, `ProtectControlGroups`, `LockPersonality`, `RestrictRealtime` and `SystemCallArchitectures=native`. The system bus is a path socket, so the keyboard unit can still connect to it from its private network namespace. Neither sets `CapabilityBoundingSet`: which capabilities InputPlumber's D-Bus policy and the i2c ioctls need as root has not been checked on the device.
- **Subprocesses:** `busctl` is called with argument lists, never through a shell. No network access.

## Testing

- **Host:** `uv run pytest`, `uv run ruff check` and `uv run mypy`. Pure logic (settings, modes, geometry, the mapper, the tracker, the focus rules, the control protocol, CLI mapping) is tested directly. `tests/test_pad_sequence.py` pins the exact evdev output of the pads. Hardware and InputPlumber are tested through fakes. The `busctl` fake replays outputs captured on a Nova. The shell scripts must pass `shellcheck` (the `shellcheck-py` dev dependency), checked as POSIX sh. The firmware helper's input checks run on the host. Its writes are checked on the Nova. `install.sh` and the root step run on the host with faked `uv`, `systemctl` and root commands, the root step into a scratch tree (`ARMADA_TRACKPAD_DESTDIR`, ignored under sudo). CI (`.github/workflows/ci.yml`) runs the same checks on Python 3.12 and 3.14 for every pull request and every push to `main`.
- **On a Nova:** `tests/device/e2e_hid.py` drives a fake uinput touchscreen into a foreground daemon and asserts the Deck controller's HID reports:
  - left pad in bytes 16-19 (touch bit 0x08, press 0x02) with force in bytes 56-57;
  - right pad in bytes 20-23 (touch bit 0x10, press 0x04) with force in bytes 58-59.
- **Manual:** in any game with a Steam Input layout, check:
  - the Steam-bound toggle;
  - both pads;
  - haptics;
  - live `set`;
  - suspend/resume, after which `sudo i2cget -f -y 1 0x38 0x85 b` reads `0x01` and `journalctl -u armada-trackpad-firmware -b` shows a run for the resume;
  - an InputPlumber restart;
  - a controller-type switch.
