# Flowtrack

<p align="center">
  <img src="https://img.shields.io/github/license/sbartaula/Flowtrack?color=00d9ff" alt="MIT License">
  <img src="https://img.shields.io/github/stars/sbartaula/Flowtrack?style=flat&color=ffbe0b" alt="Stars">
  <img src="https://img.shields.io/github/issues/sbartaula/Flowtrack?color=ff006e" alt="Issues">
  <img src="https://img.shields.io/github/actions/workflow/status/sbartaula/Flowtrack/ci.yml?label=CI&color=00ff88" alt="CI">
  <img src="https://img.shields.io/badge/python-3.10%2B-8338ec" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/platform-Linux%20X11%20%7C%20macOS%20%7C%20Windows-00d9ff" alt="Platform">
  <img src="https://img.shields.io/badge/AI-Ollama%20%7C%20OpenAI%20%7C%20Anthropic%20%7C%20Gemini-ff006e" alt="AI Providers">
</p>

<p align="center">
  <strong>Local-first productivity tracker with an AI-powered browser dashboard.</strong><br>
  Activity logs and screenshots stay on your machine unless you explicitly use a cloud AI provider or backup target.
</p>

---

## What it does

Flowtrack:

- **Tracks active windows** — app name, window title, timestamps, and context switches
- **Captures screenshots** — grayscale JPEG, compressed, auto-cleaned after 48 hours, with a 3 GB hard cap
- **Serves a browser dashboard** at `http://127.0.0.1:7070`
- **Runs optional AI analysis** with Ollama, OpenAI, Anthropic, or Gemini
- **Provides AI chat** about your local focus data
- **Exports logs** by browser download, an unlisted GitHub secret Gist, or webhook

The dashboard server binds to `127.0.0.1` only. Ubuntu/Debian X11 can run it as a systemd user service; macOS and Windows currently use manual terminal processes.

---

## Quick start: Ubuntu/Debian X11

The automated installer supports:

- Ubuntu or Debian with an active **X11/Xorg** desktop session
- systemd user services
- Python 3.10 or newer
- `apt-get` and internet access for required packages

Wayland is not supported: the current Linux window and screenshot tools depend on X11. On Ubuntu, log out and select **Ubuntu on Xorg** before installing.

```bash
git clone https://github.com/sbartaula/Flowtrack.git
cd Flowtrack
bash install.sh
```

After installation:

```bash
flowtrack          # opens the private authenticated dashboard
```

The installer enables `focusaudit.service` and `flowtrack-dashboard.service` for the current user. If `~/.local/bin` was not already on `PATH`, follow the warning printed by the installer and open a new terminal.

The systemd installation always uses `~/.focusaudit`; it does not support a custom `FLOWTRACK_HOME`.

---

## Platform support

| Platform | Tracker and screenshots | Dashboard | Lifecycle |
|----------|-------------------------|-----------|-----------|
| Ubuntu/Debian X11 | Supported | Supported | systemd installer and dashboard controls |
| Linux Wayland | Not supported | Manual dashboard can run | No tracker support |
| macOS | Manual; permissions required | Manual | Terminal only |
| Windows | Manual | Manual | Terminal only |

GitHub-hosted CI verifies Python behavior on Ubuntu, macOS, and Windows using mocked OS integrations. Active-window and screenshot capture still require a manual smoke test in a logged-in graphical session.

### macOS setup

Install Python 3.10 or newer, then:

```bash
git clone https://github.com/sbartaula/Flowtrack.git
cd Flowtrack
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

macOS must authorize the terminal application running Python:

1. Open **System Settings → Privacy & Security**.
2. Grant **Accessibility** access so System Events can read the frontmost window.
3. Grant **Screen & System Audio Recording** access for screenshots.
4. Approve any **Automation** prompt for System Events, then restart the terminal processes.

Run the tracker and dashboard in separate terminals:

```bash
# Terminal 1
.venv/bin/python tracker.py

# Terminal 2
.venv/bin/python dashboard.py
```

Use the authenticated browser tab that `dashboard.py` opens. If the tab does not open, run `open ~/flowtrack-dashboard-launch.html`. The raw localhost URL intentionally returns `401` without the private token. Service-control and autostart buttons are Linux-only. Ollama is optional; install it separately and run `ollama pull llama3` if you want local AI analysis.

### Windows setup (PowerShell)

Install 64-bit Python 3.10 or newer, then:

```powershell
git clone https://github.com/sbartaula/Flowtrack.git
cd Flowtrack
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the Python launcher `py` is unavailable, use `python -m venv .venv` instead. Run the tracker and dashboard in separate PowerShell windows; activation is not required:

```powershell
# Terminal 1
.\.venv\Scripts\python.exe tracker.py

# Terminal 2
.\.venv\Scripts\python.exe dashboard.py
```

Use the authenticated browser tab that `dashboard.py` opens. If the tab does not open, run `Start-Process "$HOME\flowtrack-dashboard-launch.html"`. The raw localhost URL intentionally returns `401` without the private token. Service-control and autostart buttons are Linux-only. Ollama is optional and is available from [ollama.com/download/windows](https://ollama.com/download/windows).

### Custom data directory for manual runs

Direct Python runs can override the default data directory with `FLOWTRACK_HOME`. Set the same value for the tracker, dashboard, and analyzer.

```bash
FLOWTRACK_HOME=/path/to/flowtrack-data .venv/bin/python tracker.py
```

```powershell
$env:FLOWTRACK_HOME = "$env:TEMP\flowtrack-data"
.\.venv\Scripts\python.exe tracker.py
```

This override is intended for manual runs and test isolation. The Linux systemd installer deliberately uses `~/.focusaudit`.

### Excluding sensitive windows

`FLOWTRACK_EXCLUDE` accepts comma-, semicolon-, or newline-separated, case-insensitive substrings. If either the application name or window title matches, that window is neither logged nor screenshotted.

For a manual run:

```bash
FLOWTRACK_EXCLUDE="1Password,bank" .venv/bin/python tracker.py
```

```powershell
$env:FLOWTRACK_EXCLUDE = "1Password,bank"
.\.venv\Scripts\python.exe tracker.py
```

For a systemd installation, create a user-unit override with `systemctl --user edit focusaudit.service`:

```ini
[Service]
Environment="FLOWTRACK_EXCLUDE=1Password,bank"
```

Then apply it with `systemctl --user daemon-reload && systemctl --user restart focusaudit.service`. Substring exclusions are a safety aid, not a guarantee; verify them with non-sensitive test windows first.

---

## Data stored

By default, data lives in `~/.focusaudit/`:

| Path | Content |
|------|---------|
| `logs/YYYY-MM-DD.jsonl` | Activity events; not automatically deleted |
| `screenshots/*.jpg` / `*.png` | Compressed screenshots; automatically cleaned |
| `reports/analysis_*.txt` | Analysis outputs |
| `dashboard-token` | Private random token used to authenticate the local dashboard |
| `tracker.log` | Tracker runtime log |
| `service.log` / `dashboard.log` | systemd installation logs on Linux |

The authenticated browser launcher is `~/flowtrack-dashboard-launch.html` with mode `0600`. It is intentionally outside the hidden data directory so confined Ubuntu browsers can open it.

Screenshots older than 48 hours are deleted, and the screenshot directory has a 3 GB cap. JSONL activity logs are retained until you remove them.

---

## AI providers

Ollama is the default local option; cloud providers require an API key and make external requests when selected.

| Provider | Key required | Default model |
|----------|--------------|---------------|
| Ollama | No | `llama3` |
| OpenAI | Yes | `gpt-4o-mini` |
| Anthropic | Yes | `claude-haiku-4-5-20251001` |
| Gemini | Yes | `gemini-3.6-flash` |

Install Ollama from [ollama.com](https://ollama.com), then run `ollama pull llama3`.

---

## Privacy and security

- The dashboard server listens only on `127.0.0.1`.
- Dashboard data and controls require a random token stored in the private `~/.focusaudit/dashboard-token` file. The `flowtrack` command and manual dashboard launch use a private `0600` launcher file; requests carry the token without placing it in a cross-port browser cookie or browser-launch command line.
- There is no telemetry or analytics.
- The local dashboard does not load third-party font assets. The separate public landing and demo HTML pages do use Google Fonts when served online.
- Cloud AI and backup requests happen only after you select and invoke those features.
- API keys are accepted per request and are not deliberately persisted by Flowtrack.
- Screenshot filenames are validated before the dashboard serves them.
- Linux service actions are restricted to `start`, `stop`, `restart`, `enable`, and `disable`.
- Window titles and screenshots can contain sensitive information. Configure `FLOWTRACK_EXCLUDE` for known sensitive apps and still stop the tracker when recording must be impossible.

---

## CLI usage

For an installed Ubuntu/Debian service:

```bash
tail -f ~/.focusaudit/logs/$(date +%Y-%m-%d).jsonl
flowtrack-analyze --no-ai
OPENAI_API_KEY=YOUR_KEY flowtrack-analyze --provider openai --model gpt-4o-mini
```

For a manual repository checkout on any platform, run `analyze.py` with the same virtual-environment Python used for the dashboard:

```bash
.venv/bin/python analyze.py --no-ai
```

```powershell
.\.venv\Scripts\python.exe analyze.py --no-ai
```

Prefer provider environment variables such as `OPENAI_API_KEY` over placing a real key directly in shell history.

---

## Cloud backup

In the dashboard Backup section:

1. Pick **Today**, **All time**, or a custom date range.
2. Click **Download** for a local JSONL download.
3. Optionally select **GitHub secret Gist** or **Webhook** and upload explicitly.

[GitHub secret Gists](https://docs.github.com/en/get-started/writing-on-github/editing-and-sharing-content-with-gists/creating-gists) are unlisted, not private: anyone who obtains the URL can read the uploaded activity logs. Prefer a local download for sensitive data, or use a backup destination with access controls you trust.

---

## Troubleshooting

Ubuntu/Debian X11:

```bash
echo "$XDG_SESSION_TYPE"       # must print x11
echo "$DISPLAY"                # must not be empty
systemctl --user status focusaudit.service flowtrack-dashboard.service
journalctl --user -u focusaudit.service -n 50
journalctl --user -u flowtrack-dashboard.service -n 50
flowtrack                         # waits for readiness and opens an authenticated dashboard
```

On macOS, missing window titles normally indicate Accessibility permission is absent; missing screenshots normally indicate Screen Recording permission is absent. Restart the terminal after changing either permission.

On Windows and macOS, dashboard service controls intentionally report unsupported. Start and stop the tracker with its terminal process.

---

## Testing

The portable test suite and syntax checks are:

```bash
python -m unittest discover -s tests -v
python -m py_compile dashboard.py tracker.py analyze.py
node --check script.js
```

Ubuntu/Debian contributors should additionally run:

```bash
bash -n install.sh
shellcheck install.sh
systemd-analyze verify focusaudit.service flowtrack-dashboard.service
python -m json.tool vercel.json >/dev/null
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the manual GUI checklist.

---

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first.

- Bugs → [GitHub Issues](https://github.com/sbartaula/Flowtrack/issues) with the bug report template
- Features → [GitHub Issues](https://github.com/sbartaula/Flowtrack/issues) with the feature request template
- Code → fork, branch, and open a PR targeting `master`

All PRs are reviewed and merged by [@saroj479](https://github.com/saroj479).

---

## Roadmap

- [ ] Native macOS and Windows background-service installers
- [ ] Wayland portal integration
- [ ] Google Drive backup
- [ ] Vision AI for screenshot analysis
- [ ] Incognito window detection
- [ ] CSV export

---

## License

[MIT](LICENSE) — Copyright (c) 2026 saroj479
