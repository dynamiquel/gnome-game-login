#!/usr/bin/python3
"""Couch mode controller daemon.

Watches Xbox controllers (USB or Xbox Wireless Adapter) *without* grabbing
them, so Steam Input and the desktop keep working normally.

On the combo:
  * if the GNOME session is locked: unlock, then open/focus Steam Big Picture
  * if a game is running: unlock only, so the game keeps/regains focus
  * if the session is already unlocked: ignore (never steal focus from a game)

On controller connect (while unlocked): open or focus Big Picture, unless a game
is running.

Force restart: hold View + Menu for 5 seconds.

A watchdog restarts Steam if it is genuinely wedged: sustained high CPU, no
game running, no download and no shader-compile work in progress.

Runs as a systemd --user service. Needs no root and no group changes:
joystick event nodes are ACL'd to the seated user by systemd's uaccess rule.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import evdev
from evdev import ecodes

LOG = logging.getLogger("couch-controller")
__version__ = "1.2.0"

CONFIG_DIR = os.path.expanduser("~/.config/couch-controller")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

RESCAN_INTERVAL = 3.0      # seconds between hotplug scans
DBUS_TIMEOUT = 4.0
CLK_TCK = os.sysconf("SC_CLK_TCK")

STEAM_DIRS = (
    os.path.expanduser("~/.local/share/Steam"),
    os.path.expanduser("~/.steam/steam"),
)

# Steam's virtual keyboard and desktop controller layout use XTEST.  On
# Wayland, Xwayland forwards that through the RemoteDesktop portal, which asks
# GNOME for permission on every new client.  extest replaces XTEST with a
# uinput device, so preload it for Steam when it is installed.  ${LIB} is
# expanded to lib64/lib by the loader (and by Steam's pressure-vessel runtime),
# so the same setting covers both 32- and 64-bit Steam processes.
EXTEST_LIBRARY = "/usr/${LIB}/extest/libextest.so"

# Button name -> possible evdev events.  A hat direction can arrive either as
# ABS_HAT0* (xpad/xone) or as BTN_DPAD_* (some HID drivers), so both are
# accepted and treated as the same logical press.
#
# NOTE the Linux naming: BTN_X is BTN_NORTH (the left face button) and BTN_Y is
# BTN_WEST.  Getting this backwards makes X and Y swap.
BUTTONS = {
    "A": (("key", ecodes.BTN_SOUTH),),
    "B": (("key", ecodes.BTN_EAST),),
    "X": (("key", ecodes.BTN_NORTH),),
    "Y": (("key", ecodes.BTN_WEST),),
    "UP": (("hat", ecodes.ABS_HAT0Y, -1), ("key", ecodes.BTN_DPAD_UP)),
    "DOWN": (("hat", ecodes.ABS_HAT0Y, 1), ("key", ecodes.BTN_DPAD_DOWN)),
    "LEFT": (("hat", ecodes.ABS_HAT0X, -1), ("key", ecodes.BTN_DPAD_LEFT)),
    "RIGHT": (("hat", ecodes.ABS_HAT0X, 1), ("key", ecodes.BTN_DPAD_RIGHT)),
    "LB": (("key", ecodes.BTN_TL),),
    "RB": (("key", ecodes.BTN_TR),),
    "START": (("key", ecodes.BTN_START),),
    "BACK": (("key", ecodes.BTN_SELECT),),
    "GUIDE": (("key", ecodes.BTN_MODE),),
}

# Reverse map: one evdev event token -> logical button name.
EVENT_TO_BUTTON = {alt: name for name, alts in BUTTONS.items() for alt in alts}

DEFAULT_CONFIG = {
    "combo": ["UP", "DOWN", "LEFT", "RIGHT"],
    "press_gap_ms": 2500,
    "window_ms": 10000,
    "allowed_controller_ids": [],
    "launch_on_connect_when_unlocked": True,
    "grab_when_locked": True,
    "launch_delay_ms": 2000,
    "min_action_interval_ms": 3000,
    "vendor_id": "045e",
    "steam_binary": "/usr/bin/steam",
    "preload_extest": True,
    "extest_library": EXTEST_LIBRARY,
    "force_restart_hold_ms": 5000,
    "hang_watchdog": True,
    "hang_cpu_percent": 85,
    "hang_seconds": 240,
    "hang_sample_seconds": 15,
}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                user_cfg = json.load(fh)
            if not isinstance(user_cfg, dict):
                raise ValueError("config must be a JSON object")
            cfg.update(user_cfg)
        except (OSError, ValueError) as exc:
            LOG.error("could not read %s (%s); using defaults", CONFIG_PATH, exc)
    else:
        os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_CONFIG, fh, indent=2)
            fh.write("\n")
        os.chmod(CONFIG_PATH, 0o600)
        LOG.info("wrote default config to %s", CONFIG_PATH)

    combo = cfg.get("combo") or []
    unknown = [name for name in combo if name not in BUTTONS]
    if unknown:
        raise ValueError(f"unknown button(s) in combo: {', '.join(map(str, unknown))}")
    if not combo:
        raise ValueError("combo must not be empty")
    if not os.path.isabs(str(cfg["steam_binary"])):
        raise ValueError("steam_binary must be an absolute path")
    try:
        int(str(cfg["vendor_id"]), 16)
    except ValueError as exc:
        raise ValueError("vendor_id must be a hex string such as 045e") from exc
    return cfg


# --------------------------------------------------------------------------
# combo matcher
# --------------------------------------------------------------------------

class ComboMatcher:
    """Matches the last N logical button presses against the combo.

    A rolling window is used instead of a strict state machine, so an extra
    press before or inside the run still matches as long as the final N
    presses are exactly the combo.  Wrong presses genuinely break it, and a
    completed match clears the window so it cannot fire twice.
    """

    def __init__(self, steps: list[str], window_s: float, gap_s: float):
        self.steps = list(steps)
        self.window_s = window_s
        self.gap_s = gap_s
        self.recent: deque[tuple[str, float]] = deque(maxlen=len(self.steps))

    def reset(self) -> None:
        self.recent.clear()

    def feed(self, name: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now

        # A pause longer than the allowed gap abandons the partial sequence.
        if self.recent and now - self.recent[-1][1] > self.gap_s:
            self.recent.clear()

        self.recent.append((name, now))
        if len(self.recent) < len(self.steps):
            return False

        presses = list(self.recent)
        if [press[0] for press in presses] != self.steps:
            return False
        if presses[-1][1] - presses[0][1] > self.window_s:
            return False

        self.recent.clear()
        return True


# --------------------------------------------------------------------------
# state planning (pure, so it can be tested)
# --------------------------------------------------------------------------

def plan_actions(locked: bool, game: bool, steam: bool, big_picture: bool) -> list[str]:
    """Return the ordered actions for the current Steam/session state."""
    if not locked:
        # Triggered by a controller connecting while the desktop is in use.
        if game:
            return []
        if steam and big_picture:
            return ["focus"]
        if steam:
            return ["open_bp"]
        return ["launch_bp"]

    # Locked: unlock first, then restore the couch state.
    if game:
        return ["unlock"]
    if steam and big_picture:
        return ["unlock", "focus"]
    if steam:
        return ["unlock", "open_bp"]
    return ["unlock", "launch_bp"]


def should_restart_hung(cpu_percent: float, hot_seconds: float, cfg: dict, busy: bool) -> bool:
    return (
        not busy
        and cpu_percent >= float(cfg["hang_cpu_percent"])
        and hot_seconds >= float(cfg["hang_seconds"])
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _gdbus(dest: str, path: str, method: str, *args: str) -> str:
    cmd = ["gdbus", "call", "--session", "-d", dest, "-o", path, "-m", method, *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=DBUS_TIMEOUT)
        if proc.returncode != 0:
            LOG.debug("gdbus %s failed: %s", method, proc.stderr.strip())
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.warning("gdbus %s error: %s", method, exc)
        return ""


def _expand_lib_path(path: str) -> list[str]:
    """Expand ld.so's ${LIB} token for both 64- and 32-bit libraries."""
    if "${LIB}" not in path and "$LIB" not in path:
        return [path]
    return [
        path.replace("${LIB}", lib).replace("$LIB", lib)
        for lib in ("lib64", "lib")
    ]


def _extest_available(path: str) -> bool:
    if any(os.path.isfile(candidate) for candidate in _expand_lib_path(path)):
        return True
    if "${LIB}" in path or "$LIB" in path:
        # Some distributions use a multiarch directory such as
        # /usr/lib/x86_64-linux-gnu instead of lib64/lib.
        pattern = path.replace("${LIB}", "*").replace("$LIB", "*")
        return any(os.path.isfile(candidate) for candidate in glob.glob(pattern))
    return False


def _steam_env(cfg: dict) -> dict[str, str]:
    """Return the environment to launch Steam with.

    extest is optional: if it is not installed, or the user disabled it, Steam
    keeps the normal XTEST/Xwayland behaviour.  An existing LD_PRELOAD (from a
    wrapper or another tool) is preserved.
    """
    env = os.environ.copy()
    if not cfg.get("preload_extest", True):
        return env

    library = os.path.expanduser(str(cfg.get("extest_library") or EXTEST_LIBRARY))
    if not _extest_available(library):
        LOG.debug("extest not found at %s; leaving LD_PRELOAD unchanged", library)
        return env

    preload = [part for part in env.get("LD_PRELOAD", "").split(":") if part]
    if library not in preload:
        preload.insert(0, library)
    if preload:
        env["LD_PRELOAD"] = ":".join(preload)
    return env


def _spawn(cmd: list[str], env: dict[str, str] | None = None) -> None:
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=os.path.expanduser("~"),
            env=env if env is not None else os.environ.copy(),
        )
    except OSError as exc:
        LOG.error("could not run %s: %s", " ".join(cmd), exc)


def _iter_cmdlines():
    try:
        pids = os.listdir("/proc")
    except OSError:
        return
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if raw:
            yield raw.replace(b"\0", b" ").decode("utf-8", "replace")


def _iter_pids() -> list[str]:
    try:
        return [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []


def _proc_comm(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/comm", "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _proc_cpu_ticks(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except OSError:
        return None
    end = data.rfind(")")
    if end < 0:
        return None
    parts = data[end + 2:].split()
    try:
        return int(parts[11]) + int(parts[12])   # utime + stime
    except (IndexError, ValueError):
        return None


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

class Actions:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.last_action = 0.0
        self.pending_launch: float | None = None

    # --- session state -------------------------------------------------
    def lock_state(self) -> bool | None:
        """True/False, or None when the lock state cannot be read.

        None matters for the grab logic: a gdbus timeout must not make the
        daemon believe the session is unlocked and release the controller.
        """
        out = _gdbus(
            "org.gnome.ScreenSaver",
            "/org/gnome/ScreenSaver",
            "org.gnome.ScreenSaver.GetActive",
        )
        if not out:
            return None
        return out.lower().startswith("(true")

    def session_locked(self) -> bool:
        return self.lock_state() is True

    def unlock(self) -> bool:
        LOG.info("unlocking session")
        if self.dry_run:
            return True
        _gdbus(
            "org.gnome.ScreenSaver",
            "/org/gnome/ScreenSaver",
            "org.gnome.ScreenSaver.SetActive",
            "false",
        )
        try:
            subprocess.run(
                ["loginctl", "unlock-session"],
                capture_output=True,
                text=True,
                timeout=DBUS_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.debug("loginctl unlock-session: %s", exc)
        # Reset the idle timer so the compositor does not immediately blank
        # and re-lock after the shield is lowered.
        _gdbus(
            "org.gnome.Mutter.IdleMonitor",
            "/org/gnome/Mutter/IdleMonitor/Core",
            "org.gnome.Mutter.IdleMonitor.ResetIdletime",
        )
        time.sleep(0.4)
        still_locked = self.session_locked()
        if still_locked:
            LOG.warning("session still reports locked after unlock request")
        return not still_locked

    # --- steam process/state -------------------------------------------
    def steam_pids(self) -> set[int]:
        pids: set[int] = set()
        uid = str(os.getuid())
        for name in ("steam", "steamwebhelper"):
            try:
                proc = subprocess.run(
                    ["pgrep", "-u", uid, "-x", name],
                    capture_output=True,
                    text=True,
                    timeout=DBUS_TIMEOUT,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            for token in proc.stdout.split():
                if token.isdigit():
                    pids.add(int(token))
        return pids

    def steam_running(self) -> bool:
        return bool(self.steam_pids())

    def _big_picture_windows(self) -> list[str]:
        if not shutil.which("wmctrl"):
            return []
        try:
            proc = subprocess.run(
                ["wmctrl", "-lx"], capture_output=True, text=True, timeout=DBUS_TIMEOUT
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return [line for line in proc.stdout.splitlines() if "Steam Big Picture Mode" in line]

    def big_picture_open(self) -> bool:
        return bool(self._big_picture_windows())

    def game_running(self) -> bool:
        """True when Steam is supervising a game.

        Steam runs games under its ``reaper`` supervisor and passes
        ``SteamLaunch`` on the command line.  Native games also have their
        executable under ``steamapps/common``.  Matching arbitrary cmdline
        text is too loose (it produced false positives), so only those
        signals count.
        """
        for pid in _iter_pids():
            if _proc_comm(pid) == "reaper":
                return True
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if not raw:
                continue
            cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace")
            argv0 = cmd.split(" ", 1)[0]
            if "/steamapps/common/" in argv0 or "SteamLaunch" in cmd:
                return True
        return False

    def steam_downloading(self) -> bool:
        for base in STEAM_DIRS:
            downloading = os.path.join(base, "steamapps", "downloading")
            try:
                with os.scandir(downloading) as entries:
                    if any(True for _ in entries):
                        return True
            except OSError:
                pass
            content_log = os.path.join(base, "logs", "content_log.txt")
            try:
                if time.time() - os.path.getmtime(content_log) >= 120:
                    continue
                with open(content_log, "r", encoding="utf-8", errors="replace") as fh:
                    tail = fh.readlines()[-8:]
                text = " ".join(tail).lower()
                # Steam rewrites this log on every start when it loads the
                # library folders; only recent download/update lines count.
                if "download" in text or "update" in text:
                    return True
            except OSError:
                pass
        return False

    def shader_work_running(self) -> bool:
        return any("fossilize" in cmd for cmd in _iter_cmdlines())

    def steam_busy(self) -> bool:
        return self.game_running() or self.steam_downloading() or self.shader_work_running()

    # --- launching ------------------------------------------------------
    def focus_big_picture(self, wait: bool = False) -> None:
        if self.dry_run:
            return
        if wait:
            self._focus_loop()
        else:
            threading.Thread(target=self._focus_loop, name="focus-big-picture", daemon=True).start()

    def _focus_loop(self) -> None:
        """Wait for the Big Picture window, then activate it.

        Runs in a thread because Steam can take a long time to show the
        window after a cold start or restart.
        """
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            windows = self._big_picture_windows()
            if windows:
                window_id = windows[0].split(None, 1)[0]
                try:
                    subprocess.run(
                        ["wmctrl", "-i", "-a", window_id],
                        timeout=DBUS_TIMEOUT,
                        capture_output=True,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    LOG.warning("could not activate Big Picture: %s", exc)
                return
            time.sleep(0.5)
        LOG.debug("Big Picture window did not appear within 120s")

    def launch_steam_bp(self, wait_focus: bool = False) -> None:
        LOG.info("launching Steam Big Picture")
        if self.dry_run:
            return
        _spawn([self.cfg["steam_binary"], "-bigpicture"], env=_steam_env(self.cfg))
        self.focus_big_picture(wait=wait_focus)

    def open_steam_bp(self, wait_focus: bool = False) -> None:
        LOG.info("switching Steam to Big Picture")
        if self.dry_run:
            return
        _spawn([self.cfg["steam_binary"], "steam://open/bigpicture"], env=_steam_env(self.cfg))
        self.focus_big_picture(wait=wait_focus)

    # --- restart --------------------------------------------------------
    def notify(self, summary: str, body: str) -> None:
        if self.dry_run or not shutil.which("notify-send"):
            return
        try:
            subprocess.run(
                ["notify-send", "-a", "Couch mode", summary, body],
                timeout=DBUS_TIMEOUT,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _signal_steam(self, sig: str) -> None:
        uid = str(os.getuid())
        for name in ("steam", "steamwebhelper"):
            try:
                subprocess.run(
                    ["pkill", f"-{sig}", "-u", uid, "-x", name],
                    timeout=DBUS_TIMEOUT,
                    capture_output=True,
                )
            except (OSError, subprocess.SubprocessError):
                pass

    def restart_steam(self, reason: str, relaunch: str = "auto", wait_focus: bool = False) -> None:
        """Shut Steam down (gracefully, then by force) and optionally relaunch."""
        was_big_picture = self.big_picture_open()
        locked = self.session_locked()
        LOG.warning("restarting Steam: %s", reason)
        self.notify("Restarting Steam", reason)
        if self.dry_run:
            return

        if not self.steam_running():
            LOG.info("Steam is not running; nothing to restart")
            return

        try:
            subprocess.run(
                [self.cfg["steam_binary"], "-shutdown"],
                timeout=15,
                capture_output=True,
                env=_steam_env(self.cfg),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.debug("steam -shutdown: %s", exc)

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and self.steam_running():
            time.sleep(0.5)

        if self.steam_running():
            LOG.warning("Steam did not exit; sending SIGTERM")
            self._signal_steam("TERM")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.steam_running():
                time.sleep(0.25)

        if self.steam_running():
            LOG.warning("Steam ignored SIGTERM; sending SIGKILL")
            self._signal_steam("KILL")
            time.sleep(1.0)

        # The X window can outlive the process for a moment.  If we launch
        # while it is still listed, the focus helper grabs the dying window and
        # exits before the new one appears.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.big_picture_open():
            time.sleep(0.25)

        if locked:
            LOG.info("session is locked; not relaunching Steam")
            return

        if relaunch == "bp" or (relaunch == "auto" and was_big_picture):
            self.launch_steam_bp(wait_focus=wait_focus)
        elif relaunch == "auto":
            LOG.info("relaunching Steam in desktop mode")
            _spawn([self.cfg["steam_binary"], "-silent"], env=_steam_env(self.cfg))

    def force_restart_steam(self) -> None:
        self.restart_steam("controller hold (View + Menu)", relaunch="bp")

    # --- events ---------------------------------------------------------
    def _state(self) -> tuple[bool, bool, bool, bool]:
        steam = self.steam_running()
        return (
            self.session_locked(),
            self.game_running(),
            steam,
            self.big_picture_open() if steam else False,
        )

    def execute(self, actions: list[str], wait_focus: bool = False) -> None:
        for action in actions:
            if action == "unlock":
                if not self.unlock():
                    LOG.warning("unlock failed; stopping")
                    return
            elif action == "launch_bp":
                self.launch_steam_bp(wait_focus=wait_focus)
            elif action == "open_bp":
                self.open_steam_bp(wait_focus=wait_focus)
            elif action == "focus":
                self.focus_big_picture(wait=wait_focus)

    def on_combo(self, wait_focus: bool = False) -> None:
        now = time.monotonic()
        min_interval = self.cfg["min_action_interval_ms"] / 1000.0
        if now - self.last_action < min_interval:
            LOG.debug("combo ignored (rate limited)")
            return
        self.last_action = now

        locked, game, steam, big_picture = self._state()
        LOG.info("combo entered (locked=%s game=%s steam=%s bp=%s)", locked, game, steam, big_picture)
        if not locked:
            # Never steal focus while the desktop is already usable: a game
            # may be running and this sequence can occur during normal play.
            LOG.info("session already unlocked; combo ignored")
            return
        self.execute(plan_actions(locked, game, steam, big_picture), wait_focus=wait_focus)

    def on_connect(self) -> None:
        if not self.cfg.get("launch_on_connect_when_unlocked", True):
            return
        delay = self.cfg["launch_delay_ms"] / 1000.0
        self.pending_launch = time.monotonic() + delay
        LOG.info("controller connected; couch action in %.1fs unless locked", delay)

    def tick(self, wait_focus: bool = False) -> None:
        if self.pending_launch is None:
            return
        if time.monotonic() < self.pending_launch:
            return
        self.pending_launch = None
        locked, game, steam, big_picture = self._state()
        if locked:
            LOG.info("connect action skipped: session is locked")
            return
        actions = plan_actions(locked, game, steam, big_picture)
        if not actions:
            LOG.info("connect action skipped: a game is running")
            return
        now = time.monotonic()
        if now - self.last_action < self.cfg["min_action_interval_ms"] / 1000.0:
            return
        self.last_action = now
        LOG.info("connect action: %s", ", ".join(actions))
        self.execute(actions, wait_focus=wait_focus)


# --------------------------------------------------------------------------
# Steam hang watchdog
# --------------------------------------------------------------------------

class SteamWatchdog(threading.Thread):
    def __init__(self, actions: Actions, cfg: dict):
        super().__init__(name="steam-watchdog", daemon=True)
        self.actions = actions
        self.cfg = cfg
        self._stop = threading.Event()
        self.hot_since: float | None = None
        self.prev: dict[int, int] = {}
        self.prev_time: float | None = None
        self.sample_seconds = max(5, int(cfg.get("hang_sample_seconds", 15)))

    def stop(self) -> None:
        self._stop.set()

    def sample_cpu(self) -> float:
        now = time.monotonic()
        pids = self.actions.steam_pids()
        ticks = {pid: t for pid in pids if (t := _proc_cpu_ticks(pid)) is not None}

        percent = 0.0
        if self.prev and self.prev_time is not None and now > self.prev_time:
            delta = sum(max(0, t - self.prev[pid]) for pid, t in ticks.items() if pid in self.prev)
            percent = (delta / CLK_TCK) / (now - self.prev_time) * 100.0

        self.prev = ticks
        self.prev_time = now
        return percent

    def run(self) -> None:
        while not self._stop.wait(self.sample_seconds):
            try:
                if not self.actions.steam_running():
                    self.hot_since = None
                    self.prev.clear()
                    self.prev_time = None
                    continue

                cpu = self.sample_cpu()
                busy = self.actions.steam_busy()
                hot = time.monotonic() - self.hot_since if self.hot_since else 0.0

                if should_restart_hung(cpu, hot, self.cfg, busy):
                    self.hot_since = None
                    self.prev.clear()
                    self.prev_time = None
                    LOG.warning("Steam looks hung (%.0f%% CPU for %ds); restarting", cpu, int(hot))
                    self.actions.restart_steam(
                        f"sustained {cpu:.0f}% CPU with no game/download", relaunch="auto"
                    )
                elif cpu >= float(self.cfg["hang_cpu_percent"]) and not busy:
                    if self.hot_since is None:
                        self.hot_since = time.monotonic()
                        LOG.warning("Steam CPU at %.0f%%; watching for a hang", cpu)
                elif self.hot_since is not None:
                    LOG.info("Steam CPU back to %.0f%%; hang watch cleared", cpu)
                    self.hot_since = None
            except Exception:
                LOG.exception("Steam watchdog error")


# --------------------------------------------------------------------------
# device tracking
# --------------------------------------------------------------------------

@dataclass
class Controller:
    dev: evdev.InputDevice
    key: tuple
    matcher: ComboMatcher
    last_hat: dict = field(default_factory=dict)
    held: set[int] = field(default_factory=set)
    hold_start: float | None = None
    hold_fired: bool = False
    grabbed: bool = False
    grab_warned: bool = False


def _is_controller(dev: evdev.InputDevice, vendor: int) -> bool:
    if dev.info.vendor != vendor:
        return False
    caps = dev.capabilities(absinfo=False)
    keys = caps.get(ecodes.EV_KEY, ())
    if ecodes.BTN_SOUTH not in keys:
        return False
    abs_codes = caps.get(ecodes.EV_ABS, ())
    return (
        ecodes.ABS_HAT0Y in abs_codes
        or ecodes.ABS_HAT0X in abs_codes
        or ecodes.BTN_DPAD_UP in keys
    )


def _score(dev: evdev.InputDevice) -> int:
    caps = dev.capabilities(absinfo=False)
    keys = caps.get(ecodes.EV_KEY, ())
    abs_codes = caps.get(ecodes.EV_ABS, ())
    score = 0
    if ecodes.ABS_HAT0Y in abs_codes:
        score += 4
    if ecodes.ABS_HAT0X in abs_codes:
        score += 2
    if ecodes.BTN_DPAD_UP in keys:
        score += 2
    score += len(keys) // 4
    return score


def scan_controllers(vendor: int, allowed_ids: list[str]) -> dict[tuple, evdev.InputDevice]:
    """Return one best event node per physical controller."""
    by_key: dict[tuple, list[evdev.InputDevice]] = {}
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except (OSError, PermissionError, FileNotFoundError):
            continue
        try:
            if not _is_controller(dev, vendor):
                dev.close()
                continue
            # One controller usually exposes several event nodes.  Dedupe by
            # the controller's own id when present (xone/xpad report the
            # serial/MAC); otherwise fall back to the physical port + name.
            uniq = dev.uniq or ""
            if uniq:
                key = (dev.info.vendor, dev.info.product, uniq)
            else:
                key = (dev.info.vendor, dev.info.product, dev.name, dev.phys or path)
            by_key.setdefault(key, []).append(dev)
        except OSError:
            dev.close()

    result: dict[tuple, evdev.InputDevice] = {}
    for key, devs in by_key.items():
        devs.sort(key=_score, reverse=True)
        keep = devs[0]
        for extra in devs[1:]:
            extra.close()
        if allowed_ids and (keep.uniq or "") not in allowed_ids:
            LOG.debug("controller %s not in allowlist (id=%r)", keep.path, keep.uniq)
            keep.close()
            continue
        result[key] = keep
    return result


def _token_for(ctrl: Controller, ev) -> tuple | None:
    if ev.type == ecodes.EV_KEY and ev.value == 1:
        return ("key", ev.code)
    if ev.type == ecodes.EV_ABS and ev.code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y):
        previous = ctrl.last_hat.get(ev.code)
        ctrl.last_hat[ev.code] = ev.value
        if ev.value in (-1, 1) and ev.value != previous:
            return ("hat", ev.code, ev.value)
    return None


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

class Daemon:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.vendor = int(str(cfg["vendor_id"]), 16)
        self.allowed_ids = [str(x) for x in cfg.get("allowed_controller_ids", [])]
        self.actions = Actions(cfg, dry_run=dry_run)
        self.controllers: dict[tuple, Controller] = {}
        self.running = True
        self._initial_scan = True
        self.watchdog: SteamWatchdog | None = None
        self.grab_when_locked = bool(cfg.get("grab_when_locked", True))
        self._locked_now: bool | None = None
        self._last_lock_poll = 0.0

    def stop(self, *_args) -> None:
        self.running = False
        if self.watchdog:
            self.watchdog.stop()

    def rescan(self) -> None:
        found = scan_controllers(self.vendor, self.allowed_ids)

        for key in list(self.controllers):
            if key not in found:
                ctrl = self.controllers.pop(key)
                LOG.info("controller disconnected: %s", ctrl.dev.name)
                try:
                    ctrl.dev.close()
                except OSError:
                    pass

        combo = list(self.cfg["combo"])
        window = self.cfg["window_ms"] / 1000.0
        gap = self.cfg["press_gap_ms"] / 1000.0
        for key, dev in found.items():
            if key in self.controllers:
                dev.close()  # already tracked
                continue
            ctrl = Controller(dev, key, ComboMatcher(combo, window, gap))
            self.controllers[key] = ctrl
            LOG.info(
                "controller connected: %s (%s) id=%r",
                dev.name, dev.path, dev.uniq or "",
            )
            if self._locked_now is True:
                self._apply_grab(ctrl, True)
            if not self._initial_scan:
                self.actions.on_connect()

        self._initial_scan = False

    def handle_event(self, ctrl: Controller, ev) -> None:
        if ev.type == ecodes.EV_SYN and ev.code == ecodes.SYN_DROPPED:
            ctrl.matcher.reset()
            ctrl.last_hat.clear()
            ctrl.held.clear()
            ctrl.hold_start = None
            ctrl.hold_fired = False
            return

        if ev.type == ecodes.EV_KEY:
            if ev.value == 1:
                ctrl.held.add(ev.code)
                if (
                    ecodes.BTN_SELECT in ctrl.held
                    and ecodes.BTN_START in ctrl.held
                    and ctrl.hold_start is None
                ):
                    ctrl.hold_start = time.monotonic()
                    ctrl.hold_fired = False
            elif ev.value == 0:
                ctrl.held.discard(ev.code)
                if ev.code in (ecodes.BTN_SELECT, ecodes.BTN_START):
                    ctrl.hold_start = None
                    ctrl.hold_fired = False

        token = _token_for(ctrl, ev)
        if token is None:
            return
        name = EVENT_TO_BUTTON.get(token)
        if name is None:
            return
        if ctrl.matcher.feed(name):
            LOG.debug("sequence matched on %s", ctrl.dev.name)
            self.actions.on_combo()
            self.update_grab_state(force=True)

    def check_holds(self) -> None:
        hold_s = float(self.cfg.get("force_restart_hold_ms", 5000)) / 1000.0
        now = time.monotonic()
        for ctrl in self.controllers.values():
            if (
                ctrl.hold_start is not None
                and not ctrl.hold_fired
                and now - ctrl.hold_start >= hold_s
            ):
                ctrl.hold_fired = True
                LOG.info("force-restart combo held on %s", ctrl.dev.name)
                self.actions.force_restart_steam()
                ctrl.held.clear()
                ctrl.hold_start = None

    def _apply_grab(self, ctrl: Controller, want: bool) -> None:
        if want and not ctrl.grabbed:
            try:
                ctrl.dev.grab()
                ctrl.grabbed = True
                ctrl.grab_warned = False
                LOG.info("controller grabbed while locked: %s", ctrl.dev.name)
            except OSError as exc:
                if not ctrl.grab_warned:
                    LOG.warning(
                        "could not grab %s while locked (%s); Steam may still see it",
                        ctrl.dev.name, exc,
                    )
                    ctrl.grab_warned = True
        elif not want and ctrl.grabbed:
            try:
                ctrl.dev.ungrab()
            except OSError as exc:
                LOG.debug("could not release %s: %s", ctrl.dev.name, exc)
            ctrl.grabbed = False
            LOG.info("controller released: %s", ctrl.dev.name)

    def _apply_grab_all(self, want: bool) -> None:
        for ctrl in list(self.controllers.values()):
            self._apply_grab(ctrl, want)

    def update_grab_state(self, force: bool = False) -> None:
        """Grab controllers while the session is locked.

        The GNOME lock screen locks the session UI, not gamepads: Steam Input
        keeps reading /dev/input in the background and can still act on Guide
        chords (including its power menu).  EVIOCGRAB while locked makes the
        daemon the only consumer; it still receives the unlock combo.  The
        grab is released as soon as the session is unlocked.
        """
        if not self.grab_when_locked:
            self._apply_grab_all(False)
            return

        now = time.monotonic()
        if not force and now - self._last_lock_poll < 2.0:
            return
        self._last_lock_poll = now

        state = self.actions.lock_state()
        if state is None:
            LOG.debug("lock state unavailable; keeping current grab state")
            return
        self._locked_now = state
        self._apply_grab_all(state)

    def run(self) -> int:
        LOG.info("watching for Xbox controllers (vendor %04x)", self.vendor)
        LOG.info("combo: %s", " ".join(self.cfg["combo"]))
        if self.cfg.get("hang_watchdog", True):
            self.watchdog = SteamWatchdog(self.actions, self.cfg)
            self.watchdog.start()
            LOG.info(
                "Steam hang watchdog: >%s%% CPU for %ss, no game/download",
                self.cfg["hang_cpu_percent"], self.cfg["hang_seconds"],
            )

        while self.running:
            self.actions.tick()
            self.check_holds()
            self.update_grab_state()
            try:
                self.rescan()
            except OSError as exc:
                LOG.warning("scan error: %s", exc)

            fds = {ctrl.dev.fd: ctrl for ctrl in self.controllers.values()}
            if not fds:
                time.sleep(1.0)
                continue
            hold_active = any(ctrl.hold_start is not None for ctrl in self.controllers.values())
            if hold_active:
                timeout = 0.25
            elif self.actions.pending_launch is not None:
                timeout = 0.5
            elif self.grab_when_locked:
                timeout = 1.0
            else:
                timeout = RESCAN_INTERVAL
            try:
                ready, _, _ = select.select(list(fds), [], [], timeout)
            except (OSError, ValueError):
                time.sleep(0.5)
                continue

            for fd in ready:
                ctrl = fds.get(fd)
                if ctrl is None:
                    continue
                try:
                    for ev in ctrl.dev.read():
                        self.handle_event(ctrl, ev)
                except (OSError, BlockingIOError) as exc:
                    LOG.info("lost controller %s (%s)", ctrl.dev.name, exc)
                    self.controllers.pop(ctrl.key, None)
                    try:
                        ctrl.dev.close()
                    except OSError:
                        pass
        for ctrl in self.controllers.values():
            try:
                ctrl.dev.close()
            except OSError:
                pass
        LOG.info("stopped")
        return 0


def print_status(cfg: dict, vendor: int, allowed_ids: list[str]) -> int:
    actions = Actions(cfg)
    print(f"config:       {CONFIG_PATH}")
    print(f"combo:        {' '.join(cfg['combo'])}")
    print(f"locked:       {actions.session_locked()}")
    steam = actions.steam_running()
    print(f"steam:        {'running' if steam else 'not running'}")
    print(f"big picture:  {actions.big_picture_open() if steam else False}")
    print(f"game:         {actions.game_running()}")
    print(f"downloading:  {actions.steam_downloading()}")
    print(f"allowlist:    {allowed_ids or '(any Xbox controller)'}")
    found = scan_controllers(vendor, allowed_ids)
    if not found:
        print("controllers:  none detected")
    for dev in found.values():
        print(
            "controller:   "
            f"{dev.name}  path={dev.path}  id={dev.uniq or ''!r} "
            f"vendor={dev.info.vendor:04x} product={dev.info.product:04x}"
        )
        dev.close()
    return 0


def doctor(cfg: dict, vendor: int, allowed_ids: list[str]) -> int:
    """Read-only environment check. Nothing is locked, unlocked or launched."""
    problems = 0
    warnings = 0

    def report(label: str, condition: bool, detail: str = "", fatal: bool = True) -> None:
        nonlocal problems, warnings
        if condition:
            print(f"ok    {label}" + (f": {detail}" if detail else ""))
        elif fatal:
            problems += 1
            print(f"FAIL  {label}" + (f": {detail}" if detail else ""))
        else:
            warnings += 1
            print(f"WARN  {label}" + (f": {detail}" if detail else ""))

    report("Python >= 3.10", sys.version_info >= (3, 10), sys.version.split()[0])
    report("python3-evdev importable", True, getattr(evdev, "__file__", "?"))
    for tool in ("gdbus", "loginctl", "wmctrl", "pgrep", "pkill"):
        report(f"{tool} present", shutil.which(tool) is not None)
    report("notify-send (optional)", shutil.which("notify-send") is not None, fatal=False)

    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    session = os.environ.get("XDG_SESSION_TYPE", "")
    report("GNOME desktop", "GNOME" in desktop.upper(), f"{desktop or '?'} / {session or '?'}", fatal=False)

    steam = str(cfg["steam_binary"])
    report("Steam binary", os.path.isfile(steam) and os.access(steam, os.X_OK), steam)

    extest_library = os.path.expanduser(
        str(cfg.get("extest_library") or EXTEST_LIBRARY)
    )
    if not cfg.get("preload_extest", True):
        report("extest Steam input", True, "disabled in config", fatal=False)
    elif _extest_available(extest_library):
        report("extest Steam input", True, extest_library, fatal=False)
    elif "WAYLAND" in session.upper():
        report(
            "extest Steam input",
            False,
            f"not found at {extest_library}; Steam may prompt for Remote Desktop",
            fatal=False,
        )
    else:
        report("extest Steam input", True, "optional on X11; not installed", fatal=False)

    try:
        load_config()
        report("config parses", True, CONFIG_PATH)
    except ValueError as exc:
        report("config parses", False, str(exc))

    actions = Actions(cfg)
    report("screen saver D-Bus", True, f"locked={actions.session_locked()}")

    found = scan_controllers(vendor, allowed_ids)
    report("controller detected", bool(found), f"{len(found)} found", fatal=False)
    if not allowed_ids:
        report(
            "controller allowlist",
            False,
            "empty: any Xbox controller can trigger the combo",
            fatal=False,
        )
    legacy_helper = os.path.expanduser("~/.local/bin/couch-focus-bp")
    if os.path.exists(legacy_helper):
        report(
            "legacy focus helper",
            False,
            f"{legacy_helper} exists but is no longer used",
            fatal=False,
        )
    else:
        report("legacy focus helper", True, "not present")

    print()
    print(f"{problems} problem(s), {warnings} warning(s)")
    return 1 if problems else 0


def self_test() -> int:
    LOG.setLevel(logging.WARNING)
    steps = ["A", "A", "X", "X", "UP", "DOWN"]
    seq = list(steps)
    failures = 0

    def check(name: str, got, want) -> None:
        nonlocal failures
        if got != want:
            failures += 1
            print(f"FAIL {name}: got {got!r}, want {want!r}")
        else:
            print(f"ok   {name}")

    def run(steps, seq, gap=1.5, window=6.0, spacing=1.0):
        m = ComboMatcher(steps, window, gap)
        return [m.feed(tok, now=float(i) * spacing) for i, tok in enumerate(seq)]

    results = run(steps, seq)
    check("exact sequence matches once", results[:-1] == [False] * 5 and results[-1] is True, True)
    results = run(steps, ["Y"] + seq)
    check("leading wrong button still allows match", results[-1] is True, True)
    results = run(steps, seq[:3] + ["Y"] + seq[3:])
    check("wrong button mid-sequence does not match", not any(results), True)
    results = run(steps, seq, spacing=2.0)
    check("gaps over the limit do not match", not any(results), True)
    results = run(steps, seq, gap=10.0, window=3.0)
    check("sequence longer than window does not match", not any(results), True)
    results = run(steps, ["A"] * 3 + seq)
    check("extra A before combo allows match", results[-1] is True, True)
    results = run(steps, ["B"] + seq)
    check("unrelated leading button still allows match", results[-1] is True, True)

    # Steam environment / extest preload.
    saved_preload = os.environ.pop("LD_PRELOAD", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "libextest.so")
            with open(fake, "w", encoding="utf-8") as fh:
                fh.write("")
            cfg = dict(DEFAULT_CONFIG)
            cfg["extest_library"] = fake
            check("extest is preloaded for Steam", _steam_env(cfg).get("LD_PRELOAD"), fake)
            os.environ["LD_PRELOAD"] = "/existing.so"
            check(
                "existing LD_PRELOAD is preserved",
                _steam_env(cfg).get("LD_PRELOAD"),
                f"{fake}:/existing.so",
            )
            os.environ["LD_PRELOAD"] = fake
            check("extest is not duplicated", _steam_env(cfg).get("LD_PRELOAD"), fake)
            cfg["preload_extest"] = False
            check("extest can be disabled", _steam_env(cfg).get("LD_PRELOAD"), fake)
            cfg["preload_extest"] = True
            cfg["extest_library"] = os.path.join(tmp, "missing.so")
            check(
                "missing extest leaves LD_PRELOAD alone",
                _steam_env(cfg).get("LD_PRELOAD"),
                fake,
            )
        check(
            "${LIB} expands for both ABIs",
            _expand_lib_path("/usr/${LIB}/extest/libextest.so"),
            ["/usr/lib64/extest/libextest.so", "/usr/lib/extest/libextest.so"],
        )
    finally:
        if saved_preload is None:
            os.environ.pop("LD_PRELOAD", None)
        else:
            os.environ["LD_PRELOAD"] = saved_preload

    # State planner.
    cases = {
        (True, False, False, False): ["unlock", "launch_bp"],
        (True, False, True, False): ["unlock", "open_bp"],
        (True, False, True, True): ["unlock", "focus"],
        (True, True, True, False): ["unlock"],
        (True, True, True, True): ["unlock"],
        (False, False, False, False): ["launch_bp"],
        (False, False, True, False): ["open_bp"],
        (False, False, True, True): ["focus"],
        (False, True, True, True): [],
    }
    for state, want in cases.items():
        check(f"plan {state}", plan_actions(*state), want)

    # Hang decision.
    hcfg = {"hang_cpu_percent": 85, "hang_seconds": 240}
    check("hang: sustained high cpu", should_restart_hung(95, 300, hcfg, False), True)
    check("hang: not yet long enough", should_restart_hung(95, 100, hcfg, False), False)
    check("hang: low cpu", should_restart_hung(40, 300, hcfg, False), False)
    check("hang: busy with game/download", should_restart_hung(95, 300, hcfg, True), False)

    # Full event -> logical button -> matcher -> action path, using the exact
    # evdev codes the controller emits (captured from the hardware).
    from types import SimpleNamespace

    def pipeline(names, hat_axis=ecodes.ABS_HAT0Y):
        cfg = dict(DEFAULT_CONFIG)
        cfg["combo"] = steps
        daemon = Daemon(cfg, dry_run=True)
        fired = []
        daemon.actions.on_combo = lambda: fired.append("combo")
        ctrl = Controller(
            dev=SimpleNamespace(name="test"),
            key=("test",),
            matcher=ComboMatcher(steps, 10.0, 2.5),
        )
        codes = {"A": ecodes.BTN_SOUTH, "B": ecodes.BTN_EAST,
                 "X": ecodes.BTN_NORTH, "Y": ecodes.BTN_WEST}
        for name in names:
            if name in codes:
                for value in (1, 0):
                    daemon.handle_event(ctrl, SimpleNamespace(type=ecodes.EV_KEY, code=codes[name], value=value))
            else:
                value = -1 if name == "UP" else 1
                for v in (value, 0):
                    daemon.handle_event(ctrl, SimpleNamespace(type=ecodes.EV_ABS, code=hat_axis, value=v))
        return fired

    check("real evdev codes fire the action", pipeline(["A", "A", "X", "X", "UP", "DOWN"]) == ["combo"], True)
    check("B instead of X does not fire", pipeline(["A", "A", "B", "B", "UP", "DOWN"]) == [], True)

    hat_ctrl = SimpleNamespace(last_hat={})
    hat_ev = SimpleNamespace(type=ecodes.EV_ABS, code=ecodes.ABS_HAT0Y, value=-1)
    first = _token_for(hat_ctrl, hat_ev)
    second = _token_for(hat_ctrl, hat_ev)
    check(
        "held hat direction counts once",
        first == ("hat", ecodes.ABS_HAT0Y, -1) and second is None,
        True,
    )

    # Hold-to-restart detection.
    cfg = dict(DEFAULT_CONFIG)
    cfg["force_restart_hold_ms"] = 5000
    daemon = Daemon(cfg, dry_run=True)
    fired = []
    daemon.actions.force_restart_steam = lambda: fired.append("restart")
    ctrl = Controller(
        dev=SimpleNamespace(name="test"),
        key=("test",),
        matcher=ComboMatcher(steps, 10.0, 2.5),
    )
    daemon.controllers[("test",)] = ctrl
    for code in (ecodes.BTN_SELECT, ecodes.BTN_START):
        daemon.handle_event(ctrl, SimpleNamespace(type=ecodes.EV_KEY, code=code, value=1))
    check("hold starts when both buttons are down", ctrl.hold_start is not None, True)
    daemon.check_holds()
    check("hold does not fire early", fired, [])
    ctrl.hold_start -= 6.0
    daemon.check_holds()
    check("hold fires after the duration", fired, ["restart"])
    ctrl.hold_start = None
    for code in (ecodes.BTN_SELECT, ecodes.BTN_START):
        daemon.handle_event(ctrl, SimpleNamespace(type=ecodes.EV_KEY, code=code, value=0))
    check("release resets the hold", ctrl.hold_fired, False)

    # Grab-while-locked.
    class FakeDev:
        name = "fake"

        def __init__(self):
            self.grabbed = False

        def grab(self):
            self.grabbed = True

        def ungrab(self):
            self.grabbed = False

    def grab_daemon(enabled: bool, locked: bool):
        cfg = dict(DEFAULT_CONFIG)
        cfg["grab_when_locked"] = enabled
        daemon = Daemon(cfg, dry_run=True)
        daemon.actions.lock_state = lambda: locked
        dev = FakeDev()
        ctrl = Controller(
            dev=dev, key=("fake",), matcher=ComboMatcher(steps, 10.0, 2.5)
        )
        daemon.controllers[("fake",)] = ctrl
        return daemon, ctrl

    daemon, ctrl = grab_daemon(True, True)
    daemon.update_grab_state(force=True)
    check("locked session grabs the controller", ctrl.grabbed, True)
    daemon.actions.lock_state = lambda: False
    daemon.update_grab_state(force=True)
    check("unlock releases the controller", ctrl.grabbed, False)
    daemon, ctrl = grab_daemon(False, True)
    daemon.update_grab_state(force=True)
    check("grab can be disabled by config", ctrl.grabbed, False)

    print("self-test:", "FAILED" if failures else "passed")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="log actions instead of performing them")
    parser.add_argument("--status", action="store_true", help="print detected controllers and session state")
    parser.add_argument("--self-test", action="store_true", help="run tests and exit")
    parser.add_argument("--doctor", action="store_true", help="check dependencies and environment")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--test-action", choices=("bp", "focus", "unlock", "lock", "restart", "combo", "connect"), help=argparse.SUPPRESS
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if args.self_test:
        return self_test()

    try:
        cfg = load_config()
    except ValueError as exc:
        LOG.error("invalid config: %s", exc)
        return 2

    vendor = int(str(cfg["vendor_id"]), 16)
    allowed_ids = [str(x) for x in cfg.get("allowed_controller_ids", [])]

    if args.doctor:
        return doctor(cfg, vendor, allowed_ids)

    if args.status:
        return print_status(cfg, vendor, allowed_ids)

    if args.test_action:
        actions = Actions(cfg, dry_run=args.dry_run)
        if args.test_action == "lock":
            print("locked before:", actions.session_locked())
            subprocess.run(["loginctl", "lock-session"], check=False)
            time.sleep(1.0)
            print("locked after: ", actions.session_locked())
        elif args.test_action == "unlock":
            print("locked before:", actions.session_locked())
            print("unlocked:     ", actions.unlock())
            print("locked after: ", actions.session_locked())
        elif args.test_action == "restart":
            actions.restart_steam("manual test", relaunch="bp", wait_focus=True)
            print("restart requested")
        elif args.test_action == "focus":
            actions.focus_big_picture(wait=True)
            print("focus requested")
        elif args.test_action == "combo":
            actions.on_combo(wait_focus=True)
            print("combo processed")
        elif args.test_action == "connect":
            actions.on_connect()
            time.sleep(cfg["launch_delay_ms"] / 1000.0 + 0.3)
            actions.tick(wait_focus=True)
            print("connect processed")
        else:
            actions.launch_steam_bp(wait_focus=True)
            print("launch requested")
        return 0

    daemon = Daemon(cfg, dry_run=args.dry_run)
    signal.signal(signal.SIGTERM, daemon.stop)
    signal.signal(signal.SIGINT, daemon.stop)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
