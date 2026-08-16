#!/usr/bin/env bash
# Flowtrack installer for Ubuntu/Debian desktop sessions running X11.
# macOS, Windows, Wayland, and non-systemd Linux installations are manual;
# see README.md for the supported commands and limitations.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The systemd templates intentionally use %h/.focusaudit too. FLOWTRACK_HOME is
# supported by direct/manual Python runs, but not by this systemd installer.
FOCUSAUDIT_HOME="$HOME/.focusaudit"
VENV_DIR="$FOCUSAUDIT_HOME/venv"
SERVICE_DIR="$HOME/.config/systemd/user"
LOCAL_BIN_DIR="$HOME/.local/bin"
SERVICE_NAME="focusaudit.service"
DASH_SERVICE_NAME="flowtrack-dashboard.service"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { printf '%b\n' "${GREEN}[OK]${NC} $*"; }
warn()  { printf '%b\n' "${YELLOW}[WARN]${NC} $*"; }
error() { printf '%b\n' "${RED}[ERROR]${NC} $*" >&2; exit 1; }

require_source_file() {
    [[ -f "$SCRIPT_DIR/$1" ]] || error "Required source file is missing: $SCRIPT_DIR/$1"
}

printf '\n%s\n\n' 'Flowtrack Installer (Ubuntu/Debian X11)'

# ── 1. Supported platform and session ─────────────────────────────────────────
[[ "$(uname -s)" == "Linux" ]] || error \
    "This installer supports Ubuntu/Debian Linux only. Use the manual macOS or Windows setup in README.md."

[[ -r /etc/os-release ]] || error "Cannot identify this Linux distribution (/etc/os-release is missing)."
# shellcheck disable=SC1091
. /etc/os-release
distro_id="${ID:-}"
distro_like="${ID_LIKE:-}"
case " $distro_id $distro_like " in
    *" ubuntu "*|*" debian "*) ;;
    *) error "Unsupported Linux distribution '$distro_id'. This installer requires Ubuntu or Debian." ;;
esac

if [[ "${XDG_SESSION_TYPE:-}" == "wayland" || -n "${WAYLAND_DISPLAY:-}" ]]; then
    error "Wayland capture is not supported. Log out, choose an X11/Xorg session, and run the installer again."
fi
if [[ -n "${XDG_SESSION_TYPE:-}" && "${XDG_SESSION_TYPE}" != "x11" ]]; then
    error "Unsupported graphical session '${XDG_SESSION_TYPE}'. Flowtrack currently requires X11/Xorg."
fi
[[ -n "${DISPLAY:-}" ]] || error \
    "DISPLAY is not set. Run this installer from the logged-in X11/Xorg desktop session."

if [[ -n "${FLOWTRACK_HOME:-}" && "${FLOWTRACK_HOME}" != "$FOCUSAUDIT_HOME" ]]; then
    warn "FLOWTRACK_HOME is ignored by the systemd installer; services use $FOCUSAUDIT_HOME."
fi

command -v apt-get >/dev/null 2>&1 || error "apt-get is required by this Ubuntu/Debian installer."
command -v systemctl >/dev/null 2>&1 || error "systemd is required. Use the manual setup in README.md on other init systems."
systemctl --user show-environment >/dev/null 2>&1 || error \
    "The systemd user manager is unavailable. Run this installer from your logged-in desktop session."

for source_file in \
    tracker.py analyze.py dashboard.py requirements.txt \
    focusaudit.service flowtrack-dashboard.service
do
    require_source_file "$source_file"
done

# ── 2. System packages ────────────────────────────────────────────────────────
info "Checking system dependencies"
missing_packages=()

if ! command -v python3 >/dev/null 2>&1; then
    missing_packages+=(python3 python3-venv)
elif ! python3 -m venv --help >/dev/null 2>&1; then
    missing_packages+=(python3-venv)
fi
command -v xdotool >/dev/null 2>&1 || missing_packages+=(xdotool)
command -v scrot >/dev/null 2>&1 || missing_packages+=(scrot)
command -v xdg-open >/dev/null 2>&1 || missing_packages+=(xdg-utils)

if (( ${#missing_packages[@]} )); then
    if (( EUID == 0 )); then
        apt_command=(apt-get)
    else
        command -v sudo >/dev/null 2>&1 || error \
            "Missing packages require administrator access, but sudo is not installed: ${missing_packages[*]}"
        apt_command=(sudo apt-get)
    fi
    warn "Installing missing packages: ${missing_packages[*]}"
    "${apt_command[@]}" update
    "${apt_command[@]}" install -y "${missing_packages[@]}"
else
    info "All system packages are present"
fi

command -v python3 >/dev/null 2>&1 || error "python3 is still unavailable after package installation."
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || error \
    "Flowtrack requires Python 3.10 or newer; found $(python3 --version 2>&1)."
python3 -m venv --help >/dev/null 2>&1 || error "python3-venv is unavailable after package installation."

# ── 3. Application files and Python environment ───────────────────────────────
info "Creating $FOCUSAUDIT_HOME"
install -d -m 0700 \
    "$FOCUSAUDIT_HOME" \
    "$FOCUSAUDIT_HOME/screenshots" \
    "$FOCUSAUDIT_HOME/logs" \
    "$FOCUSAUDIT_HOME/reports"
find "$FOCUSAUDIT_HOME/screenshots" "$FOCUSAUDIT_HOME/logs" "$FOCUSAUDIT_HOME/reports" \
    -type f -exec chmod 0600 {} +

info "Creating Python virtual environment at $VENV_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$SCRIPT_DIR/requirements.txt"
"$VENV_DIR/bin/python" -m pip check
info "Python dependencies installed from requirements.txt"

info "Installing Flowtrack scripts"
for script in tracker.py analyze.py dashboard.py; do
    install -m 0755 "$SCRIPT_DIR/$script" "$FOCUSAUDIT_HOME/$script"
done

# ── 4. Commands and systemd user services ─────────────────────────────────────
mkdir -p "$LOCAL_BIN_DIR" "$SERVICE_DIR"

ANALYZE_WRAPPER="$LOCAL_BIN_DIR/flowtrack-analyze"
cat > "$ANALYZE_WRAPPER" << WRAPPER_EOF
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python" "$FOCUSAUDIT_HOME/analyze.py" "\$@"
WRAPPER_EOF
chmod 0755 "$ANALYZE_WRAPPER"

BROWSER_WRAPPER="$LOCAL_BIN_DIR/flowtrack"
cat > "$BROWSER_WRAPPER" << BROWSER_EOF
#!/usr/bin/env bash
systemctl --user is-active $DASH_SERVICE_NAME >/dev/null 2>&1 || \
    systemctl --user start $DASH_SERVICE_NAME >/dev/null 2>&1
ready=false
for _ in {1..80}; do
    if [[ -s "$HOME/flowtrack-dashboard-launch.html" ]] && \
        "$VENV_DIR/bin/python" -c 'import hashlib,hmac,json,pathlib,secrets,urllib.request; token=(pathlib.Path.home()/".focusaudit/dashboard-token").read_text().strip(); nonce=secrets.token_urlsafe(24); data=json.load(urllib.request.urlopen("http://127.0.0.1:7070/api/launcher-proof?nonce="+nonce,timeout=0.3)); raise SystemExit(0 if hmac.compare_digest(data.get("proof",""),hmac.new(token.encode(),nonce.encode(),hashlib.sha256).hexdigest()) else 1)' 2>/dev/null
    then
        ready=true
        break
    fi
    sleep 0.1
done
[[ "\$ready" == true ]] || { printf '%s\n' 'Flowtrack dashboard did not become ready or failed identity verification.' >&2; exit 1; }
exec xdg-open "$HOME/flowtrack-dashboard-launch.html"
BROWSER_EOF
chmod 0755 "$BROWSER_WRAPPER"

case ":$PATH:" in
    *":$LOCAL_BIN_DIR:"*) ;;
    *)
        warn "$LOCAL_BIN_DIR is not currently on PATH."
        warn "Add 'export PATH=\"\$HOME/.local/bin:\$PATH\"' to your shell profile, then open a new terminal."
        ;;
esac

info "Installing tracked systemd user-service templates"
install -m 0644 "$SCRIPT_DIR/$SERVICE_NAME" "$SERVICE_DIR/$SERVICE_NAME"
install -m 0644 "$SCRIPT_DIR/$DASH_SERVICE_NAME" "$SERVICE_DIR/$DASH_SERVICE_NAME"

# A user manager normally inherits its desktop environment at login. Refresh
# the variables available in the terminal used for installation without ever
# guessing a DISPLAY number.
session_variables=()
for variable_name in DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR XDG_SESSION_TYPE; do
    [[ -n "${!variable_name:-}" ]] && session_variables+=("$variable_name")
done
if (( ${#session_variables[@]} )); then
    systemctl --user import-environment "${session_variables[@]}"
fi

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME" "$DASH_SERVICE_NAME"

if systemctl --user restart "$SERVICE_NAME"; then
    info "Tracker service restarted with the installed code"
else
    warn "Tracker did not start. Check: journalctl --user -u $SERVICE_NAME -n 50"
fi

if systemctl --user restart "$DASH_SERVICE_NAME"; then
    info "Dashboard service restarted with the installed code"
    sleep 1
    "$BROWSER_WRAPPER" >/dev/null 2>&1 || true
else
    warn "Dashboard did not start. Check: journalctl --user -u $DASH_SERVICE_NAME -n 50"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
printf '\n%s\n' 'Installation complete.'
printf 'Dashboard: authenticated localhost service on 127.0.0.1:7070\n'
printf 'Open it:   flowtrack\n'
printf 'Analyze:   flowtrack-analyze --no-ai\n'
printf "Live log:  tail -f %s/logs/\$(date +%%Y-%%m-%%d).jsonl\n" "$FOCUSAUDIT_HOME"
printf 'Status:    systemctl --user status %s %s\n\n' "$SERVICE_NAME" "$DASH_SERVICE_NAME"
