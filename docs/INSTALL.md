# Installing InputDeck

* [1. Quick install](#1-quick-install)
* [2. Manual install per distribution](#2-manual-install-per-distribution)
* [3. One-time access setup](#3-one-time-access-setup)
* [4. First run](#4-first-run)
* [5. Autostart](#5-autostart)
* [6. Updating and uninstalling](#6-updating-and-uninstalling)
* [Troubleshooting](#troubleshooting)

## 1. Quick install

```bash
git clone https://github.com/t3ng4/inputdeck.git
cd inputdeck
./install.sh
```

The script detects your distribution family (pacman, apt or dnf), installs what is missing after asking,
copies InputDeck to `~/.local/share/inputdeck/app`, installs the icon in all sizes, creates a launcher
`~/.local/bin/inputdeck` and a menu entry. It never needs root except for the distribution packages.

Options: `--yes` (no prompts), `--no-deps` (skip package installation), `--service` (also install the
headless auto-switch service), `--setup-access` (see section 3), `--uninstall`.

Make sure `~/.local/bin` is in your `PATH`. Or run straight from the checkout without installing:
`python3 -m inputdeck`.

## 2. Manual install per distribution

InputDeck needs **PyQt6** and **python-evdev** for your system Python (3.9 or newer).

| Family | Distributions | Command |
|---|---|---|
| pacman | Arch, CachyOS, Garuda, EndeavourOS, Manjaro | `sudo pacman -S --needed python-pyqt6 python-evdev` |
| apt | Debian 13 and Ubuntu 24.04 or newer, Linux Mint, Pop!_OS | `sudo apt install python3-pyqt6 python3-evdev` |
| dnf | Fedora | `sudo dnf install python3-qt6 python3-evdev` |

Package names can differ between releases. If one is not found, search for it (`dnf search PyQt6`,
`apt search pyqt6`) or use the virtual environment route below.

**Any other distribution, or an older release:**

```bash
python3 -m venv --system-site-packages ~/.local/share/inputdeck/venv
~/.local/share/inputdeck/venv/bin/pip install PyQt6 evdev      # evdev compiles: needs gcc and the kernel headers
~/.local/share/inputdeck/venv/bin/python -m inputdeck          # run it
```

### Optional: input-remapper

Only needed if you want the input-remapper engine instead of the built-in one. It must be **2.x**.

| Family | Install |
|---|---|
| Arch-based | from the AUR / chaotic-aur (for example `yay -S input-remapper-git`) or your distribution's repository |
| Debian / Ubuntu | `sudo apt install input-remapper` (check that it is 2.x, otherwise use the `.deb` from its GitHub releases) |
| Fedora | `sudo dnf install input-remapper` |

Then `sudo systemctl enable --now input-remapper`.

### Optional: window-aware rules (`xprop`)

Rules set to "window visible" or "window focused" (for example: use the *Game* preset only while the game is
on screen, not while it is minimized or in the tray) need the `xprop` tool. `install.sh` offers to install it.
Manually:

| Distribution | Command |
|--------------|---------|
| Arch / CachyOS / Garuda | `sudo pacman -S xorg-xprop` |
| Debian / Ubuntu | `sudo apt install x11-utils` |
| Fedora | `sudo dnf install xorg-x11-utils` |

Without it everything else works; those rules then behave like "running". See
[USAGE](USAGE.md#only-while-the-window-is-on-screen-minimized-or-tray-hidden-programs).

## 3. One-time access setup

Reading keys from your devices needs the `input` group. The built-in engine also creates virtual devices
and needs write access to `/dev/uinput`. Both in one step:

```bash
./install.sh --setup-access        # shows what it will do and asks before using sudo
```

It adds you to the `input` group, installs the udev rule
`KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"`, and loads the `uinput`
kernel module at boot. **Log out and back in** afterwards so the group change applies.

Doing it by hand: `sudo usermod -aG input $USER`, then create
`/etc/udev/rules.d/99-inputdeck-uinput.rules` with the rule above, `echo uinput | sudo tee
/etc/modules-load.d/inputdeck-uinput.conf`, `sudo modprobe uinput` and
`sudo udevadm control --reload-rules && sudo udevadm trigger`.

## 4. First run

```bash
inputdeck --check      # every line should say [ ok ]
inputdeck
```

1. Pick a device on the left (use a **mouse** for your first test of the built-in engine, not your only keyboard).
2. **Add mapping…**, press **Record…**, press the button on the device, choose the output.
3. Leave *Apply the preset right after saving* ticked. The banner shows what is active.

If something goes wrong: <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>Esc</kbd> stops the built-in
engine instantly. **Options → Reset to defaults** (or `inputdeck --reset`) does a full clean-up without
touching your presets.

## 5. Autostart

* **With the window / tray**: add `inputdeck --tray` to your desktop's autostart (KDE: *System Settings →
  Autostart*; GNOME: *Startup Applications* or a `.desktop` file in `~/.config/autostart/`). Tray icons on
  GNOME need the "AppIndicator and KStatusNotifierItem Support" extension.
* **Headless auto-switching**: `./install.sh --service` installs and starts a systemd *user* service
  (`systemctl --user status inputdeck`). Untick *Auto-switch* in the GUI if you let the service do the switching.
* With the **built-in engine** the engine process is started automatically when a preset is applied.
  Presets you apply by hand do not survive a reboot; auto-switch rules with a *fallback* (DEFAULT) rule
  apply a preset as soon as the service or the app starts.

## 6. Updating and uninstalling

Update: `git pull && ./install.sh --no-deps`. Uninstall: `./install.sh --uninstall`. Your presets, rules,
macro library and settings are kept; delete `~/.config/inputdeck` and `~/.local/share/inputdeck` yourself if
you want them gone.

## Troubleshooting

First step, always: `inputdeck --check` and **Logs → Engine log** / **Logs → Auto-switch & app log**.

| Symptom | Likely cause and fix |
|---|---|
| "No devices found" / device list empty | You are not in the `input` group yet: `./install.sh --setup-access`, then log out and in. Verify with `groups`. |
| *Could not grab the device … busy* | Another remapper (the input-remapper daemon, keyd, …) holds the device. Stop it, or switch engine under **Options → Remapping engine**. |
| Built-in engine: "permission to create virtual devices (/dev/uinput)" fails | Run `./install.sh --setup-access`, log out and in. |
| A mapping does nothing with the built-in engine | It may have been skipped: apply it with the **Apply preset** button (a dialog lists skipped mappings) or open **Logs → Engine log**. Axis mappings and macro functions like `if_single` are not supported by the built-in engine. |
| Mapping stopped working after a kernel / firmware update | **Options → Repair stale device IDs for this device…** |
| Keys feel stuck or the keyboard is unresponsive | <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>Esc</kbd>; then **Options → Reset to defaults** or `inputdeck --reset`. |
| `--check` complains about the "1.x preset folder" | Your input-remapper is version 1.x, which uses another preset format. Upgrade to 2.x, or use the built-in engine. |
| `qt.qpa.plugin: Could not load the Qt platform plugin "xcb"` | Install the missing system library (Debian/Ubuntu: `libxcb-cursor0`), or try `QT_QPA_PLATFORM=wayland inputdeck` / `QT_QPA_PLATFORM=xcb inputdeck`. |
| `kf.iconthemes: Icon theme "oxygen" not found` on KDE | Harmless, printed by the desktop's Qt theme plugin. |
| Letter outputs like `p` are the wrong key on a non-QWERTY layout (built-in engine) | Use layout-independent names such as `KEY_P`. |
| No tray icon on GNOME | Install the AppIndicator extension, or untick *Keep running in the system tray*. |

Still stuck? Open an issue with the *Bug report* form and paste the `--check` output and the logs.
