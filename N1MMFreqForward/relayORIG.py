"""steprtool-relay
================

Runs on a remote operator's Windows machine. Captures N1MM RadioInfo UDP
broadcasts on localhost and forwards each TXFreq value to the shack
steprtool server over HTTPS, where the server's /api/n1mm/txfreq endpoint
applies it via the same auto-retune logic as a locally-connected N1MM.

UX is a tray icon (green=forwarding, yellow=receiving but not forwarding,
red=server unreachable, gray=no UDP yet). Right-click for Settings,
Open log folder, Reload config, Quit. No autostart — the operator is
expected to launch the relay manually when starting a remote session.

Config and log live in %APPDATA%\\steprtool-relay\\ on Windows, or
~/.config/steprtool-relay/ on other platforms (mainly for development).

Standalone build: see build.bat in this directory. PyInstaller produces
a single .exe that the operator runs directly (no installer, no Python).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional

import pystray
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageDraw, ImageFont


# ---------------------------- paths -------------------------------------

def _appdata_dir() -> Path:
    """Per-user writable config/log directory. Cross-platform."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home())
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(base) / "steprtool-relay"
    p.mkdir(parents=True, exist_ok=True)
    return p


APP_DIR    = _appdata_dir()
CONFIG_FILE = APP_DIR / "config.json"
LOG_FILE    = APP_DIR / "relay.log"

DEFAULT_CONFIG: dict = {
    "server_url":           "https://100.98.246.94:6300",
    "server_secret":        "",
    "operator_name":        "",
    "operator_callsign":    "",
    "ports":                [12060, 13063, 13065],
    "min_send_interval_ms": 500,
}

# Status tuple values (used by tray icon and status menu item)
GRAY   = ("gray",   "no UDP packets received")
YELLOW = ("orange", "receiving N1MM but not forwarding")
GREEN  = ("green",  "forwarding to server")
RED    = ("red",    "server unreachable")


# ---------------------------- config -------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                cfg.update(user)
        except Exception as e:
            logging.exception("config load failed, using defaults: %s", e)
    # Normalize
    try:
        cfg["ports"] = [int(p) for p in cfg.get("ports", [])] or DEFAULT_CONFIG["ports"]
    except Exception:
        cfg["ports"] = list(DEFAULT_CONFIG["ports"])
    cfg["min_send_interval_ms"] = int(cfg.get("min_send_interval_ms",
                                              DEFAULT_CONFIG["min_send_interval_ms"]))
    cfg["server_url"] = (cfg.get("server_url") or "").strip().rstrip("/")
    return cfg


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


# ---------------------------- HTTPS POST ---------------------------------

def post_txfreq(cfg: dict, tx_freq_tens_of_hz: int) -> tuple[bool, dict | str]:
    """POST a TXFreq value to the server. Returns (ok, parsed_response_or_error)."""
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
    # Disable TLS verification — Tailscale already encrypts and the steprtool
    # server uses a self-signed cert. (Per the project's design decision.)
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


# ---------------------------- UDP listener -------------------------------

class UdpListener:
    """Listens for N1MM RadioInfo XML on the given localhost UDP ports."""

    def __init__(self, host: str, ports: list[int],
                 on_txfreq: Callable[[int], None]):
        self.host = host
        self.ports = ports
        self.on_txfreq = on_txfreq
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._sockets: list[socket.socket] = []

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
                data, _ = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle(data)

    def _handle(self, data: bytes) -> None:
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
        tx = root.find("TXFreq") or root.find("txfreq")
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


# ---------------------------- Relay state machine ------------------------

class Relay:
    """Owns the UDP listener, dedupe/throttle, and HTTPS forwarding."""

    def __init__(self, config: dict):
        self.config = dict(config)
        self._lock = threading.Lock()
        self._last_sent_freq: Optional[int] = None
        self._last_sent_at:   float = 0.0
        self._last_udp_at:    Optional[float] = None
        self._last_post_ok_at:  Optional[float] = None
        self._last_post_err:    Optional[str]  = None
        self._stopped = False
        self._udp = UdpListener("127.0.0.1", config["ports"], self._on_txfreq)

    def start(self) -> None:
        self._udp.start()

    def stop(self) -> None:
        self._stopped = True
        self._udp.stop()

    def reload_config(self, new_config: dict) -> None:
        """Replace config in place. UDP ports are restarted only if they
        actually changed (most config changes don't need a UDP restart)."""
        with self._lock:
            old_ports = self.config["ports"]
            self.config = dict(new_config)
        if old_ports != new_config["ports"]:
            self._udp.stop()
            self._udp = UdpListener("127.0.0.1", new_config["ports"], self._on_txfreq)
            self._udp.start()

    def status(self) -> tuple[str, str]:
        """Return (color_name, human_text) for the tray icon and menu."""
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
            # N1MM stopped emitting some time ago
            return GRAY[0], "no recent UDP from N1MM"
        if ok_age < 60:
            return GREEN
        return YELLOW

    def _on_txfreq(self, tx_freq: int) -> None:
        if self._stopped:
            return
        now = time.monotonic()
        send = False
        with self._lock:
            self._last_udp_at = now
            min_interval = self.config["min_send_interval_ms"] / 1000.0
            same = (tx_freq == self._last_sent_freq)
            recent = (now - self._last_sent_at) < min_interval
            if same and recent:
                return  # debounce: identical freq inside the throttle window
            self._last_sent_freq = tx_freq
            self._last_sent_at   = now
            send = config_is_complete(self.config)
            cfg_copy = dict(self.config)
        if not send:
            logging.debug("config incomplete; not forwarding TXFreq=%d", tx_freq)
            return
        # Send on a worker so the UDP thread keeps draining
        threading.Thread(
            target=self._do_post, args=(cfg_copy, tx_freq), daemon=True,
        ).start()

    def _do_post(self, cfg: dict, tx_freq: int) -> None:
        ok, info = post_txfreq(cfg, tx_freq)
        with self._lock:
            if ok:
                self._last_post_ok_at = time.monotonic()
                # Only clear err on a successful response.
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


# ---------------------------- Settings dialog ----------------------------

def show_settings_dialog(root: tk.Tk, cfg: dict, on_saved: Callable[[dict], None]) -> None:
    """Modal settings dialog. Called from the tk main thread."""
    dlg = tk.Toplevel(root)
    dlg.title("steprtool-relay settings")
    dlg.transient(root)
    dlg.resizable(False, False)
    try:
        dlg.attributes("-topmost", True)
    except Exception:
        pass

    frm = ttk.Frame(dlg, padding=14)
    frm.grid(sticky="nsew")

    ttk.Label(frm, text="Server URL:").grid(row=0, column=0, sticky="e", pady=4)
    server_var = tk.StringVar(value=cfg.get("server_url", ""))
    ttk.Entry(frm, textvariable=server_var, width=42).grid(row=0, column=1, pady=4)

    ttk.Label(frm, text="Shared secret:").grid(row=1, column=0, sticky="e", pady=4)
    secret_var = tk.StringVar(value=cfg.get("server_secret", ""))
    ttk.Entry(frm, textvariable=secret_var, width=42, show="*").grid(row=1, column=1, pady=4)

    ttk.Label(frm, text="Your name:").grid(row=2, column=0, sticky="e", pady=4)
    name_var = tk.StringVar(value=cfg.get("operator_name", ""))
    ttk.Entry(frm, textvariable=name_var, width=42).grid(row=2, column=1, pady=4)

    ttk.Label(frm, text="Your callsign:").grid(row=3, column=0, sticky="e", pady=4)
    call_var = tk.StringVar(value=cfg.get("operator_callsign", ""))
    ttk.Entry(frm, textvariable=call_var, width=42).grid(row=3, column=1, pady=4)

    ttk.Label(frm, text="Min send interval (ms):").grid(row=4, column=0, sticky="e", pady=4)
    interval_var = tk.StringVar(value=str(cfg.get("min_send_interval_ms", 500)))
    ttk.Entry(frm, textvariable=interval_var, width=10).grid(row=4, column=1, sticky="w", pady=4)

    ttk.Label(frm, text=f"Config file:\n{CONFIG_FILE}",
              foreground="#666", font=("TkDefaultFont", 8)).grid(
        row=5, column=0, columnspan=2, sticky="w", pady=(10, 4))

    btns = ttk.Frame(frm)
    btns.grid(row=6, column=0, columnspan=2, pady=(8, 0))

    def on_ok():
        try:
            iv = int(interval_var.get().strip() or "500")
        except ValueError:
            messagebox.showerror("steprtool-relay", "Send interval must be an integer.")
            return
        new_cfg = dict(cfg)
        new_cfg["server_url"]           = server_var.get().strip().rstrip("/")
        new_cfg["server_secret"]        = secret_var.get().strip()
        new_cfg["operator_name"]        = name_var.get().strip()
        new_cfg["operator_callsign"]    = call_var.get().strip().upper()
        new_cfg["min_send_interval_ms"] = max(0, iv)
        save_config(new_cfg)
        on_saved(new_cfg)
        dlg.destroy()

    def on_cancel():
        dlg.destroy()

    ttk.Button(btns, text="Save",   command=on_ok).pack(side="left", padx=6)
    ttk.Button(btns, text="Cancel", command=on_cancel).pack(side="left", padx=6)

    dlg.protocol("WM_DELETE_WINDOW", on_cancel)
    dlg.update_idletasks()
    # Center over the (hidden) root
    w, h = dlg.winfo_width(), dlg.winfo_height()
    sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
    dlg.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")
    dlg.focus_force()


# ---------------------------- tray icon ----------------------------------

def make_icon_image(color: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([4, 4, 60, 60], fill=color, outline="#222", width=2)
    try:
        f = ImageFont.truetype("arial.ttf", 32)
    except Exception:
        f = ImageFont.load_default()
    # Centered "R"
    try:
        bbox = d.textbbox((0, 0), "R", font=f)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((64 - tw) / 2 - bbox[0], (64 - th) / 2 - bbox[1] - 2),
               "R", fill="white", font=f)
    except Exception:
        d.text((22, 14), "R", fill="white", font=f)
    return img


def build_tray(relay: Relay,
               on_settings: Callable[[], None],
               on_reload:   Callable[[], None],
               on_open_log: Callable[[], None],
               on_quit:     Callable[[], None]) -> pystray.Icon:
    def status_text(_item=None):
        _, text = relay.status()
        return f"Status: {text}"

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings...",      lambda i, it: on_settings()),
        pystray.MenuItem("Reload config",    lambda i, it: on_reload()),
        pystray.MenuItem("Open log folder",  lambda i, it: on_open_log()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit",             lambda i, it: on_quit()),
    )
    icon = pystray.Icon("steprtool-relay",
                        make_icon_image(GRAY[0]),
                        "steprtool-relay",
                        menu)
    return icon


# ---------------------------- main ---------------------------------------

def setup_logging() -> None:
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
    # Also echo to stderr when running from console
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def main() -> int:
    setup_logging()
    logging.info("steprtool-relay starting; config=%s", CONFIG_FILE)

    cfg = load_config()
    if not CONFIG_FILE.exists():
        save_config(cfg)  # write defaults on first run

    relay = Relay(cfg)
    relay.start()

    # Tkinter root — hidden, runs the main loop.
    root = tk.Tk()
    root.withdraw()
    root.title("steprtool-relay")

    # ---- thread-safe bridges from pystray (background thread) to tk ----

    def show_settings_safe():
        root.after(0, lambda: show_settings_dialog(
            root, relay.config,
            on_saved=lambda c: (relay.reload_config(c),
                                logging.info("config reloaded from dialog"))))

    def reload_safe():
        new_cfg = load_config()
        relay.reload_config(new_cfg)
        logging.info("config reloaded from file")

    def open_log_safe():
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(APP_DIR))
            elif sys.platform == "darwin":
                os.system(f"open '{APP_DIR}'")
            else:
                os.system(f"xdg-open '{APP_DIR}'")
        except Exception as e:
            logging.warning("open log folder failed: %s", e)

    def quit_safe():
        root.after(0, root.quit)

    icon = build_tray(relay, show_settings_safe, reload_safe,
                      open_log_safe, quit_safe)

    # Run pystray in a background thread so the main thread owns tk.
    threading.Thread(target=icon.run, name="tray", daemon=True).start()

    # Periodically refresh the tray icon color
    def refresh_icon():
        try:
            color, _ = relay.status()
            icon.icon = make_icon_image(color)
        except Exception:
            pass
        root.after(2000, refresh_icon)
    root.after(2000, refresh_icon)

    # First-run prompt: if config is incomplete, open settings immediately
    if not config_is_complete(cfg):
        root.after(300, show_settings_safe)

    try:
        root.mainloop()
    finally:
        logging.info("steprtool-relay shutting down")
        try: icon.stop()
        except Exception: pass
        relay.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
