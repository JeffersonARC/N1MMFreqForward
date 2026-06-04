# N1MMFreqForward

A small Windows helper for remote operators of the Jefferson ARC StepIR
antenna control system. While you run N1MM on your own computer, the relay
captures N1MM's frequency broadcasts and forwards them to the shack so the
antenna follows your VFO automatically — just as if N1MM were running on
the shack computer itself.

You only need this if you operate the StepIR remotely. Local operators at
the shack don't need it.

---

## Install & first run

1. Download `N1MMFreqForward.exe` from the club.
2. Double-click to run. Windows will probably show "Windows protected your
   PC" — click **More info**, then **Run anyway**. (This is normal for
   small free utilities; we don't pay for code-signing.)
3. The first time it starts, a Settings dialog appears. Fill in:
   - **Server URL** — the steprtool address the club gave you
     (e.g. `https://100.98.246.94:6300`)
   - **Shared secret** — also provided by the club
   - **Your name** and **callsign** — these appear in the activity feed
     so other operators can see whose N1MM is driving the antenna
4. Click **Save**. The relay icon appears in your system tray (near the
   clock). Right-click for menu.

The tray icon color tells you what's happening:

| Color  | Meaning                                                |
|--------|--------------------------------------------------------|
| Gray   | No UDP from N1MM yet                                   |
| Yellow | N1MM is sending UDP, but the server isn't accepting it |
| Green  | Forwarding successfully                                |
| Red    | Server unreachable                                     |

---

## Configure N1MM to broadcast RadioInfo

Once per N1MM installation:

1. In N1MM, open **Config → Configure Ports, Mode Control, Audio, Other**.
2. Go to the **Broadcast Data** tab.
3. Check **Radio**.
4. In the IP Addresses & Ports field, ensure `127.0.0.1:12060` is listed.
   (That's N1MM's default; the relay listens there.)
5. Click **OK**. N1MM will start emitting `<RadioInfo>` packets whenever
   your VFO frequency changes.

You do not need to restart N1MM.

---

## Day-to-day use

- Start the relay before starting your operating session.
- Quit it (tray icon → Quit) when you're done — it's not autostart on
  purpose, so it never runs in the background unless you intend to drive
  the shack antenna.
- If the antenna doesn't follow your VFO and you expected it to:
  - Check the tray icon color
  - Right-click → Open log folder → look at `relay.log`
  - Verify N1MM is actually sending RadioInfo (steps above)
  - Verify the shack's steprtool is running and the antennas aren't
    marked disconnected on the home page

---

## Where things live

- **Config and log:** `%APPDATA%\N1MMFreqForward\` (typically
  `C:\Users\YourName\AppData\Roaming\N1MMFreqForward\`).
- **`config.json`** — editable directly if you prefer; choose
  "Reload config" from the tray menu after editing.
- **`relay.log`** — rotates at 1 MB, keeps 3 generations.

---

## Building from source

If you have the source and want to build your own .exe (e.g., to update
the default server URL before distribution):

1. Install Python 3.11+ from python.org. Check "Add to PATH".
2. In a Command Prompt in this directory:
   ```
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements-build.txt
   build.bat
   ```
3. The result is `dist\N1MMFreqForward.exe` — a single ~15 MB standalone
   file that needs no Python on the target machine.
