#!/usr/bin/env bash
# InputDeck installer (per-user, no root except for distro packages).
# Works on Arch-based (Arch, CachyOS, Garuda, EndeavourOS...), Debian-based (Debian, Ubuntu, Mint...)
# and Fedora-based systems. Anything else: it tells you what to install by hand.
#
#   ./install.sh [--yes] [--no-deps] [--service]
#   ./install.sh --setup-access     (one-time: input group + /dev/uinput rule, for recording and the built-in engine)
#   ./install.sh --uninstall
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
APP_DIR="$DATA/inputdeck/app"
BIN="$HOME/.local/bin/inputdeck"
DESKTOP="$DATA/applications/inputdeck.desktop"
ICON_ROOT="$DATA/icons/hicolor"
UNIT="$CONF/systemd/user/inputdeck.service"

YES=0; DEPS=1; SERVICE=0; UNINSTALL=0; ACCESS=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        --no-deps) DEPS=0 ;;
        --service) SERVICE=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --setup-access) ACCESS=1 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if ((ACCESS)); then
    cat <<'MSG'
This will (with sudo):
  - add you to the 'input' group            (read /dev/input to record keys)
  - install a udev rule for /dev/uinput     (create virtual devices; only the built-in engine needs this)
  - make the 'uinput' kernel module load at boot
MSG
    if ((!YES)); then read -r -p "Continue? [y/N] " a; [[ "${a:-n}" =~ ^[Yy]$ ]] || exit 0; fi
    SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
    export PATH="$PATH:/usr/sbin:/sbin"
    $SUDO usermod -aG input "${SUDO_USER:-${USER:-$(id -un)}}"
    echo 'KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"' \
        | $SUDO tee /etc/udev/rules.d/99-inputdeck-uinput.rules >/dev/null
    echo uinput | $SUDO tee /etc/modules-load.d/inputdeck-uinput.conf >/dev/null
    $SUDO modprobe uinput || true
    $SUDO udevadm control --reload-rules && $SUDO udevadm trigger --name-match=uinput || true
    echo "Done. Log out and back in so the new group membership applies."
    exit 0
fi

if ((UNINSTALL)); then
    systemctl --user disable --now inputdeck.service 2>/dev/null || true
    rm -rf "$APP_DIR" "$BIN" "$DESKTOP" "$UNIT"
    find "$ICON_ROOT" -name inputdeck.png -o -name inputdeck.svg 2>/dev/null | xargs -r rm -f
    echo "Removed the program. Your rules/settings in $CONF/inputdeck and your presets are untouched."
    exit 0
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null \
    || { echo "Python 3.9 or newer is required." >&2; exit 1; }

# ---------------------------------------------------------------- dependencies
if   [[ -n "${INPUTDECK_PM:-}" ]];             then PM="$INPUTDECK_PM"      # override: pacman | apt | dnf
elif command -v pacman  >/dev/null 2>&1; then PM=pacman
elif command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf     >/dev/null 2>&1; then PM=dnf
else PM=""; fi

case "$PM" in pacman|apt|dnf) ;; *) PM="" ;; esac

SUDO=""; [[ $EUID -ne 0 ]] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

pkg_install() {            # pkg_install <packages...>
    local flag=""
    case "$PM" in
        pacman) ((YES)) && flag="--noconfirm"; $SUDO pacman -S --needed $flag "$@" ;;
        apt)    ((YES)) && flag="-y";          $SUDO apt-get install $flag "$@" ;;
        dnf)    ((YES)) && flag="-y";          $SUDO dnf install $flag "$@" ;;
    esac
}

missing_py=()
python3 -c "import PyQt6.QtWidgets" 2>/dev/null || missing_py+=(PyQt6)
python3 -c "import evdev" 2>/dev/null            || missing_py+=(evdev)

if ((DEPS)) && ((${#missing_py[@]})); then
    echo "Missing Python modules: ${missing_py[*]}"
    pkgs=()
    case "$PM" in
        pacman) [[ " ${missing_py[*]} " == *" PyQt6 "* ]] && pkgs+=(python-pyqt6)
                [[ " ${missing_py[*]} " == *" evdev "* ]] && pkgs+=(python-evdev) ;;
        apt)    [[ " ${missing_py[*]} " == *" PyQt6 "* ]] && pkgs+=(python3-pyqt6)
                [[ " ${missing_py[*]} " == *" evdev "* ]] && pkgs+=(python3-evdev) ;;
        dnf)    [[ " ${missing_py[*]} " == *" PyQt6 "* ]] && pkgs+=(python3-qt6)
                [[ " ${missing_py[*]} " == *" evdev "* ]] && pkgs+=(python3-evdev) ;;
    esac
    if [[ -z "$PM" ]]; then
        echo "Unknown package manager. Install PyQt6 and python-evdev yourself (see README)."
    else
        # input-remapper is in the Debian/Ubuntu and Fedora repos; on Arch it comes from the AUR.
        if ! command -v input-remapper-control >/dev/null 2>&1 && [[ "$PM" != pacman ]]; then
            pkgs+=(input-remapper)
        fi
        reply="y"
        ((YES)) || read -r -p "Install with $PM: ${pkgs[*]} ? [Y/n] " reply
        if [[ "${reply:-y}" =~ ^[Yy]$ ]]; then pkg_install "${pkgs[@]}" || echo "Package install failed."; fi
    fi
    python3 -c "import PyQt6.QtWidgets, evdev" 2>/dev/null || cat <<'MSG'
Still missing PyQt6/evdev for the system python. Fallback that works everywhere:
    python3 -m venv --system-site-packages ~/.local/share/inputdeck/venv
    ~/.local/share/inputdeck/venv/bin/pip install PyQt6 evdev
and run InputDeck with  ~/.local/share/inputdeck/venv/bin/python -m inputdeck
MSG
fi

# Optional: `xprop` lets rules react to a program's window (visible / focused / minimized).
if ((DEPS)) && [[ -n "$PM" ]] && ! command -v xprop >/dev/null 2>&1; then
    case "$PM" in pacman) xp=xorg-xprop ;; apt) xp=x11-utils ;; dnf) xp=xorg-x11-utils ;; esac
    reply="y"
    ((YES)) || read -r -p "Optional: install $xp so rules can react to minimized/hidden windows? [Y/n] " reply
    if [[ "${reply:-y}" =~ ^[Yy]$ ]]; then pkg_install "$xp" || echo "Could not install $xp (window rules will act like 'running')."; fi
fi

# ---------------------------------------------------------------- install files
mkdir -p "$APP_DIR" "$(dirname "$BIN")" "$(dirname "$DESKTOP")"
rm -rf "$APP_DIR/inputdeck"
cp -r "$SRC/inputdeck" "$APP_DIR/"
find "$APP_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -f "$ICON_ROOT/scalable/apps/inputdeck.svg"                 # from an earlier version
for f in "$SRC"/inputdeck/data/icons/inputdeck-*.png; do
    size="${f##*-}"; size="${size%.png}"
    mkdir -p "$ICON_ROOT/${size}x${size}/apps"
    cp "$f" "$ICON_ROOT/${size}x${size}/apps/inputdeck.png"
done

cat > "$BIN" <<LAUNCH
#!/bin/sh
PYTHONPATH="$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m inputdeck "\$@"
LAUNCH
chmod +x "$BIN"

cat > "$DESKTOP" <<DESK
[Desktop Entry]
Type=Application
Name=InputDeck
Comment=Hotkeys, macro recording and per-app presets for input-remapper
Exec=$BIN
Icon=inputdeck
StartupWMClass=InputDeck
Categories=Settings;Utility;
Terminal=false
DESK

command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f "$ICON_ROOT" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATA/applications" 2>/dev/null || true

if ((SERVICE)); then
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<SVC
[Unit]
Description=InputDeck auto-switcher (headless)
After=graphical-session.target

[Service]
ExecStart=$BIN --daemon
Restart=on-failure

[Install]
WantedBy=default.target
SVC
    systemctl --user daemon-reload
    systemctl --user enable --now inputdeck.service
    echo "Service enabled. Untick 'Auto-switch' in the GUI if you let the service do the switching."
fi

# ---------------------------------------------------------------- things only you can fix
echo
echo "Installed. Run 'inputdeck' (needs ~/.local/bin in PATH) or open it from your app menu."
notes=()
command -v input-remapper-control >/dev/null 2>&1 \
    || notes+=("input-remapper isn't installed: fine, InputDeck's built-in engine will be used (see README); run ./install.sh --setup-access once for it.")
id -nG "${USER:-$(id -un)}" 2>/dev/null | tr ' ' '\n' | grep -qx input \
    || notes+=("You are not in the 'input' group (needed to record keys): sudo usermod -aG input \$USER, then log out and in.")
if command -v input-remapper-control >/dev/null 2>&1; then
    systemctl is-active --quiet input-remapper 2>/dev/null \
        || notes+=("The input-remapper daemon isn't running: sudo systemctl enable --now input-remapper")
fi
if ((${#notes[@]})); then
    echo "Still to do:"; printf '  - %s\n' "${notes[@]}"
fi
echo "'inputdeck --check' shows the full checklist."
