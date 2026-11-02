# Security model

## What this project is

A user-level convenience layer: it reads gamepad events and, on a button
combo, asks the *already logged-in* GNOME session to lower its lock screen and
then brings Steam Big Picture forward.

It does **not** authenticate you to the system. It does not touch GDM, PAM,
`/etc/pam.d`, autologin, SSH, or your account password.

## Trust boundaries

- The service runs as the desktop user. No root, no setuid, no capabilities, no
  network access.
- The combo is a **local physical-access** gesture. It is not an account
  credential and is not compared against any secret store.
- The config file is user-owned (`0600`) and contains no passwords. It holds
  the combo in plain text.
- The screen lock is a privacy boundary, not a privilege boundary. Any process
  running as the user can call `org.gnome.ScreenSaver.SetActive(false)`; the
  combo is simply another such process. It grants nothing that local code did
  not already have.
- A controller allowlist (`allowed_controller_ids`) is a convenience filter.
  An unprivileged local process can create a virtual gamepad with any
  vendor/serial, and an attacker with disk access can edit the config.
- The daemon sets `LD_PRELOAD` to the system `extest` library for Steam child
  processes only. It does not preload code into itself, the desktop session, or
  unrelated applications. Disable it with `"preload_extest": false`.

## What it protects against

- A housemate or guest pressing buttons on your controller and getting in.
- A different, non-allowlisted controller triggering the combo.
- Accidental activation while a game is running (the combo is ignored when the
  session is unlocked, and focus is never taken from a game).
- Steam Input reacting to Guide-button chords while the screen is locked
  (the daemon grabs the controller with `EVIOCGRAB` while locked by default).
- A wedged Steam stealing CPU/RAM indefinitely (optional watchdog, conservative
  and disabled while a game/download/shader job runs).

## What it does not protect against

- Someone who can reboot the machine: login is still password-gated, but disk
  data is only protected if you use full-disk encryption.
- Malware or any process already running as your user.
- Someone who reads the combo from `~/.config/couch-controller/config.json`.
- Brute force by a patient human: the combo is short. The 3-second action
  debounce slows this down but does not make it strong.
- Physical access to the disk or bootloader.

For real protection use full-disk encryption, a BIOS/UEFI password, and
disabled USB boot.

## Reporting

Open a GitHub issue for non-sensitive bugs. For a security issue, use GitHub's
private vulnerability reporting on the repository rather than a public issue.
