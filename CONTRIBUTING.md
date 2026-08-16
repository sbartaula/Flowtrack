# Contributing to Flowtrack

Thank you for contributing. Flowtrack is a local-first productivity tracker with a standard-library web server plus two required screenshot dependencies, `mss` and Pillow.

## Platform boundaries

- Ubuntu/Debian **X11** is the primary platform and has a systemd installer.
- Linux Wayland tracking and screenshot capture are not currently supported.
- macOS uses AppleScript and requires Accessibility plus Screen Recording permission.
- Windows uses native Windows APIs/PowerShell integration.
- macOS and Windows run the tracker and dashboard manually; dashboard service controls are Linux-only.

CI runs portable tests on Ubuntu, macOS, and Windows. GUI capture cannot be validated on headless GitHub runners, so platform-related changes also need a manual graphical-session test.

## How to contribute

1. Fork the repository.
2. Clone your fork:

   ```bash
   git clone https://github.com/YOUR_USERNAME/Flowtrack.git
   cd Flowtrack
   ```

3. Create a focused branch:

   ```bash
   git switch -c fix/your-change-name
   ```

4. Make and test the change.
5. Push the branch and open a Pull Request targeting `master`.

Only the maintainer, [@saroj479](https://github.com/saroj479), merges changes into the main repository.

## Local setup

Python 3.10 or newer is required.

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Using the virtual-environment interpreter directly avoids PowerShell activation-policy issues.

Run the dashboard without systemd:

```bash
python dashboard.py
```

On Windows, use `.\.venv\Scripts\python.exe dashboard.py`. Use the authenticated browser tab it opens, or open the private launcher path printed in the terminal. The raw localhost URL returns `401` without its token by design.

### Isolated test data

Manual runs can set `FLOWTRACK_HOME` so development data does not mix with real activity data:

```bash
FLOWTRACK_HOME=/tmp/flowtrack-dev python tracker.py
```

```powershell
$env:FLOWTRACK_HOME = Join-Path $env:TEMP "flowtrack-dev"
.\.venv\Scripts\python.exe tracker.py
```

Use the same value for the tracker, dashboard, and analyzer. `install.sh` and the systemd units intentionally ignore custom locations and always use `~/.focusaudit`.

## Automated checks

Run the portable suite on every platform:

```bash
python -m pip check
python -m unittest discover -s tests -v
python -m py_compile dashboard.py tracker.py analyze.py
```

If Node.js is installed, validate the landing-page JavaScript:

```bash
node --check script.js
```

Ubuntu/Debian contributors should also validate the installer and tracked unit templates:

```bash
bash -n install.sh
shellcheck install.sh
systemd-analyze verify focusaudit.service flowtrack-dashboard.service
python -m json.tool vercel.json >/dev/null
```

## Manual checklist

Use a temporary or non-sensitive desktop session. The tracker records window titles and screenshots.

- The tracker records a window-title change and creates a readable screenshot.
- The private launcher opens the dashboard, its status card loads, and the authenticated `/api/status` request returns JSON (the raw URL returns `401` by design).
- Analysis without AI completes and writes a report.
- Backup download returns a JSONL file.
- Ollama or a cloud-provider test is only required when changing that integration.
- No exception appears in the terminal used for a manual run.
- For a Linux systemd installation, no exception appears in `tracker.log`, `service.log`, or `dashboard.log`.

For macOS, test after granting Accessibility and Screen Recording permissions. For Windows, verify both the foreground title and application name. For Linux, verify `echo "$XDG_SESSION_TYPE"` reports `x11`.

## Code and dependency style

- Follow the style of the file being changed; do not reformat unrelated code.
- Prefer the Python standard library, but do not describe mss or Pillow as optional when screenshot support depends on them.
- Resolve application data through `FLOWTRACK_HOME` with a fallback to `~/.focusaudit`; do not introduce another hard-coded data directory.
- Preserve the case-insensitive `FLOWTRACK_EXCLUDE` behavior for window-title and application-name substrings.
- Never log, persist, or commit API keys or personal activity data.
- Keep the server bound to `127.0.0.1` unless a separately reviewed security design says otherwise.
- Validate filenames and subprocess arguments at trust boundaries.
- Explain and justify any new runtime dependency in the PR.

## Pull request rules

- Keep one concern per PR.
- Link the issue the PR addresses.
- Fill in the PR template, including exact test commands and the OS used.
- Do not commit screenshots, JSONL logs, API keys, local virtual environments, or shell-profile files.

## Reporting bugs

Use [GitHub Issues](https://github.com/sbartaula/Flowtrack/issues) and include:

- OS, desktop session (`x11` or `wayland` on Linux), and Python version
- Flowtrack commit
- whether the run was manual or systemd-managed
- relevant terminal or service-log output with sensitive window titles removed

## License

By contributing, you agree that your changes are licensed under the repository's [MIT License](LICENSE).
