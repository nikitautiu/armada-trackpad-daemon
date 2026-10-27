# Contributing

Thanks for helping. This file covers the development setup, the checks a change must pass, the code
and test conventions, how the root step is kept safe, and how to add a device.
[docs/design.md](docs/design.md) explains how the daemon works and why; read the parts your change
touches first. It also records the InputPlumber, gamescope and Steam behaviour the daemon depends on,
with file:line evidence: check there before assuming how the input stack behaves.

## Development setup

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). Development happens on any Linux
host. Only the device tests need a supported handheld.

```sh
uv sync                                    # the dev environment, with pytest, ruff, mypy, shellcheck and pre-commit
uv run pytest                              # host tests, shellcheck on every shell script included
uv run ruff check                          # lint
uv run ruff format --check src tests dev   # formatting (drop --check to apply it)
uv run mypy                                # types: src strict, tests typed with relaxed overrides
uv run pre-commit install                  # once: checks on each commit, tests on each push
uv run pre-commit run --all-files          # every check on every file, Vale included
```

The pre-commit hooks lint the Markdown with [Vale](https://vale.sh) and the
[ai-tells](https://github.com/tbhb/vale-ai-tells) style. pre-commit installs Vale from PyPI on first use.

CI runs the four Python checks on Python 3.12 and 3.14, and Vale, for every pull request. All of them must pass.

## Runtime constraints

- The daemon is a systemd **user** service. There is no persistent root process: root is used only by
  the one-time root step, `scripts/system-files.sh`, and by the short oneshot units it installs.
- It doesn't read or write a **config file**. Settings start from built-in device defaults and change at
  runtime over the control socket.
- Its only runtime dependency is `python-xlib`. The daemon talks to InputPlumber through `busctl`.

## Code style

ruff (configured in `pyproject.toml`) and mypy enforce most of this.

- Line length 120, double quotes, 4-space indent (`ruff format`).
- Built-in generics (`list[str]`), `X | None` rather than `Optional`, and the `type` statement for aliases.
- Type hints on every function and method, including `-> None`. mypy runs in strict mode on `src`.
- No `Any` except for decoded JSON or other wire data, with a comment saying why. Avoid `# noqa` and
  `# type: ignore`; when one is unavoidable, justify it in a comment.
- Accept abstract inputs (`Sequence`, `Mapping`, `Iterable`) and return concrete types.
- At most 5 function arguments; beyond that, `# noqa: PLR0913` with a justification.
- Descriptive names. A leading `_` marks module-private functions and classes.
- Delete dead code: unused imports and variables, commented-out blocks.

### Design

- Readability first: pick the structure a reader understands fastest.
- Prefer small functions with one goal that take their inputs as parameters and return their outputs.
  Keep I/O and side effects at the edges, in thin wrappers, so the logic is testable without hardware.
  `Trackpad`, `Mapper` and `Tracker` are synchronous and don't do I/O. Only `daemon.py` and `control.py`
  use asyncio.
- Model data so that wrong states cannot be represented. Avoid loose `dict` or `str` fields.
- Things that change together live together. There is no shared constants module: each constant lives
  with the code that owns it.
- Fix problems at their source rather than with special cases at the call sites.

### Comments and docstrings

- Comment the **why**: intent, constraints, surprising choices. Never narrate what the code does or
  how it changed ("added", "previously", "we").
- A constant or type alias gets a `#` comment on the line after it, not a docstring.
- Google-style docstrings on public modules, classes and functions, with `Args:`, `Returns:` and
  `Raises:` sections. Document the caller's view: behaviour, side effects, constraints and errors.
- Wrap identifiers in single backticks. No reST markup (`:func:`, `:param:`, `::` blocks).
- No em dashes in comments, docstrings or docs: use a comma, a colon or a new sentence.

## Tests

- Write the test first: a failing test for the behaviour, then the simplest code that passes it, then
  refactor while green. Every change in behaviour comes with a test.
- Fake only at system boundaries: the touchscreen, uinput, `busctl`, X, the filesystem and commands.
  The shared fakes are in `tests/fakes.py`. Never fake an internal module; code that is hard to test
  that way is a design problem.
- Group tests in classes by concern, and name them `test_<what>_when_<condition>`.
- Every test, test class, fixture and helper has a docstring. A fixture's docstring says what its data
  represents and why it has that structure.
- One concept per test. Use `pytest.mark.parametrize` instead of copied tests, and `pytest.approx` for
  floats.
- Use synthetic data with fixed, predictable values, never random data.
- The shell scripts are tested on the host with faked commands, and the root step installs into a
  scratch tree. They must pass shellcheck as POSIX sh.

### The behaviour pin

`tests/test_pad_sequence.py` pins the exact evdev output of the virtual pads for a fixed touch
sequence. Its expected values change only for an intended change in behaviour, never in a refactor.
A pull request that changes them must say why the new output is the intended one.

### Device tests

These need a supported device running Armada OS, with the daemon installed and the root step done.

- `dev/deploy.sh HOST` copies your checkout to the device over ssh and runs `install.sh` there.
- `tests/device/e2e_hid.py` creates a fake touchscreen, runs a daemon on it in the foreground, and
  checks the Steam Deck controller's HID reports. Stop the service first, and start it again after:

  ```sh
  ssh HOST 'systemctl --user stop armada-trackpadd;
    ~/.local/share/armada-trackpad-daemon/.venv/bin/python ~/armada-trackpad-daemon/tests/device/e2e_hid.py;
    systemctl --user start armada-trackpadd'
  ```

  It must print `PASS`.
- `dev/latency_probe.py` measures the latency from a touch to the controller's report; usage is in its
  header.
- Check by hand in any game with a Steam Input layout: the bound toggle, both pads, haptics, a live
  `set`, suspend and resume, an InputPlumber restart and a controller type switch.

## Security model of the root step

The root step runs as root from the user's checkout, and the units it installs run as root at boot,
after each resume and after each InputPlumber restart. Changes there get extra scrutiny.
[docs/design.md](docs/design.md#security-model) has the full model; in short:

- `scripts/system-files.sh` checks the whole device folder before it writes anything. Neither the device
  folder, the shared `data/common/` folder nor anything in them may be a symlink.
- A root unit must be named `armada-trackpad-[a-z0-9-]*.service`, and its `Exec*=` lines may run and
  name only files in `/etc/armada-trackpad/` and under `/usr/`, in a strict format (no quotes, escapes,
  specifiers, variables or line continuations), so what the script checks is what systemd runs.
- Helpers run from root-owned copies in `/etc/armada-trackpad/`, never from the checkout. They validate
  every input against an allowlist, log problems as warnings and always exit 0, so they never fail a
  boot or a resume.
- Both units share a systemd sandbox (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateNetwork` and
  more), which `tests/test_shell_scripts.py` enforces.
- Device detection in the root step runs `devices.py` on the system Python with `-I -S`, so
  `devices.py` must import only the standard library (a test checks this).

A pull request that changes a root unit, a helper or `system-files.sh` must explain what runs as root
and why it is safe.

## Adding a device

A device is a `DeviceProfile` plus the data files that differ from the shared ones. Everything else
is shared.

1. **The profile.** Add a `DeviceProfile` in `src/armada_trackpad/devices.py` and list it in `DEVICES`:
   - `id`: a lowercase name such as `vendor-model`. It is also the name of the device's data folder.
   - `name`: the model's name for people.
   - `touchscreen_name`: a substring of the touchscreen's evdev name, from the `N: Name=` lines of
     `cat /proc/bus/input/devices`. Pick one that matches only the touchscreen.
   - `panel_mm`: the touch panel's active area as width and height in mm. Take it from the datasheet or measure it.
     It scales the pads to the Deck's 32.5 mm, so measure carefully.
   - `devicetree_compatible` (ARM devices: `tr '\0' '\n' < /sys/firmware/devicetree/base/compatible`) or
     `dmi_product_names` (x86 devices: `cat /sys/class/dmi/id/product_name`): what identifies the model.
   - `inputplumber_composite`: the name of the device's own InputPlumber composite, the one that owns
     the controller. It is the `name:` of InputPlumber's stock config for the device in
     `/usr/share/inputplumber/devices/`; `busctl --system tree org.shadowblip.InputPlumber` and
     `busctl --system get-property org.shadowblip.InputPlumber <path> org.shadowblip.Input.CompositeDevice Name`
     show it on the running device.
   - `toggle_chord`: buttons from `chord.BUTTONS` that toggle trackpad mode when all are held. Pick a
     combination that games and streaming clients leave alone (Moonlight quits on
     Select + Start + L1 + R1), or leave it empty.
   - `x_displays`: gamescope's Xwayland servers. The default fits Armada.
2. **The data folder,** `src/armada_trackpad/data/devices/<id>/`. A file there replaces the shared
   file of the same path in `data/common/`, so add only what differs:
   - `inputplumber/devices.d/49-armada_trackpad_left.yaml` and `..._right.yaml`: copy the Nova's and
     change the `matches` block to your device's devicetree `compatible` or DMI product name. A test
     checks that these values are the profile's.
   - `inputplumber/composite`: the profile's `inputplumber_composite` on one line. A test checks that
     they agree.
   - `system/`: the device's root units and their helpers, if it needs any. Each device has its
     own, since a unit is validated only on the device it comes with. The Nova's keyboard unit fixes a
     race in Armada's controller-type unit. Copy it only once you have seen the problem on your device.
   - `firmware/registers`, only if the touchscreen needs registers rewritten after each resume. The
     firmware helper (`system/firmware.sh`) allows only the Nova's driver (`edt_ft5x06`) and its two
     registers (0x80, 0x85). A new chip needs its own allowlist in the helper, a matching
     `REGISTER_LINE` in `tests/test_devices.py`, and a security review of the change.
3. **The tests.** Most data tests are parametrized over `DEVICES` and cover the new device by
   themselves: the `matches` values and the composite name agree with the profile, the pad configs
   adopt the pad devices by name, the registers lines are well formed, and every file a unit names is
   installed once. Add a detection test with your device's `compatible` string or DMI name in
   `tests/test_devices.py`.
4. **On the device.** Run the root step and `tests/device/e2e_hid.py`. It must print `PASS`. Then do
   the manual checks under [Device tests](#device-tests).
5. **The docs.** Add the device to the README's supported note, the table in `docs/usage.md` and the changelog.

## Commits and pull requests

- Use [Conventional Commits](https://www.conventionalcommits.org/): `feat(cli): ...`, `fix(scripts): ...`,
  `docs: ...`. Keep each commit focused, with its tests in the same commit.
- Open pull requests against `main`. Describe the change and how you tested it, on the host and, for
  anything that touches the device, on which device.
- A change to the control protocol that removes, renames or changes the meaning of a method, param or
  field must raise `PROTOCOL` in `trackpad.py` ([docs/design.md](docs/design.md#control-protocol), "Versioning").
  Additions keep it.
- Add a line to the `Unreleased` section of [CHANGELOG.md](CHANGELOG.md) for a change users or client
  authors will notice.

## Reporting issues

See [Reporting a problem](README.md#reporting-a-problem) in the README for what to include. Report
security problems privately, as [SECURITY.md](SECURITY.md) describes.
