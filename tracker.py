#!/usr/bin/env python3
"""
Flowtrack - High-Frequency Window Activity Tracker (Modules 1 & 2)

Polls the active window title every second. Logs an entry on every title
change OR every 30 seconds if the title is unchanged. Captures a
compressed grayscale screenshot alongside each log entry.
Purges screenshots older than 48 hours on startup.

Storage layout:
    ~/.focusaudit/
        logs/          YYYY-MM-DD.jsonl  (one line per event, kept forever)
        screenshots/   YYYY-MM-DD_HH-MM-SS.jpg
        service.log    systemd / runtime log
        tracker.log    Python logger output
"""

import os
import platform
import json
import ctypes
import ntpath
import re
import time
import signal
import logging
import datetime
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

# ── Optional heavy dependencies ─────────────────────────────────────────────────
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import mss  # type: ignore
    HAS_MSS = True
except ImportError:
    HAS_MSS = False

# ── Configuration ────────────────────────────────────────────────────────────────
BASE_DIR        = Path(os.environ.get("FLOWTRACK_HOME", Path.home() / ".focusaudit")).expanduser()
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
LOG_DIR         = BASE_DIR / "logs"
SYSTEM_LOG      = BASE_DIR / "tracker.log"

LOG_INTERVAL    = 30     # seconds between "still-here" interval log entries
POLL_INTERVAL   = 1      # seconds between window-title polls
PURGE_HOURS     = 48     # screenshots older than this are deleted
IMG_WIDTH       = 1000   # resize screenshots to this width (px)
IMG_QUALITY     = 60     # JPEG quality (0-100)
SCREENSHOT_MAX_GB = 3    # hard cap for screenshot storage

# Comma/semicolon/newline-separated, case-insensitive substrings. Matching
# windows are neither logged nor screenshotted (for example: "1Password,bank").
EXCLUDE_PATTERNS = tuple(
    value.strip().casefold()
    for value in re.split(r"[,;\n]", os.environ.get("FLOWTRACK_EXCLUDE", ""))
    if value.strip()
)

# ── Directory setup ──────────────────────────────────────────────────────────────
def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            path.chmod(0o700)
        except OSError:
            pass


def _ensure_private_file(path: Path) -> None:
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError:
            pass


for _data_dir in (BASE_DIR, SCREENSHOTS_DIR, LOG_DIR):
    _ensure_private_dir(_data_dir)

# Create the log securely before logging opens it. O_APPEND preserves prior
# diagnostics while the explicit mode avoids umask-dependent privacy leaks.
_log_fd = os.open(SYSTEM_LOG, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
os.close(_log_fd)
_ensure_private_file(SYSTEM_LOG)

logging.basicConfig(
    filename=str(SYSTEM_LOG),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════════
#  Window title detection  (native APIs; Linux desktop capture requires X11)
# ════════════════════════════════════════════════════════════════════════════════

def _run(cmd: list, timeout: int = 2) -> Optional[str]:
    """Run a subprocess, return stdout or None on any failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _app_name_from_pid(pid: str) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return "unknown"


def _get_windows_foreground_ctypes() -> Optional[Tuple[str, str]]:
    """Read the foreground window using Win32 directly, without a subprocess."""
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        window = user32.GetForegroundWindow()
        if not window:
            return None

        title_length = user32.GetWindowTextLengthW(window)
        if title_length <= 0:
            return None
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        if user32.GetWindowTextW(window, title_buffer, len(title_buffer)) <= 0:
            return None
        title = title_buffer.value.strip()
        if not title:
            return None

        process_id = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(window, ctypes.byref(process_id)):
            return None
        if not process_id.value:
            return None

        # PROCESS_QUERY_LIMITED_INFORMATION is enough for
        # QueryFullProcessImageNameW and avoids requesting broader privileges.
        process = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not process:
            return None
        try:
            executable_buffer = ctypes.create_unicode_buffer(32768)
            executable_length = wintypes.DWORD(len(executable_buffer))
            if not kernel32.QueryFullProcessImageNameW(
                process,
                0,
                executable_buffer,
                ctypes.byref(executable_length),
            ):
                return None
            executable = executable_buffer.value.strip()
        finally:
            kernel32.CloseHandle(process)

        app = ntpath.splitext(ntpath.basename(executable))[0].strip()
        return (title, app) if app else None
    except Exception as exc:
        # Some protected/system windows deny process access. The caller can use
        # the slower PowerShell compatibility path for those cases.
        log.debug("Win32 foreground query failed: %s", exc)
        return None


def _get_windows_foreground_powershell() -> Optional[Tuple[str, str]]:
    """Compatibility fallback when direct Win32 access is unavailable."""
    ps_script = (
        "$sig='[DllImport(\"user32.dll\")]public static extern IntPtr GetForegroundWindow();"
        "[DllImport(\"user32.dll\",CharSet=CharSet.Unicode,SetLastError=true)]public static extern int GetWindowText(IntPtr hWnd,System.Text.StringBuilder text,int count);"
        "[DllImport(\"user32.dll\")]public static extern uint GetWindowThreadProcessId(IntPtr hWnd,[ref] uint processId);';"
        "Add-Type -MemberDefinition $sig -Name Win32 -Namespace Native -ErrorAction SilentlyContinue | Out-Null;"
        "$h=[Native.Win32]::GetForegroundWindow();"
        "$sb=New-Object System.Text.StringBuilder 32768;"
        "[void][Native.Win32]::GetWindowText($h,$sb,$sb.Capacity);"
        "$processId=[uint32]0; [void][Native.Win32]::GetWindowThreadProcessId($h,[ref]$processId);"
        "$p=(Get-Process -Id $processId -ErrorAction SilentlyContinue);"
        "$appName=if($p){$p.ProcessName}else{'unknown'};"
        "[Console]::Out.Write($sb.ToString()+[char]31+$appName);"
    )
    raw = _run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            ps_script,
        ],
        timeout=3,
    )
    if raw:
        title, separator, app = raw.partition("\x1f")
        if separator and title.strip():
            return title.strip(), app.strip() or "unknown"
    return None


def get_active_window_info() -> Tuple[str, str]:
    """Return (window_title, app_name) for the currently focused window."""

    system_name = platform.system()

    # ── Windows native (Win32 ctypes; PowerShell compatibility fallback) ───
    if system_name == "Windows":
        return (
            _get_windows_foreground_ctypes()
            or _get_windows_foreground_powershell()
            or ("Unknown", "unknown")
        )

    # ── macOS native (AppleScript) ──────────────────────────────────────────
    if system_name == "Darwin":
        script = (
            'tell application "System Events"\n'
            "set frontProcess to first application process whose frontmost is true\n"
            "set appName to name of frontProcess\n"
            "try\n"
            "set windowTitle to name of front window of frontProcess\n"
            "on error\n"
            "set windowTitle to appName\n"
            "end try\n"
            "return windowTitle & (character id 31) & appName\n"
            "end tell"
        )
        raw = _run(["osascript", "-e", script], timeout=3)
        if raw:
            title, separator, app = raw.partition("\x1f")
            if separator and title.strip():
                return title.strip(), app.strip() or "unknown"
        return "Unknown", "unknown"

    # The remaining probes are Linux/X11 or GNOME-specific. Running them on
    # macOS/Windows masks native permission/configuration errors with noise.
    if system_name != "Linux":
        return "Unknown", "unknown"

    # ── xdotool (X11) ────────────────────────────────────────────────────────
    title = _run(["xdotool", "getactivewindow", "getwindowname"])
    if title:
        app = "unknown"
        win_id = _run(["xdotool", "getactivewindow"])
        if win_id:
            pid = _run(["xdotool", "getwindowpid", win_id])
            if pid:
                app = _app_name_from_pid(pid)
        return title, app

    # ── gdbus / GNOME Shell (Wayland) ─────────────────────────────────────────
    gdbus_out = _run([
        "gdbus", "call", "--session",
        "--dest", "org.gnome.Shell",
        "--object-path", "/org/gnome/Shell",
        "--method", "org.gnome.Shell.Eval",
        "global.display.focus_window ? global.display.focus_window.get_title() : 'Unknown'",
    ], timeout=3)
    if gdbus_out and "true" in gdbus_out:
        parts = gdbus_out.split("'")
        if len(parts) >= 2:
            return parts[1].strip(), "unknown"

    # ── xprop fallback (X11 without xdotool) ─────────────────────────────────
    net_win = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    if net_win:
        win_id = net_win.split()[-1]
        if win_id and win_id != "0x0":
            wm_name = _run(["xprop", "-id", win_id, "WM_NAME"])
            if wm_name and '"' in wm_name:
                return wm_name.split('"')[1], "unknown"

    return "Unknown", "unknown"


# ════════════════════════════════════════════════════════════════════════════════
#  Screenshot capture  (mss primary → scrot fallback)
# ════════════════════════════════════════════════════════════════════════════════

def take_screenshot() -> Optional[str]:
    """
    Capture the primary monitor, resize to IMG_WIDTH, convert to grayscale,
    save as JPEG with IMG_QUALITY compression.
    Returns the filename (not full path), or None on failure.
    """
    ts       = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:23]
    filename = f"{ts}.jpg"
    dst      = SCREENSHOTS_DIR / filename

    img: Optional["Image.Image"] = None  # type: ignore[name-defined]

    # ── mss (pure Python, very low overhead) ─────────────────────────────────
    if HAS_MSS and HAS_PIL:
        try:
            import mss as _mss
            # The lowercase factory works from mss 9.x through current releases.
            with _mss.mss() as sct:
                monitor = sct.monitors[1]      # primary monitor
                raw     = sct.grab(monitor)
                img     = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        except Exception as exc:
            log.warning("mss capture failed: %s", exc)
            img = None

    # ── scrot fallback ────────────────────────────────────────────────────────
    if img is None and platform.system() == "Linux":
        tmp = SCREENSHOTS_DIR / f"{ts}_raw.png"
        ok  = _run(["scrot", str(tmp)], timeout=5)
        if ok is not None or tmp.exists():
            if HAS_PIL and tmp.exists():
                try:
                    img = Image.open(str(tmp))
                    tmp.unlink(missing_ok=True)
                except Exception as exc:
                    log.warning("PIL open after scrot failed: %s", exc)
                    tmp.unlink(missing_ok=True)
                    return None
            elif tmp.exists():
                # No PIL — keep the raw PNG, rename to match expected filename
                png_dst = dst.with_suffix(".png")
                tmp.rename(png_dst)
                _ensure_private_file(png_dst)
                return filename.replace(".jpg", ".png")
            else:
                return None

    if img is None:
        return None

    # ── Process: resize → grayscale → JPEG ───────────────────────────────────
    try:
        ratio      = IMG_WIDTH / img.width
        new_height = int(img.height * ratio)
        img        = img.resize((IMG_WIDTH, new_height), Image.LANCZOS)
        img        = img.convert("L")                           # grayscale
        img.save(str(dst), "JPEG", quality=IMG_QUALITY, optimize=True)
        _ensure_private_file(dst)
        return filename
    except Exception as exc:
        log.warning("Image processing failed: %s", exc)
        return None


# ════════════════════════════════════════════════════════════════════════════════
#  Module 2 — 48-hour purge
# ════════════════════════════════════════════════════════════════════════════════

def purge_old_screenshots() -> None:
    """Delete .jpg (and .png fallback) files older than PURGE_HOURS hours."""
    cutoff  = time.time() - PURGE_HOURS * 3600
    purged  = 0
    for f in SCREENSHOTS_DIR.glob("*"):
        if f.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                purged += 1
        except OSError as exc:
            log.warning("Could not delete %s: %s", f, exc)
    if purged:
        log.info("Purged %d screenshot(s) older than %d hours.", purged, PURGE_HOURS)


def enforce_screenshot_storage_cap(max_gb: float = SCREENSHOT_MAX_GB) -> None:
    """Keep screenshots under max_gb by deleting oldest files first."""
    cap_bytes = int(max_gb * 1024 * 1024 * 1024)
    files = [
        f for f in SCREENSHOTS_DIR.glob("*")
        if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png")
    ]
    if not files:
        return

    total = sum(f.stat().st_size for f in files)
    if total <= cap_bytes:
        return

    deleted = 0
    # Delete oldest screenshots first to preserve recent context.
    for f in sorted(files, key=lambda p: p.stat().st_mtime):
        try:
            size = f.stat().st_size
            f.unlink()
            total -= size
            deleted += 1
            if total <= cap_bytes:
                break
        except OSError as exc:
            log.warning("Could not delete %s during cap enforcement: %s", f, exc)

    log.info(
        "Storage cap enforced: deleted %d screenshot(s), now %.2f GB.",
        deleted,
        total / (1024 * 1024 * 1024),
    )


# ════════════════════════════════════════════════════════════════════════════════
#  Logging
# ════════════════════════════════════════════════════════════════════════════════

def _daily_log_path(timestamp: Optional[datetime.datetime] = None) -> Path:
    log_date = timestamp.date() if timestamp is not None else datetime.date.today()
    return LOG_DIR / f"{log_date.isoformat()}.jsonl"


def append_log_entry(
    title: str,
    app: str,
    event: str,            # "change" | "interval"
    screenshot: Optional[str],
    duration: float,       # non-overlapping seconds represented by this row
    timestamp: Optional[datetime.datetime] = None,
    schema_version: int = 2,
) -> None:
    """Append one JSON line to the segment-start day's log without truncating."""
    timestamp = timestamp or datetime.datetime.now()
    entry = {
        "schema_version": schema_version,
        "ts":         timestamp.isoformat(timespec="seconds"),
        "title":      title,
        "app":        app,
        "event":      event,
        "duration":   round(max(0.0, duration), 1),
        "screenshot": screenshot,
    }
    path = _daily_log_path(timestamp)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _ensure_private_file(path)


# ════════════════════════════════════════════════════════════════════════════════
#  Main tracking loop
# ════════════════════════════════════════════════════════════════════════════════

@dataclass
class _Segment:
    title: str
    app: str
    started_monotonic: float
    started_wall: datetime.datetime
    screenshot: Optional[str]


class SegmentRecorder:
    """Turn polling observations into non-overlapping activity segments."""

    UNKNOWN_DEBOUNCE_POLLS = 3

    def __init__(
        self,
        writer: Callable[..., None] = append_log_entry,
        screenshotter: Callable[[], Optional[str]] = take_screenshot,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime.datetime] = datetime.datetime.now,
        interval: float = LOG_INTERVAL,
        exclude_patterns: Tuple[str, ...] = EXCLUDE_PATTERNS,
        storage_capper: Callable[[], None] = enforce_screenshot_storage_cap,
        max_observation_gap: Optional[float] = None,
    ) -> None:
        self._writer = writer
        self._screenshotter = screenshotter
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._interval = interval
        self._exclude_patterns = exclude_patterns
        self._storage_capper = storage_capper
        self._max_observation_gap = max_observation_gap or max(interval * 2, 60.0)
        self._segment: Optional[_Segment] = None
        self._unknown_count = 0
        self._unknown_since: Optional[float] = None
        self._last_observed: Optional[float] = None

    @staticmethod
    def _unknown(title: str) -> bool:
        return not title.strip() or title.strip().casefold() in {"unknown", "n/a"}

    def _excluded(self, title: str, app: str) -> bool:
        searchable = f"{app}\n{title}".casefold()
        return any(pattern in searchable for pattern in self._exclude_patterns)

    def _start(
        self,
        title: str,
        app: str,
        now: float,
        wall_now: datetime.datetime,
    ) -> None:
        screenshot = self._screenshotter()
        try:
            self._storage_capper()
        except OSError as exc:
            log.warning("Storage cap check failed: %s", exc)
        self._segment = _Segment(title, app, now, wall_now, screenshot)

    def _finish(self, event: str, now: float) -> None:
        segment = self._segment
        if segment is None:
            return
        self._writer(
            segment.title,
            segment.app,
            event,
            segment.screenshot,
            max(0.0, now - segment.started_monotonic),
            timestamp=segment.started_wall,
            schema_version=2,
        )
        self._segment = None

    def observe(
        self,
        title: str,
        app: str,
        *,
        now: Optional[float] = None,
        wall_now: Optional[datetime.datetime] = None,
    ) -> None:
        now = self._monotonic() if now is None else now
        wall_now = self._wall_clock() if wall_now is None else wall_now

        # Suspend/resume and a wedged desktop probe must not be reported as
        # active work. Close at the last trustworthy poll and start a fresh
        # segment from the current observation.
        if self._last_observed is not None:
            gap = now - self._last_observed
            if gap < 0 or gap > self._max_observation_gap:
                cutoff = self._unknown_since
                if cutoff is None:
                    cutoff = self._last_observed + min(POLL_INTERVAL, max(0.0, gap))
                self._finish("interval", cutoff)
                self._unknown_count = 0
                self._unknown_since = None
        self._last_observed = now

        # A single failed desktop probe must not manufacture Unknown→window
        # switches. A sustained outage closes the prior segment at the first
        # failed poll so downtime is not credited as active work.
        if self._unknown(title):
            self._unknown_count += 1
            if self._unknown_since is None:
                self._unknown_since = now
            if self._unknown_count == self.UNKNOWN_DEBOUNCE_POLLS:
                self._finish("interval", self._unknown_since)
            return
        self._unknown_count = 0
        self._unknown_since = None

        if self._excluded(title, app):
            # Close without claiming a visible transition: the next logged
            # segment may follow an arbitrary amount of excluded activity.
            self._finish("interval", now)
            return

        if self._segment is None:
            self._start(title, app, now, wall_now)
            return

        changed = (title, app) != (self._segment.title, self._segment.app)
        interval_elapsed = now - self._segment.started_monotonic >= self._interval
        if changed or interval_elapsed:
            self._finish("change" if changed else "interval", now)
            self._start(title, app, now, wall_now)

    def flush(self, *, now: Optional[float] = None) -> None:
        """Persist the current partial segment during an orderly shutdown."""
        now = self._monotonic() if now is None else now
        if self._unknown_since is not None:
            # With no later successful probe, even a short trailing Unknown
            # run is unresolved rather than a confirmed transient failure.
            now = self._unknown_since
        elif self._last_observed is not None and now - self._last_observed > self._max_observation_gap:
            now = self._last_observed + POLL_INTERVAL
        self._finish("interval", now)


_running = False


def _handle_signal(signum, frame):   # noqa: ARG001
    global _running
    log.info("Signal %d received — shutting down.", signum)
    _running = False


def main() -> None:
    global _running
    _running = True
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)
    log.info("Flowtrack tracker starting. DISPLAY=%s", os.environ.get("DISPLAY", "unset"))

    # Module 2: purge old screenshots on every startup
    purge_old_screenshots()
    enforce_screenshot_storage_cap()

    recorder = SegmentRecorder()
    try:
        while _running:
            try:
                title, app = get_active_window_info()
                recorder.observe(title, app)
            except Exception as exc:            # never crash the daemon
                log.error("Tracker loop error: %s", exc, exc_info=True)
            time.sleep(POLL_INTERVAL)
    finally:
        try:
            recorder.flush()
        except Exception as exc:
            log.error("Could not flush final activity segment: %s", exc, exc_info=True)
        log.info("Flowtrack tracker stopped.")


if __name__ == "__main__":
    main()
