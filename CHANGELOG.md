# Changelog

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
