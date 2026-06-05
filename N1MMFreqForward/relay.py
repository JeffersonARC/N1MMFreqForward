"""N1MM Frequency Forwarder
==========================

Runs on a remote operator's Windows machine. Captures N1MM RadioInfo UDP
broadcasts on localhost and forwards each TXFreq value to the shack
steprtool server over HTTPS, where the server's /api/n1mm/txfreq endpoint
applies it via the same auto-retune logic as a locally-connected N1MM.

UX is a tray icon (green=forwarding, yellow=receiving but not forwarding,
red=server unreachable, gray=no UDP yet). Right-click for Edit config
(opens config.json in Notepad), Reload config (re-reads the file after
you've edited it), Open log folder, Quit. No autostart — the operator is
expected to launch this manually when starting a remote session.

Config and log live in %APPDATA%\\N1MMFreqForward\\ on Windows, or
~/.config/N1MMFreqForward/ on other platforms (mainly for development).

Standalone build: see build.bat in this directory. PyInstaller produces
a single .exe that the operator runs directly (no installer, no Python).

DESIGN NOTE: this version uses no tkinter. Earlier versions tried to
combine pystray (Win32 tray icon) with tkinter for a settings dialog;
the two Win32 message loops fought, and tkinter-side menu callbacks
hung indefinitely. Editing config.json in Notepad is the reliable
fallback, and it matches how operators edit most other ham-radio config.
"""

from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import os
import socket
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw, ImageFont


# ---------------------------- paths -------------------------------------

def _appdata_dir() -> Path:
    """Per-user writable config/log directory. Cross-platform."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(base) / "N1MMFreqForward"
    p.mkdir(parents=True, exist_ok=True)
    return p


APP_DIR     = _appdata_dir()
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE    = APP_DIR / "relay.log"
CRASH_FILE  = APP_DIR / "startup-error.txt"

APP_TITLE   = "N1MM Frequency Forwarder"

DEFAULT_CONFIG: dict = {
    "server_url":           "https://100.98.246.94:6300",
    "server_secret":        "",
    "operator_name":        "",
    "operator_callsign":    "",
    "ports":                [12060, 13063, 13065],
    "min_send_interval_ms": 500,
    # Watchdog: exit if N1MM never sends UDP within the first N minutes,
    # or if N1MM goes silent for N minutes after the first packet arrives.
    # Set either to 0 to disable that phase of the watchdog.
    "N1MM_initial_wait_mins":    15,
    "N1MM_wait_mins":            15,
}

# Status: (icon_color, human_text)
GRAY   = ("gray",   "no UDP packets received")
YELLOW = ("orange", "receiving N1MM but not forwarding")
GREEN  = ("green",  "forwarding to server")
RED    = ("red",    "server unreachable")


# ---------------------------- Windows message boxes ---------------------

# Bit flags for MessageBoxW uType. We hardcode these so we don't depend
# on win32 modules being available at build time.
MB_OK              = 0x00000000
MB_ICONERROR       = 0x00000010
MB_ICONINFORMATION = 0x00000040
MB_SETFOREGROUND   = 0x00010000
MB_TOPMOST         = 0x00040000

# Pre-bind with explicit argtypes so ctypes doesn't guess wrong on some Windows builds.
if sys.platform.startswith("win"):
    try:
        _MBW = ctypes.windll.user32.MessageBoxW
        _MBW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                         ctypes.c_wchar_p, ctypes.c_uint]
        _MBW.restype  = ctypes.c_int
    except Exception:
        _MBW = None
else:
    _MBW = None


def msgbox(text: str, title: str = APP_TITLE, flags: int = MB_OK) -> None:
    """Show a Windows MessageBox on a dedicated thread (fire-and-forget).

    Why a thread: when MessageBoxW is called directly from a pystray menu
    callback, the dialog's modal loop nests inside pystray's tray message
    loop on the same thread — the dialog appears but OK / X clicks stop
    responding. A fresh thread isolates the two loops completely. MB_TOPMOST | MB_SETFOREGROUND ensure the dialog appears on top with focus rather than hiding behind Notepad, the tray menu, etc.
    """
    if _MBW is None:
        return
    eff_flags = flags | MB_SETFOREGROUND | MB_TOPMOST

    def _show():
        try:
            _MBW(None, text, title, eff_flags)
        except Exception as e:
            try:
                logging.warning("MessageBox failed: %s", e)
            except Exception:
                pass

    threading.Thread(target=_show, daemon=True, name="msgbox").start()


# ---------------------------- config ------------------------------------

def load_config() -> dict:
    """Load config, falling back to defaults for any missing or malformed
    keys. Never raises — designed to give the app *something* to start with."""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update(user)
        except Exception as e:
            logging.exception("config load failed, using defaults: %s", e)
    try:
        cfg["ports"] = [int(p) for p in cfg.get("ports", [])] or list(DEFAULT_CONFIG["ports"])
    except Exception:
        cfg["ports"] = list(DEFAULT_CONFIG["ports"])
    cfg["min_send_interval_ms"] = int(cfg.get("min_send_interval_ms",
                                              DEFAULT_CONFIG["min_send_interval_ms"]))
    cfg["server_url"] = (cfg.get("server_url") or "").strip().rstrip("/")
    return cfg


def validate_config_file() -> Optional[str]:
    """Re-parse the file strictly. Returns an error string for the user, or
    None if the file is valid JSON. Used by Reload so users get told when
    their hand-edit broke the syntax."""
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return f"Config file not found:\n{CONFIG_FILE}"
    except json.JSONDecodeError as e:
        return (f"Config file has a JSON syntax error:\n\n"
                f"  Line {e.lineno}, column {e.colno}: {e.msg}\n\n"
                f"File: {CONFIG_FILE}\n\nFix it in Notepad and try Reload again.")
    except Exception as e:
        return f"Config file could not be read:\n{e}\n\nFile: {CONFIG_FILE}"
    if not isinstance(data, dict):
        return "Config file must contain a JSON object (curly braces)."
    return None


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_FILE)


def config_is_complete(cfg: dict) -> bool:
    return bool(cfg.get("operator_callsign", "").strip()
                and cfg.get("operator_name", "").strip()
                and cfg.get("server_url", "").strip()
                and cfg.get("server_secret", "").strip())


# ---------------------------- HTTPS POST --------------------------------

def post_txfreq(cfg: dict, tx_freq_tens_of_hz: int) -> tuple[bool, dict | str]:
    body = json.dumps({
        "tx_freq_tens_of_hz": tx_freq_tens_of_hz,
        "operator": {
            "name":     cfg["operator_name"].strip(),
            "callsign": cfg["operator_callsign"].strip().upper(),
        },
    }).encode("utf-8")
    url = f"{cfg['server_url'].rstrip('/')}/api/n1mm/txfreq"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if cfg.get("server_secret"):
        req.add_header("X-Steprtool-Auth", cfg["server_secret"])
    # Disable TLS verification — Tailscale already encrypts and the shack
    # server uses a self-signed cert.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return True, json.loads(raw)
            except Exception:
                return True, raw
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return False, f"HTTP {e.code}: {err_body}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ---------------------------- UDP listener ------------------------------

class UdpListener:
    def __init__(self, host: str, ports: list[int],
                 on_txfreq: Callable[[int], None]):
        self.host = host
        self.ports = ports
        self.on_txfreq = on_txfreq
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sockets: list[socket.socket] = []
        # Track which ports have already logged their first packet, so we
        # confirm UDP is reaching us without spamming the log per-packet
        # (N1MM emits RadioInfo several times per second).
        self._first_logged: set[int] = set()

    def start(self) -> None:
        for port in self.ports:
            t = threading.Thread(
                target=self._run_port, args=(port,),
                name=f"udp-{port}", daemon=True,
            )
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for s in self._sockets:
            try: s.close()
            except Exception: pass

    def _run_port(self, port: int) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try: sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError: pass
            if hasattr(socket, "SO_REUSEPORT"):
                try: sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError: pass
            sock.bind((self.host, port))
            sock.settimeout(1.0)
            self._sockets.append(sock)
            logging.info("UDP bound on %s:%d", self.host, port)
        except OSError as e:
            logging.error("UDP bind %s:%d failed: %s", self.host, port, e)
            return

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle(port, addr, data)

    def _handle(self, port: int, addr, data: bytes) -> None:
        # First packet ever on this port: log it loudly so we know UDP is
        # actually reaching us. After that, parsing-level details stay quiet.
        if port not in self._first_logged:
            self._first_logged.add(port)
            try:
                src = f"{addr[0]}:{addr[1]}"
            except Exception:
                src = "?"
            logging.info("first UDP packet on port %d (from %s, %d bytes)",
                         port, src, len(data))
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return
        if not text.lstrip().startswith("<"):
            return
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return
        if root.tag != "RadioInfo":
            return
        # Explicit `is None` checks — Element objects with no children are
        # FALSY in Python (deprecated, but still the case in 3.13), so the
        # tempting `or` chain silently drops elements that contain only text.
        tx = root.find("TXFreq")
        if tx is None:
            tx = root.find("txfreq")
        if tx is None or tx.text is None:
            return
        try:
            v = int(tx.text.strip())
        except ValueError:
            return
        if v > 0:
            try:
                self.on_txfreq(v)
            except Exception as e:
                logging.warning("on_txfreq crashed: %s", e)


# ---------------------------- Relay state machine -----------------------

class Relay:
    def __init__(self, config: dict):
        self.config = dict(config)
        self._lock = threading.Lock()
        self._last_sent_freq: Optional[int] = None
        self._last_sent_at:   float = 0.0
        self._last_udp_at:    Optional[float] = None
        self._last_post_ok_at:  Optional[float] = None
        self._last_post_err:    Optional[str]  = None
        self._stopped = False
        self._udp = UdpListener("0.0.0.0", config["ports"], self._on_txfreq)

    @property
    def last_udp_at(self) -> Optional[float]:
        """Monotonic timestamp of the most recent UDP packet, or None."""
        with self._lock:
            return self._last_udp_at

    def start(self) -> None:
        self._udp.start()

    def stop(self) -> None:
        self._stopped = True
        self._udp.stop()

    def reload_config(self, new_config: dict) -> None:
        with self._lock:
            old_ports = self.config["ports"]
            self.config = dict(new_config)
        if old_ports != new_config["ports"]:
            self._udp.stop()
            self._udp = UdpListener("0.0.0.0", new_config["ports"], self._on_txfreq)
            self._udp.start()

    def status(self) -> tuple[str, str]:
        now = time.monotonic()
        with self._lock:
            if self._last_udp_at is None:
                return GRAY
            udp_age = now - self._last_udp_at
            ok_age  = (now - self._last_post_ok_at) if self._last_post_ok_at else 1e9
            err     = self._last_post_err
        if err and ok_age > 60:
            return RED[0], f"server unreachable ({err})"
        if udp_age > 120:
            return GRAY[0], "no recent UDP from N1MM"
        if ok_age < 60:
            return GREEN
        return YELLOW

    def _on_txfreq(self, tx_freq: int) -> None:
        if self._stopped:
            return
        now = time.monotonic()
        with self._lock:
            self._last_udp_at = now
            min_interval = self.config["min_send_interval_ms"] / 1000.0
            same = (tx_freq == self._last_sent_freq)
            recent = (now - self._last_sent_at) < min_interval
            if same and recent:
                return
            self._last_sent_freq = tx_freq
            self._last_sent_at   = now
            send = config_is_complete(self.config)
            cfg_copy = dict(self.config)
        if not send:
            logging.debug("config incomplete; not forwarding TXFreq=%d", tx_freq)
            return
        threading.Thread(
            target=self._do_post, args=(cfg_copy, tx_freq), daemon=True,
        ).start()

    def _do_post(self, cfg: dict, tx_freq: int) -> None:
        ok, info = post_txfreq(cfg, tx_freq)
        with self._lock:
            if ok:
                self._last_post_ok_at = time.monotonic()
                self._last_post_err = None
            else:
                self._last_post_err = str(info)

        if ok and isinstance(info, dict):
            if info.get("applied"):
                logging.info("forwarded %d kHz -> antenna retuned",
                             tx_freq // 100)
            else:
                logging.info("forwarded %d kHz -> not retuned (%s)",
                             tx_freq // 100, info.get("reason", "?"))
        elif ok:
            logging.info("forwarded TXFreq=%d (unparsed response)", tx_freq)
        else:
            logging.warning("forward failed TXFreq=%d: %s", tx_freq, info)


# ---------------------------- tray icon --------------------------------

def make_icon_image(color: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill=color, outline="#222", width=2)
    try:
        f = ImageFont.truetype("arial.ttf", 32)
    except Exception:
        f = ImageFont.load_default()
    try:
        bbox = d.textbbox((0, 0), "R", font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((64 - tw) / 2 - bbox[0], (64 - th) / 2 - bbox[1] - 2),
               "R", fill="white", font=f)
    except Exception:
        d.text((22, 14), "R", fill="white", font=f)
    return img


# ---------------------------- logging ----------------------------------

def setup_logging() -> None:
    """Configure logging. Careful with sys.stderr — in PyInstaller
    --windowed mode it's None, and StreamHandler() would default to it
    and crash on first emit."""
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    h = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    # Only add a stderr handler if stderr is actually usable.
    if sys.stderr is not None:
        try:
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            root.addHandler(sh)
        except Exception:
            pass


# ---------------------------- main -------------------------------------

def main() -> int:
    setup_logging()
    logging.info("%s starting; config=%s", APP_TITLE, CONFIG_FILE)

    cfg = load_config()
    if not CONFIG_FILE.exists():
        save_config(cfg)
        logging.info("wrote default config")

    relay = Relay(cfg)
    relay.start()

    # ---- menu actions (all run on pystray's tray thread; that's fine
    #      because none of them touch GUI state — they just shell out to
    #      Notepad / Explorer / a MessageBox, or twiddle relay config) ----

    def open_in_default_app(path: Path) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))
            elif sys.platform == "darwin":
                os.system(f"open '{path}'")
            else:
                os.system(f"xdg-open '{path}'")
        except Exception as e:
            logging.warning("open %s failed: %s", path, e)

    def on_edit_config(*_a):
        # On Windows, .json is usually associated with Notepad; if the user
        # has set a different default editor, that's fine too.
        open_in_default_app(CONFIG_FILE)
        msgbox(
            "The config file is now open in your text editor.\n\n"
            "Fill in:\n"
            "    server_url\n"
            "    server_secret\n"
            "    operator_name\n"
            "    operator_callsign\n\n"
            "Then SAVE the file, return here, and choose "
            "'Reload config' from the tray menu.",
            APP_TITLE, MB_OK | MB_ICONINFORMATION,
        )

    def on_reload(*_a):
        err = validate_config_file()
        if err:
            msgbox(err, APP_TITLE, MB_OK | MB_ICONERROR)
            return
        try:
            new_cfg = load_config()
            relay.reload_config(new_cfg)
            logging.info("config reloaded")
            if config_is_complete(new_cfg):
                msg = ("Config reloaded successfully.\n\n"
                       f"Operator: {new_cfg['operator_name']} "
                       f"{new_cfg['operator_callsign'].upper()}\n"
                       f"Server: {new_cfg['server_url']}")
            else:
                msg = ("Config reloaded, but some required fields are still "
                       "blank:\n\n"
                       f"    server_url:        "
                       f"{'set' if new_cfg['server_url']        else 'BLANK'}\n"
                       f"    server_secret:     "
                       f"{'set' if new_cfg['server_secret']     else 'BLANK'}\n"
                       f"    operator_name:     "
                       f"{'set' if new_cfg['operator_name']     else 'BLANK'}\n"
                       f"    operator_callsign: "
                       f"{'set' if new_cfg['operator_callsign'] else 'BLANK'}\n\n"
                       "Forwarding will not start until all four are filled in.")
            msgbox(msg, APP_TITLE, MB_OK | MB_ICONINFORMATION)
        except Exception as e:
            logging.exception("reload failed")
            msgbox(f"Reload failed:\n\n{e}", APP_TITLE, MB_OK | MB_ICONERROR)

    def on_open_log(*_a):
        open_in_default_app(APP_DIR)

    icon: Optional[pystray.Icon] = None  # forward declaration for on_quit

    def on_quit(*_a):
        try:
            relay.stop()
        except Exception:
            pass
        if icon is not None:
            icon.stop()

    def status_text(_item=None):
        _, text = relay.status()
        return f"Status: {text}"

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Edit config",     on_edit_config),
        pystray.MenuItem("Reload config",   on_reload),
        pystray.MenuItem("Open log folder", on_open_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit",            on_quit),
    )
    icon = pystray.Icon("N1MMFreqForward",
                        make_icon_image(GRAY[0]),
                        APP_TITLE,
                        menu)

    # Background thread to keep the tray-icon color current.
    def color_loop():
        while not relay._stopped:
            time.sleep(2)
            try:
                color, _ = relay.status()
                icon.icon = make_icon_image(color)
            except Exception:
                pass
    threading.Thread(target=color_loop, name="color-loop", daemon=True).start()

    # First-run guidance.
    if not config_is_complete(cfg):
        # Show in a worker so we don't block the tray from coming up.
        def first_run_prompt():
            time.sleep(1.0)  # let tray icon appear first
            msgbox(
                "Welcome — first-run setup.\n\n"
                "I'll open the config file in Notepad now. Fill in:\n\n"
                "    server_url         (the shack URL the club gave you)\n"
                "    server_secret      (the shared secret)\n"
                "    operator_name      (your name)\n"
                "    operator_callsign  (your callsign)\n\n"
                "Save the file, then right-click the tray icon (look near "
                "the clock, click the ^ to see hidden icons) and choose "
                "'Reload config'.",
                APP_TITLE, MB_OK | MB_ICONINFORMATION,
            )
            time.sleep(0.7)
            open_in_default_app(CONFIG_FILE)
        threading.Thread(target=first_run_prompt, daemon=True).start()

    # Watchdog: auto-exit when N1MM stops sending.
    #
    # Phase 1 — initial wait: if no UDP at all arrives within
    #   N1MM_initial_wait_mins, assume N1MM isn't running and exit.
    # Phase 2 — rolling: after the first packet, restart the clock on
    #   every packet; exit when wait_mins elapses with no new UDP.
    # Either phase is disabled by setting its value to 0 in config.
    #
    # The watchdog reads wait times from relay.config on each iteration
    # so a Reload picks up new values immediately.
    #
    # on_quit is used as the exit action — same clean shutdown path as
    # the Quit menu item.
    def _watchdog():
        # ---- phase 1: wait for first packet ----
        with relay._lock:
            initial_mins = int(relay.config.get("N1MM_initial_wait_mins", 15))
        if initial_mins > 0:
            deadline = time.monotonic() + initial_mins * 60
            logging.info("watchdog: waiting up to %d min for first N1MM UDP",
                         initial_mins)
            while time.monotonic() < deadline:
                time.sleep(10)
                if relay._stopped:
                    return
                if relay.last_udp_at is not None:
                    break
            else:
                # timed out with no UDP at all
                logging.info("watchdog: no UDP in %d min — exiting", initial_mins)
                msgbox(
                    f"No N1MM UDP received in the first {initial_mins} minutes.\n\n"
                    f"N1MM Frequency Forwarder will now exit.\n\n"
                    f"Check that N1MM is running and that 'Radio' is ticked on the\n"
                    f"Broadcast Data tab pointing to 127.0.0.1:12060.",
                    APP_TITLE, MB_OK | MB_ICONINFORMATION,
                )
                time.sleep(3)   # give the MessageBox thread a moment to appear
                on_quit()
                return

        # ---- phase 2: rolling watchdog ----
        while not relay._stopped:
            time.sleep(30)      # check every 30 seconds
            with relay._lock:
                wait_mins = int(relay.config.get("N1MM_wait_mins", 15))
            if wait_mins <= 0:
                continue        # rolling watchdog disabled
            last = relay.last_udp_at
            if last is not None:
                age_mins = (time.monotonic() - last) / 60
                if age_mins >= wait_mins:
                    logging.info("watchdog: no UDP for %.1f min — exiting",
                                 age_mins)
                    msgbox(
                        f"No N1MM UDP received for {wait_mins} minutes.\n\n"
                        f"N1MM Frequency Forwarder will now exit.",
                        APP_TITLE, MB_OK | MB_ICONINFORMATION,
                    )
                    time.sleep(3)
                    on_quit()
                    return

    threading.Thread(target=_watchdog, name="watchdog", daemon=True).start()

    # icon.run() blocks on the main thread, which is exactly what
    # pystray wants on Windows.
    icon.run()

    logging.info("%s shutting down", APP_TITLE)
    relay.stop()
    return 0


def _crash_with_dialog(exc: BaseException) -> int:
    """Last-resort handler: write the traceback to a file in APP_DIR and
    show a MessageBox. So when something fails before logging is even
    ready, the user still gets a visible signal."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        with CRASH_FILE.open("w", encoding="utf-8") as f:
            f.write(tb)
    except Exception:
        pass
    msgbox(
        f"{APP_TITLE} could not start.\n\n"
        f"Error: {type(exc).__name__}: {exc}\n\n"
        f"Full traceback saved to:\n{CRASH_FILE}",
        APP_TITLE, MB_OK | MB_ICONERROR,
    )
    return 1


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException as e:
        rc = _crash_with_dialog(e)
    sys.exit(rc)
