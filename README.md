# gnome-game-login

Turn an Xbox controller into a couch-mode switch for a GNOME desktop.

When you pick up the controller (or enter a button combo while the screen is
locked), the session unlocks and Steam Big Picture comes to the front. No
autologin, no stored password, no root.

Tested on **Nobara 44 / GNOME 50 / Wayland** with an **Xbox Elite Series 2**
over the **Xbox Wireless Adapter** (and USB), Steam 1.0.0.87.

## What it does

- Watches Xbox controllers through `evdev` **without grabbing** them, so Steam
  Input, Proton and games keep working normally.
- **Combo while locked:** unlocks the GNOME session, then restores the Steam
  state (launch Big Picture, switch to it, or just focus it).
- **Controller connects while unlocked:** opens or focuses Big Picture after a
  short delay, unless a game is running.
- **Game running:** unlock only. The game keeps/regains focus; Big Picture is
  not forced in front of it.
- **Force restart:** hold **View + Menu** for 5 seconds.
- **Hang watchdog:** restarts Steam if it is genuinely wedged (sustained CPU,
  no game, no download, no shader compilation).

## Behaviour by state

| Steam state | Combo while locked | Controller connect (unlocked) |
|---|---|---|
| not running | unlock → launch Big Picture | launch Big Picture |
| desktop mode | unlock → switch to Big Picture → focus | switch to Big Picture → focus |
| Big Picture | unlock → focus | focus |
| game running | unlock only (game keeps focus) | do nothing |
| game running in Big Picture | unlock only (game keeps focus) | do nothing |

The combo is ignored while the session is already unlocked, so it cannot
interrupt a game. Setting a game aside to show Big Picture is not possible
without taking focus from the game, which is why it is not attempted.

## Requirements

- GNOME (Wayland or X11) with `gdbus` and a systemd user session
- Python 3.10+
- `python3-evdev`
- `wmctrl`
- Steam installed at `/usr/bin/steam` (configurable)
- A controller that appears as an Xbox pad (`vendor 045e`)
- On Wayland: `extest` (recommended) so Steam's virtual keyboard and desktop
  controller layout do not trigger GNOME's Remote Desktop prompt

Fedora / Nobara:

```sh
sudo dnf install python3-evdev wmctrl
```

Debian / Ubuntu:

```sh
sudo apt install python3-evdev wmctrl
```

## Install

```sh
git clone https://github.com/dynamiquel/gnome-game-login
cd gnome-game-login
./install.sh --with-idle
```

`--with-idle` also sets GNOME to blank and lock after 10 minutes and never
suspend on AC. Omit it to leave your power settings alone.

The installer copies two files, writes `~/.config/couch-controller/config.json`
only if it does not exist, and enables a user service. It does **not** change
GDM, autologin or your account password.

## Configure the combo

Edit `~/.config/couch-controller/config.json`:

```json
{
  "combo": ["UP", "DOWN", "LEFT", "RIGHT"],
  "press_gap_ms": 2500,
  "window_ms": 10000,
  "grab_when_locked": true
}
```

Valid names: `A B X Y UP DOWN LEFT RIGHT LB RB START BACK GUIDE`.

- Presses can be as fast as you like.
- `press_gap_ms` is the maximum pause **between** presses.
- `window_ms` is the maximum total time for the sequence.

Restart the service after editing:

```sh
systemctl --user restart couch-controller.service
```

### Pin the controller (optional)

`allowed_controller_ids` restricts the combo to specific controllers. Run:

```sh
~/.local/bin/couch-controller.py --status
```

and copy the `id=` value into the list:

```json
"allowed_controller_ids": ["0123456789ABCDEF"]
```

An empty list accepts any Xbox controller. This is a convenience filter, not a
security boundary (see below).

## Locked-screen input

GNOME's lock screen locks the session UI, not gamepads. Steam Input keeps
reading `/dev/input` in the background and can still act on Guide-button
chords, including its power menu (which can suspend the machine) while the
screen is locked.

With `grab_when_locked` enabled (the default), the daemon takes exclusive
control of the controller with `EVIOCGRAB` while the session is locked. Steam
cannot see the device, but the daemon still receives the unlock combo, and it
releases the controller immediately when the session unlocks.

If Steam has already grabbed the device, the daemon logs a warning and retries;
disabling Steam's Guide Button Chord Layout / "Guide button focuses Steam" is
the fallback.

## Force restart

Hold **View + Menu** for 5 seconds. Steam is shut down gracefully, then
SIGTERM, then SIGKILL, and relaunched in Big Picture if the session is unlocked.
Tune with `force_restart_hold_ms`.

This is deliberately explicit: it will interrupt a running game or download.

## Steam hang watchdog

Every 15 s the service samples the combined CPU of `steam` and
`steamwebhelper`. Steam is restarted only when **all** of these are true:

- CPU ≥ 85% continuously for 4 minutes
- no game running
- no download in progress
- no shader compilation (`fossilize`)

Tune with `hang_cpu_percent`, `hang_seconds`, `hang_sample_seconds`, or set
`"hang_watchdog": false`.

## Steam input on Wayland

GNOME 50 starts Xwayland with EI portal support. Steam's on-screen keyboard and
controller desktop layout inject input with **XTEST**, so Xwayland asks the
RemoteDesktop portal for an EIS socket. The resulting GNOME permission dialog
has to be accepted again for every new XTEST client, because Xwayland does not
request a persistent permission.

`couch-controller` avoids that path by preloading `extest` into Steam launches
when it is installed. extest replaces XTEST with a `uinput` virtual device, so
Steam input works without the portal. No wrapper script or desktop-file edit is
needed; the daemon sets `LD_PRELOAD` only for Steam and its children.

Defaults:

```json
{
  "preload_extest": true,
  "extest_library": "/usr/${LIB}/extest/libextest.so"
}
```

`${LIB}` is expanded by the dynamic loader to `lib64` or `lib`, covering both
32- and 64-bit Steam processes. Steam's pressure-vessel runtime understands the
same token and maps the library into its containers.

- Set `"preload_extest": false` to disable it.
- Set `"extest_library"` to a custom path if your distribution installs the
  libraries elsewhere.
- `--doctor` reports whether extest was found and warns on Wayland when it is
  missing.
- extest is not required on X11; XTEST and XWayland work normally there.

## Tests

No controller, Steam or session changes are required:

```sh
./test.sh
```

or individually:

```sh
./couch-controller.py --self-test   # combo matcher, state planner, watchdog logic, hold detector
./couch-controller.py --doctor      # dependencies, config, session, controller detection
./couch-controller.py --status      # current lock/Steam/game state + controllers
```

The self-test uses the **real evdev codes** captured from an Elite Series 2,
so regressions such as swapped X/Y buttons fail the tests.

## Files

| File | Purpose |
|---|---|
| `couch-controller.py` | the whole daemon: evdev watcher, combo matcher, state planner, Steam control, watchdog, tests |
| `config.example.json` | example configuration (combo `UP DOWN LEFT RIGHT`) |
| `couch-controller.service` | systemd user unit |
| `install.sh` | copies files, writes config if missing, enables the service |
| `uninstall.sh` | disables/removes the service and script (`--purge` removes config) |
| `test.sh` | runs the self-test and doctor |
| `SECURITY.md` | threat model |

## How it works

1. `evdev` reports each Xbox controller. The daemon opens the gamepad node and
   reads events; it never calls `EVIOCGRAB`, so other consumers still see them.
2. Presses are normalised to logical names (`BTN_NORTH` is **X**, `BTN_WEST`
   is **Y**). A rolling window matches the last N presses with a per-press gap
   and a total window.
3. On a match, a pure state planner decides what to do from four booleans:
   session locked, game running, Steam running, Big Picture window present.
4. Unlock uses `org.gnome.ScreenSaver.SetActive(false)` on the session bus and
   `loginctl unlock-session`, then resets the Mutter idle timer.
5. Focus uses `wmctrl -i -a` on the XWayland Big Picture window; Mutter honours
   EWMH activation.
6. Steam launches inherit an `LD_PRELOAD` pointing at `extest` when it is
   installed, so Steam's XTEST-based keyboard and desktop controller input do
   not go through the Wayland RemoteDesktop portal.

## Security model

Read `SECURITY.md` for the full threat model. The short version:

- The service runs as **you**, with no root, no setuid, no network code, and no
  stored credentials.
- Boot, logout, SSH and `sudo` still require your account password. There is no
  autologin.
- The combo is a **local physical-access convenience**, not an account
  credential. It is stored in plain text and is short enough to guess with
  enough patience.
- Screen locking protects against people, not processes. Anything already
  running as your user can unlock the session directly; the combo adds no new
  privilege for local code.
- An allowlisted controller can be spoofed by a local process (uinput), and a
  physical attacker can edit your config. If you need real protection, use
  full-disk encryption and a BIOS password.
- The combo does nothing at the GDM greeter: a controller cannot log in to a
  Wayland session, and this project does not try to inject input there.

## Limitations

- GNOME-specific (`org.gnome.ScreenSaver`/Mutter). Other desktops would need a
  different unlock/focus backend.
- A running game is never interrupted; if you want Big Picture instead, quit or
  alt-tab to Steam first.
- Focus relies on the window title `Steam Big Picture Mode` and XWayland. A
  native Wayland Steam UI or a localized title would need a matching change.
- The hang watchdog is a heuristic. It deliberately errs on the side of not
  killing Steam while a game, download or shader job is active.
- Only tested with Xbox controllers (`vendor 045e`). Other pads can be added by
  extending `BUTTONS` and the vendor check.

## Troubleshooting

```sh
~/.local/bin/couch-controller.py --doctor
~/.local/bin/couch-controller.py --status
journalctl --user -u couch-controller.service -f
```

- **Combo does nothing:** check `--status` shows the controller and
  `locked: True`; verify the combo in `config.json`; ensure the session is
  locked (the combo is intentionally ignored when unlocked).
- **Controller not detected:** it must expose `BTN_SOUTH` and a D-pad. Check
  permissions with `ls -l /dev/input/event*`; joystick nodes get a `uaccess`
  ACL for the seated user.
- **Unlock works but focus does not:** ensure `wmctrl` is installed and
  `wmctrl -lx` lists `Steam Big Picture Mode`.
- **X and Y swapped?** They are not: on Linux `BTN_NORTH` is X. The self-test
  encodes the real mapping.
- **GNOME asks to enable Remote Desktop when using the Steam keyboard:** run
  `--doctor` and check the `extest Steam input` line. Install `extest` if it is
  missing, or set `"preload_extest": false` only if you do not need Steam's
  virtual keyboard / desktop controller input.

## Uninstall

```sh
./uninstall.sh          # keeps config
./uninstall.sh --purge  # removes config too
```

GNOME idle/lock settings are left unchanged.

## License

MIT. See `LICENSE`.
