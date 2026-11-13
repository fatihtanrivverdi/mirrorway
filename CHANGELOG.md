# Changelog

## 3.5.0 — screen arrangement
- **Settings › Arrangement** on the Mac: a picture of the Mac's displays and Windows's monitors. Drag the Windows
  monitors (as one group) to where they stand on your desk; they snap to the nearest side of the Mac, which also
  sets "Windows is on this side". Sizes are in the same units on both sides (Mac points; Windows pixels divided by
  Windows's display scale, which Windows now reports).
- With an arrangement the pointer crosses straight, like between displays on a Mac: only the part where the screens
  face each other leads across, the rest of each edge is a wall. Out and back through the same spot lands where it
  left. **Reset** returns to the default (the Mac's edge leads to the whole of Windows's edge, in proportion), and
  so does choosing another side in the menu. While the second screen is open its own routes apply, as before.
- Windows keeps its walls when the Mac tells it which part of its edge faces the Mac (3.5); an older Windows
  lets the pointer back from anywhere on its edge and the Mac places it at the nearest point.

## 3.4.1 — Windows's sound on the Mac, first release
3.4.0 was not published: one test failed on the Windows runner. Everything in 3.4.0 below is in this version, plus:
- On the Mac, Windows's sound keeps the AudioQueue running through short gaps (rebuffering) and pauses the audio
  hardware only after 3 seconds of silence. Starting it took ~400 ms on the macOS runner, which would have added
  delay after every short pause.
- When the Mac refuses Windows's sound (not set to play on the Mac), Windows now always shows the reason: the Mac
  waits for Windows to read it before closing, and Windows reads it even if its own send failed first.
- CI on a Windows runner with a virtual sound card, for real: another program's tone is captured (−12 dBFS),
  Mirrorway's own process tree is left out, capture continues with the speakers muted, and the tone reached the
  Mac side 94 ms after it was started (including starting the program that played it). The macOS runner played the
  AudioQueue's sound and ScreenCaptureKit heard it.

## 3.4.0 — Windows's sound on the Mac; one choice for where sound plays
- **Where sound plays** is now one choice on the Mac (Mirrorway › Audio, Settings › Audio) and on Windows
  (Settings › Audio): **each on its own** (the default), **all on Windows** (the Mac's sound on Windows, as before)
  or **all on the Mac** (new). The Audio tile in both panels and ⌃⌥A switch between *each on its own* and the
  direction used last.
- **All on the Mac:** Windows's sound plays through the Mac's current output, for example in headphones connected
  to the MacBook. Windows captures every program except Mirrorway with WASAPI process loopback (Windows 11; Windows
  10 build 20348 and later) and mutes its own speakers meanwhile (unmuted afterwards, also after a crash). Older
  Windows 10 captures what the speakers play and leaves them on. The Mac plays it through an AudioQueue with the
  same short buffering as Windows uses for the Mac's sound; during silence nothing is sent and the Mac's output is
  paused. Both computers need 3.4.
- Settings changed: the Mac's "Play the Mac's sound on Windows" switch became the choice above (an existing setting
  carries over as "all on Windows"). An older Windows can still turn the Mac's sound on and off.
- CI: a new job installs a virtual sound card on a Windows runner and checks the capture (another program's tone is
  captured, Mirrorway's own is not, still captured with the speakers muted), the speakers' mute and the whole path
  through Mirrorway; the macOS job plays sound through the AudioQueue.

## 3.3.0 — the Mac's keyboard on Windows' lock screen, Ctrl+Alt+Del and UAC prompts
- **Lock screen support** (Windows Settings › General › Lock screen › Install…, one administrator prompt): type
  your Windows password on the lock screen and the sign-in screen after a restart with the Mac's keyboard, answer
  UAC prompts with the Mac's mouse, and send **Ctrl+Alt+Del with ⌃⌥⌫** on the Mac.
- A Windows service (MirrorwaySecure, LocalSystem) keeps a helper running as SYSTEM in the session at the console.
  The helper connects to the Mac on its own with this PC's device key, verifies the Mac's key, and reports which
  desktop Windows shows; while it is a secure desktop the Mac sends the keyboard and mouse to the helper, which types
  from a thread attached to that desktop. Nothing on the PC can make it type, so it cannot approve UAC prompts for
  another program. Ctrl+Alt+Del goes through the service (SendSAS); the "software SAS" policy is set to allow
  services and restored on removal.
- Its files are under Program Files (only administrators can change them; the in-app update leaves them alone) and
  its key is readable only by SYSTEM and administrators. Removing it (Settings, or uninstalling Mirrorway) removes
  the service, the files and the policy change.
- When Windows locks while you control it, control now stays on Windows if lock screen support is installed (you can
  type the password); without it, or when the Mac locks too, it comes back to the Mac as before.
- CI installs lock screen support on a real Windows runner, checks the helper connects as SYSTEM and reports the
  desktop, opens the Ctrl+Alt+Del screen and closes it with Escape typed by the helper, then removes everything.

## 3.2.4 — the pointer keeps its place; audio from chosen apps
- **Fix: the pointer landed somewhere else after every switch.** With several Windows monitors side by side, the
  way back measured the position along one monitor while the way in measured it along all of them, so going down
  to Windows and straight back moved the Mac's pointer (300 → 600 pt, 1500 → 1200 pt on a 1800 pt wide Mac). Now
  the way out is the exact inverse of the way in, whatever the monitors: back through the same edge, the pointer is
  where it left.
- With the shortcut (⌃⌥ arrows) or the panel's button, Windows' pointer stays where it is and the Mac's comes back
  to where it was (before, both jumped to the middle of the screen).
- **Audio from chosen apps:** Settings › Audio › "Send the sound of" › "Only the apps below", then switch on the
  apps whose sound should play on Windows. The choice applies at once; apps that start later are picked up within
  a few seconds.
- **The audio level in the log is right:** the minute line gave "level avg silent" even while music played (the
  silent packets between songs pulled the average of dB values down). It now shows the level while sound played,
  how much of the minute had sound and the loudest moment; dropouts and trims are counted per minute, not since
  the start.

## 3.2.3 — lock both computers together
- **New setting on the Mac, Settings › General › "Lock both computers together"** (off by default): when the Mac
  locks (lid closed, ⌃⌘Q, screen saver, idle), Windows locks too, like Windows + L; when Windows locks, the Mac
  locks too. Unlock each computer as usual. A lock the other side reports right after it was asked for is not sent
  back.
- Whenever Windows or the Mac locks, the keyboard and mouse come back to the Mac (Windows' lock screen does not
  take Mirrorway's input yet), also with the setting off.
- Windows follows its lock with WTSRegisterSessionNotification; the Mac with the system's lock notifications and
  locks with the system's own lock function. Both computers need 3.2.3.

## 3.2.2 — a ring shows where the pointer appears
- **After every switch** (screen edge, shortcut, the panel's button) a violet ring grows and fades for half a second
  where the pointer appears: on Windows when the Mac's pointer arrives, on the Mac when control comes back. It has
  a thin white edge, so it shows on light and dark screens, and it never takes clicks or the focus.
- Windows draws it in a layered, click-through window (smooth edges, at the monitor's scale); the Mac animates it
  with Core Animation above every window, also full-screen apps and all Spaces.
- Turn it off in Settings › General: "Show a ring where the pointer appears" (on each computer separately).
- CI shows the ring on real Windows and macOS: the window's styles, the place, hiding after the animation, a new
  switch during the animation.

## 3.2.1 — file transfers: whole blocks per read
- The first real measurement with 3.2.0, Mac → Windows: a folder of 20,929 files went at 20 MB/s while one large
  file went at 44 MB/s. The file connection read every few KB that arrived separately (each time coming back to
  Python while the writer threads were busy); now it waits for a whole 1 MB block in one system call, on both
  sides, with the system's own 60-second timeout.
- CI now also copies the 5,000-file folder over a simulated 40 MB/s link, both receiving and sending; it has to
  reach 85% of the link. On the Windows runner receiving went from 34–41 to 38–42 MB/s (the runners vary; the
  real 20 MB/s case could not be reproduced there, the next real copy will show whether this was it).
- The log line of a received transfer shows how long creating the folders took.

## 3.2.0 — folders of many small files copy much faster
- **Copying a folder with thousands of small files** (a project, an app folder) no longer waits for each file in
  turn: the sending side reads files ahead on several threads, the receiving side writes them on several threads,
  and small files travel packed together (about 1 MB per packet). Opening a file costs more than its content,
  above all on Windows where Defender scans it; now those waits overlap.
- Measured on CI with a 5,000-file folder through the encrypted connection: Mac 60 → 125 MB/s, Windows 24 →
  40 MB/s (on the same machine; between two computers the network is the limit).
- Every transfer's log line now shows where the time went, for example `5002 files, 418 folders in 3.4 s,
  180 frames (packed); waiting for the disk 2.6 s, for the network 0.7 s`.
- Both sides need 3.2 for the packed transfer; with an older version on the other side files still go the old
  way.
- The folder listing before a transfer is faster on Windows, and the file connection uses larger network buffers.

## 3.1.6 — drag-and-drop disk image for the Mac
- **`Mirrorway-3.1.6-mac.dmg` (Apple silicon):** drag Mirrorway into Applications and open it. The app brings its
  own Python with the pinned packages, so nothing has to be installed; the first start needs **Open Anyway** once
  in System Settings (the app is not signed by Apple).
- The app installs its code to `~/.mirrorway/app` on the first start; updates still go there and keep the
  permissions. An update that needs a new package adds it beside the app's Python, never inside the app.
- Opened from the disk image or the Downloads folder, Mirrorway asks to be moved to Applications first. The login
  item follows the app when it moves.
- CI builds the disk image and tries it on real macOS: copied out, first start, an older and a newer installed
  version, an update with new packages.
- Intel Macs keep the zip package with `install-mac.command`.

## 3.1.5 — one-click installer for Windows
- **`Mirrorway-3.1.5-windows-setup.exe`:** double-click and Mirrorway is installed and running; no questions, no
  separate Python (it brings its own, with the pinned packages), no administrator rights (only the optional
  firewall rule asks). It replaces an earlier Mirrorway or Köprü, keeps the settings and pairing, and can be
  removed from Windows' Installed apps.
- An update keeps your choices: a deleted desktop shortcut or a turned-off start with Windows stay that way.
- CI builds the installer on real Windows and tries it every release: install, start (Mirrorway must keep running),
  install again over a running Mirrorway, uninstall.

## 3.1.4 — fix: Mirrorway on Windows closed right after starting
- The Language submenu of the tray icon made pystray refuse the menu, so the Windows app ended at once (since 3.0).
  The CI's stand-in for pystray now checks menu actions the same way.

## 3.1.3 — the Settings window (Windows)
- **New Settings window on Windows**, in the style of Windows 11 and following its light/dark theme: Connection
  (status with Reconnect, the paired Mac with its fingerprint, a 6-digit pairing code in separate boxes, the Mac's
  address, starting with Windows), Mac screen, Audio, Clipboard and files, Shortcuts and General.
- **Start with Windows** can be turned off and on (the shortcut in the Startup folder).
- The `Ctrl + Alt` shortcut letter can be changed or turned off; it applies at once.

## 3.1.2 — the tray panel (Windows)
- **Tray panel.** A left click on the Windows tray icon opens a panel above it with the connection status, the
  edge of the Mac's screen that leads to this PC, **Show/Hide the Mac's screen** and tiles for clipboard, the Mac's
  sound, files, reconnecting, pairing and diagnostics. It follows the Windows light/dark app theme and closes when
  it loses focus. The menu is still on the right click.
- **Ctrl + Alt + S** shows or hides the Mac's screen on Windows (skipped, and logged, if another app uses it).
- **The Mac's sound from Windows.** The **Mac's sound** tile turns the audio transfer on the Mac on or off (new
  message: the Mac accepts it from 3.1.2 on).
- **Files can be turned off on Windows** (the **Files** tile): copied files are then neither sent nor received.

## 3.1.1 — the Settings window (Mac)
- **Settings window.** **Settings…** (`⌘,`) in the quick panel and the menu opens a window with a sidebar of
  pages: General, Keyboard and Mouse, Screen and Video, Audio, Clipboard and Files, Network, Paired Devices and
  Updates. It holds every setting from the menu, follows the light and dark appearance, and changes apply at once.
- **Quick shortcuts can be changed** in Settings › General (a letter, or off).
- **The state next to the menu bar icon** (• and Win) can be turned off; warnings (! and ⏸) always show.
- The pages come from `mirrorway/prefs.py`, tested without AppKit; CI opens the window on macOS and prints pages.

## 3.1.0 — the quick panel (Mac)
- **Quick panel.** Clicking the Mirrorway icon in the menu bar opens a panel with the connection status, the
  switch button and one-click tiles for the screen on Windows, the second screen, audio, clipboard, files and
  Wake-on-LAN; pairing, network and disconnect are one click away. It follows the light and dark appearance. The
  full menu opens with a right click or **Settings…**; **Click Opens the Quick Panel** turns the panel off.
- **Quick shortcuts.** `⌃⌥S` shows or hides the Mac's screen on Windows, `⌃⌥D` turns the second screen on or off,
  `⌃⌥A` moves the Mac's sound between the Mac and Windows and `⌃⌥M` opens the panel. They also work while you
  control Windows. The letters are set in the `shortcuts` setting.
- **The Mac can close the screen window on Windows** (the Screen tile and `⌃⌥S`); Windows needs 3.1 for this,
  which it receives from the Mac when it connects.

## 3.0.0 — 6 November 2026
Köprü is now **Mirrorway**, and the project is open source under the MIT License.

- **New name and logo.** The app, its menus, logs, settings and code are named Mirrorway. The logo (an M over its
  reflected W) is the Mac app icon, the menu bar icon and the Windows tray icon; the tray icon's colour still shows
  the connection state.
- **English and Turkish interface.** Mirrorway follows the system language; choose another one under
  **Mirrorway › Language** on the Mac or in the Windows tray menu.
- **Moving over from Köprü.** Settings, pairing, trusted networks and device keys are carried over on first start,
  so there is no need to pair again. Because the connection format changed, update both computers with the new
  installers: they close and remove Köprü. On the Mac, grant the permissions to Mirrorway.app again.
- **Updates without an account.** The repository is public, so the Mac checks for new releases without a GitHub
  token; the token menu is gone. Releases are still signed with Ed25519 and verified before installing.
- **Download packages.** Every release has a Mac package (with Mirrorway.app, built and signed in CI from
  `macapp/launcher.c`) and a Windows package. In a copy of the source code, `install-mac.command` builds
  Mirrorway.app itself.

## Earlier versions (as Köprü)
- **2.8** — Audio transfer: the Mac's sound plays on Windows (ScreenCaptureKit, 48 kHz stereo), optionally muting
  the Mac meanwhile.
- **2.7** — File copy and paste between Finder and Explorer over a separate encrypted connection; notifications
  through osascript.
- **2.6** — Trackpad gestures act on Windows while controlling it (virtual desktops, Task View, show desktop, zoom).
- **2.5** — A Windows monitor as the Mac's second screen through a virtual display (experimental).
- **2.4** — Rich clipboard: HTML, RTF and images, sent in chunks; clipboard transfers in the log.
- **2.3** — In-app updates on the Mac: signed release packages, verification, rollback.
- **2.2** — Pinned dependencies (version + hash), CI on Linux, macOS and Windows.
- **2.1** — Works only on trusted (home) networks; diagnostics bundle.
- **2.0** — Pairing with a 6-digit code (SPAKE2), permanent device keys (Keychain / DPAPI), paired device list.
- **1.9** — Adaptive H.264 bitrate and frame rate.
- **1.8** — GPU-drawn viewer on Windows (SDL2, Direct3D 11), hardware decoding, pipelined encoding on the Mac.
- **1.7** — H.264 video with the Mac's hardware encoder (VideoToolbox).
- **1.6** — Edge bounce protection, Wake-on-LAN.
- **1.5** — Partial redrawing on Windows, automatic pointer speed, special characters with Option.
- **1.4** — Only changed screen regions are sent; simplejpeg.
- **1.3** — Windows updates itself from the Mac; latency and frame measurements.
- **1.2** — Movement computed from pointer positions; "already running" handling; VPN-aware addresses.
- **1.1** — Mac app bundle (permissions go to the app, not Terminal), open at login, one-click Windows installer.
- **1.0** — First version: keyboard and mouse to Windows at the screen edge or with a shortcut, the Mac's screen on
  Windows, text clipboard sync, AES-256-GCM encrypted connection.
