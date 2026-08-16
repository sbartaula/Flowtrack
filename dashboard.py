#!/usr/bin/env python3
"""
Flowtrack Dashboard — local web UI
Served on http://127.0.0.1:7070  (localhost only, never exposed to internet)

Start:   systemctl --user start flowtrack-dashboard
Open:    xdg-open http://127.0.0.1:7070
Manual:  python3 ~/.focusaudit/dashboard.py
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(os.environ.get("FLOWTRACK_HOME", Path.home() / ".focusaudit")).expanduser()
SCREENSHOTS_DIR = BASE_DIR / "screenshots"
LOG_DIR         = BASE_DIR / "logs"
REPORTS_DIR     = BASE_DIR / "reports"
SERVICE_NAME    = "focusaudit"
HOST            = "127.0.0.1"   # localhost only — never 0.0.0.0
PORT            = 7070
SCREENSHOT_CAP_GB = 3
MAX_REQUEST_BYTES = 1024 * 1024
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
PROVIDER_DEFAULT_MODELS = {
  "ollama": "llama3",
  "openai": "gpt-4o-mini",
  "anthropic": "claude-haiku-4-5-20251001",
  "gemini": "gemini-3.6-flash",
}
CHAT_PROVIDER_DEFAULT_MODELS = {
  **PROVIDER_DEFAULT_MODELS,
  "xai": "grok-4.3",
  "openrouter": "openai/gpt-4o-mini",
  "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
  "groq": "openai/gpt-oss-20b",
}
API_KEY_ENV_VARS = {
  "openai": "OPENAI_API_KEY",
  "anthropic": "ANTHROPIC_API_KEY",
  "gemini": "GEMINI_API_KEY",
}

OLLAMA_URL = "http://localhost:11434/api/generate"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
XAI_URL = "https://api.x.ai/v1/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TOGETHER_URL = "https://api.together.xyz/v1/chat/completions"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _ensure_private_dir(path: Path) -> None:
  path.mkdir(mode=0o700, parents=True, exist_ok=True)
  if os.name == "posix":
    try:
      path.chmod(0o700)
    except OSError:
      pass


for _data_dir in (BASE_DIR, SCREENSHOTS_DIR, LOG_DIR, REPORTS_DIR):
  _ensure_private_dir(_data_dir)


def _load_or_create_dashboard_token() -> str:
  """Return a persistent same-user token stored below the private data dir."""
  token_path = BASE_DIR / "dashboard-token"
  for _ in range(20):
    try:
      existing = token_path.read_text(encoding="ascii").strip()
      if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", existing):
        if os.name == "posix":
          token_path.chmod(0o600)
        return existing
    except (OSError, UnicodeError):
      pass

    token = secrets.token_urlsafe(32)
    try:
      fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
      # Another dashboard may be in the tiny window between creating and
      # filling the file. Wait for its complete token instead of replacing it.
      time.sleep(0.01)
      continue
    with os.fdopen(fd, "w", encoding="ascii") as fh:
      fh.write(token + "\n")
    if os.name == "posix":
      token_path.chmod(0o600)
    return token

  raise RuntimeError(f"Dashboard token file is invalid or unreadable: {token_path}")


DASHBOARD_TOKEN = _load_or_create_dashboard_token()
DASHBOARD_TOKEN_HEADER = "X-Flowtrack-Token"
DASHBOARD_LAUNCHER = Path(
  os.environ.get("FLOWTRACK_LAUNCHER", Path.home() / "flowtrack-dashboard-launch.html")
).expanduser()


def _write_dashboard_launcher() -> None:
  authenticated_url = f"http://{HOST}:{PORT}/?token={DASHBOARD_TOKEN}"
  nonce = secrets.token_urlsafe(24)
  expected_proof = hmac.new(
    DASHBOARD_TOKEN.encode("ascii"), nonce.encode("ascii"), hashlib.sha256
  ).hexdigest()
  proof_url = f"http://{HOST}:{PORT}/api/launcher-proof?nonce={nonce}"
  launcher_html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; connect-src http://{HOST}:{PORT}; style-src 'unsafe-inline'">
<title>Opening Flowtrack…</title></head>
<body><p id="status">Verifying and opening the authenticated Flowtrack dashboard…</p>
<script>
const target = {json.dumps(authenticated_url)};
const expectedProof = {json.dumps(expected_proof)};
fetch({json.dumps(proof_url)}, {{cache: 'no-store', referrerPolicy: 'no-referrer'}})
  .then(response => {{ if (!response.ok) throw new Error('not ready'); return response.json(); }})
  .then(data => {{
    if (!data || data.proof !== expectedProof) throw new Error('server identity mismatch');
    window.location.replace(target);
  }})
  .catch(() => {{
    document.getElementById('status').textContent =
      'Flowtrack is not ready or server verification failed. Run the flowtrack command again.';
  }});
</script></body></html>
"""
  fd = os.open(DASHBOARD_LAUNCHER, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.write(launcher_html)
  if os.name == "posix":
    DASHBOARD_LAUNCHER.chmod(0o600)


_write_dashboard_launcher()


def _resolve_analyze_script() -> str:
  candidates = [
    Path(__file__).with_name("analyze.py"),
    BASE_DIR / "analyze.py",
  ]
  for candidate in candidates:
    if candidate.exists():
      return str(candidate)
  return str(candidates[0])


def _resolve_python_executable() -> str:
  candidates = [
    Path(sys.executable),
    BASE_DIR / "venv" / "Scripts" / "python.exe",
    BASE_DIR / "venv" / "bin" / "python3",
  ]
  for candidate in candidates:
    if candidate and Path(candidate).exists():
      return str(candidate)
  return sys.executable or "python3"


def _local_host_and_port(value: str, expected_port: int) -> bool:
  """Return True only for an explicit loopback Host header on this server port."""
  value = value.strip().lower()
  if value.startswith("["):
    match = re.fullmatch(r"\[([^]]+)]:(\d+)", value)
    return bool(match and match.group(1) == "::1" and int(match.group(2)) == expected_port)
  match = re.fullmatch(r"([^:]+):(\d+)", value)
  return bool(match and match.group(1) in LOCAL_HOSTS and int(match.group(2)) == expected_port)


def _local_origin(value: str, expected_port: int) -> bool:
  try:
    parsed = urlparse(value)
    return (
      parsed.scheme in {"http", "https"}
      and (parsed.hostname or "").lower() in LOCAL_HOSTS
      and parsed.port == expected_port
      and not parsed.username
      and not parsed.password
    )
  except ValueError:
    return False


def _open_folder(path: Path) -> tuple[bool, str]:
  system_name = platform.system()
  opener = "explorer.exe" if system_name == "Windows" else "open" if system_name == "Darwin" else "xdg-open"
  if shutil.which(opener) is None:
    return False, f"'{opener}' is not available on this system."
  try:
    subprocess.Popen(
      [opener, str(path)],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
    )
    return True, ""
  except OSError as exc:
    return False, f"Open folder failed: {exc}"


def _open_browser(url: str) -> None:
  """Open a URL without allowing desktop integration failures to stop the server."""
  try:
    webbrowser.open(url, new=2)
  except Exception:
    pass

# ── System helpers ─────────────────────────────────────────────────────────────

def _sh(cmd: list[str], timeout: int = 5) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        ).stdout.strip()
    except Exception:
        return ""


def service_status() -> dict:
  if platform.system() != "Linux":
    # On macOS/Windows, systemd is unavailable. Keep dashboard usable.
    return {
      "active": False,
      "status": "manual",
      "pid": 0,
      "ram_mb": 0.0,
      "service_controls_supported": False,
    }

  active = _sh(["systemctl", "--user", "is-active", SERVICE_NAME])
  prop   = _sh(["systemctl", "--user", "show", SERVICE_NAME,
          "--property=MainPID,MemoryCurrent"])
  pid, mem = 0, 0
  for line in prop.splitlines():
    k, _, v = line.partition("=")
    if k == "MainPID" and v.isdigit():
      pid = int(v)
    elif k == "MemoryCurrent" and v.isdigit():
      mem = int(v)
  ram_mb = round(mem / (1024 * 1024), 1)
  if ram_mb == 0 and pid > 0:
    try:
      for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
        if ln.startswith("VmRSS:"):
          ram_mb = round(int(ln.split()[1]) / 1024, 1)
          break
    except OSError:
      pass
  return {
    "active": active == "active",
    "status": active,
    "pid": pid,
    "ram_mb": ram_mb,
    "service_controls_supported": True,
  }


def storage_stats() -> dict:
    def _size(files) -> int:
        total = 0
        for file_path in files:
            try:
                if file_path.is_file():
                    total += file_path.stat().st_size
            except OSError:
                continue
        return total

    screenshot_files = [
        file_path
        for pattern in ("*.jpg", "*.jpeg", "*.png")
        for file_path in SCREENSHOTS_DIR.glob(pattern)
    ]
    total = _size(BASE_DIR.rglob("*"))
    screenshots_size = _size(screenshot_files)
    logs_size = _size(LOG_DIR.glob("*.jsonl"))
    return {
        "total_mb":         round(total / (1024 * 1024), 2),
        "screenshots_mb":   round(screenshots_size / (1024 * 1024), 2),
        "logs_kb":          round(logs_size / 1024, 1),
        "screenshot_count": len(screenshot_files),
        "cap_gb":           SCREENSHOT_CAP_GB,
    }


def _all_logs() -> dict[str, str]:
    logs: dict[str, str] = {}
    for file_path in sorted(LOG_DIR.glob("*.jsonl")):
        try:
            logs[file_path.name] = file_path.read_text(encoding="utf-8")
        except OSError:
            continue
    return logs


def _valid_webhook_url(target: str) -> bool:
    try:
        parsed = urlparse(target)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        return False


def sync_json_to_cloud(
    provider: str,
    target: str,
    api_key: str,
    logs: dict[str, str] | None = None,
) -> dict:
    """Upload selected JSONL logs to a user-selected cloud target."""
    provider = provider.lower().strip()
    payload = {
        "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "logs": logs if logs is not None else _all_logs(),
    }

    data_text = json.dumps(payload, ensure_ascii=False, indent=2)

    if provider == "webhook":
        if not _valid_webhook_url(target):
            return {"ok": False, "error": "A valid HTTP(S) webhook URL is required."}
        req = urllib.request.Request(
            target,
            data=data_text.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                return {"ok": True, "message": "JSON backup sent to webhook."}
        except (urllib.error.URLError, OSError) as exc:
            return {"ok": False, "error": f"Webhook upload failed: {exc}"}

    if provider == "gist":
        if not api_key:
            return {"ok": False, "error": "GitHub token is required for gist backup."}
        files = {
            f"flowtrack_logs_{datetime.date.today().isoformat()}.json": {
                "content": data_text
            }
        }
        body = json.dumps({
            "description": "Flowtrack JSON backup",
            "public": False,
            "files": files,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.github.com/gists",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/vnd.github+json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                gist = json.loads(resp.read())
                return {
                    "ok": True,
                    "message": "Uploaded to a secret (unlisted, not private) Gist. Anyone with its URL can read the logs.",
                    "url": gist.get("html_url", ""),
                }
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"Gist upload failed: {exc}"}

    return {"ok": False, "error": "Unsupported provider. Use gist or webhook."}


def _ensure_ollama_running() -> bool:
    """Start `ollama serve` on demand if not already running.
    Returns True once the API is reachable, False if ollama is not installed."""
    # Fast path — already up?
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2):
            return True
    except Exception:
        pass
    # Try to start it as a background process.
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False  # ollama binary not on PATH
    # Wait up to 10 seconds for the server to become ready.
    for _ in range(20):
        time.sleep(0.5)
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2):
                return True
        except Exception:
            continue
    return False


def _compat_endpoint(provider: str, base_url: str = "") -> tuple[str, str]:
    """Return (chat_completions_url, models_url) for OpenAI-compatible providers."""
    defaults = {
      "openai": OPENAI_URL,
      "xai": XAI_URL,
      "openrouter": OPENROUTER_URL,
      "together": TOGETHER_URL,
      "groq": GROQ_URL,
    }
    base = (base_url or defaults.get(provider, OPENAI_URL)).strip().rstrip("/")
    if base.endswith("/chat/completions"):
      return base, base[: -len("/chat/completions")] + "/models"
    if base.endswith("/completions"):
      return base, base[: -len("/completions")] + "/models"
    if base.endswith("/v1"):
      return base + "/chat/completions", base + "/models"
    return base + "/chat/completions", base + "/models"


def _extract_model_ids(data: dict) -> list[str]:
    out: list[str] = []
    raw = data.get("data")
    if not isinstance(raw, list):
      raw = data.get("models")
    if isinstance(raw, list):
      for item in raw:
        if isinstance(item, str):
          out.append(item)
          continue
        if not isinstance(item, dict):
          continue
        ident = item.get("id") or item.get("name") or item.get("model")
        if isinstance(ident, str) and ident.strip():
          out.append(ident.strip())
    return sorted(set(out))


def fetch_provider_models(provider: str, api_key: str, base_url: str = "") -> tuple[list[str], str | None]:
    provider = provider.lower().strip()
    try:
      if provider == "ollama":
        if not _ensure_ollama_running():
          return [], "Ollama is not running."
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=8) as resp:
          tags = json.loads(resp.read())
        models = [m.get("name", "") for m in tags.get("models", []) if m.get("name")]
        return sorted(set(models)), None

      compat = {"openai", "xai", "openrouter", "together", "groq"}
      if provider in compat:
        if not api_key:
          return [], f"API key is required for {provider}."
        _, models_url = _compat_endpoint(provider, base_url)
        req = urllib.request.Request(
          models_url,
          headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
          },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
          data = json.loads(resp.read())
        models = _extract_model_ids(data)
        if not models:
          return [], f"No models returned by {provider}."
        return models, None

      if provider == "anthropic":
        if not api_key:
          return [], "API key is required for anthropic."
        models_url = (base_url or ANTHROPIC_URL).replace("/messages", "/models")
        req = urllib.request.Request(
          models_url,
          headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
          },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
          data = json.loads(resp.read())
        models = _extract_model_ids(data)
        return (models, None) if models else ([], "No models returned by anthropic.")

      if provider == "gemini":
        if not api_key:
          return [], "API key is required for gemini."
        url = "https://generativelanguage.googleapis.com/v1beta/models?key=" + api_key
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
          data = json.loads(resp.read())
        models: list[str] = []
        for item in data.get("models", []):
          methods = item.get("supportedGenerationMethods", [])
          if methods and "generateContent" not in methods:
            continue
          name = item.get("name", "")
          if name.startswith("models/"):
            name = name.split("/", 1)[1]
          if name:
            models.append(name)
        return (sorted(set(models)), None) if models else ([], "No models returned by gemini.")

      return [], f"Unsupported provider: {provider}"
    except urllib.error.HTTPError as exc:
      detail = ""
      try:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
      except Exception:
        detail = str(exc)
      return [], f"HTTP {exc.code} from {provider}: {detail}"
    except Exception as exc:
      return [], f"Could not load models for {provider}: {exc}"


def query_llm(
    prompt: str,
    provider: str,
    model: str,
    api_key: str,
    base_url: str = "",
    history: list[dict] | None = None,
) -> tuple[str | None, str | None]:
  provider = provider.lower().strip()
  history = history or []
  try:
    if provider == "ollama":
      model = (model or "llama3").strip()
      ollama_url = (base_url or OLLAMA_URL).strip()
      if ollama_url.endswith("/"):
        ollama_url = ollama_url[:-1]
      if ollama_url.endswith(":11434"):
        ollama_url = ollama_url + "/api/generate"
      elif "/api/" not in ollama_url:
        ollama_url = ollama_url + "/api/generate"

      # Start ollama on-demand if not running.
      if not _ensure_ollama_running():
        return None, "Ollama is not installed or failed to start. Install from https://ollama.com then run: ollama pull llama3"

      # If requested model is missing, fall back to an installed Ollama model.
      try:
        tags_url = ollama_url.replace("/api/generate", "/api/tags")
        with urllib.request.urlopen(tags_url, timeout=10) as resp:
          tags = json.loads(resp.read())
        installed = [m.get("name", "") for m in tags.get("models", []) if m.get("name")]
        if not installed:
          return None, "Ollama is running but no models are installed. Run: ollama pull llama3"
        if model in installed:
          pass
        elif ":" not in model:
          pref = next((m for m in installed if m.startswith(model + ":")), "")
          model = pref or installed[0]
        else:
          model = installed[0]
      except Exception:
        pass

      # keep_alive=5m: model stays loaded for 5 min of inactivity, then auto-unloads.
      payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "keep_alive": "5m"}).encode("utf-8")
      req = urllib.request.Request(
        ollama_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
      )
      with urllib.request.urlopen(req, timeout=600) as resp:
        data = json.loads(resp.read())
        text = data.get("response", "").strip()
        return (text if text else None, None if text else "Ollama returned an empty response.")

    if provider in {"openai", "xai", "openrouter", "together", "groq"}:
      if not api_key:
        return None, f"{provider} API key is required."
      chat_url, _ = _compat_endpoint(provider, base_url)
      payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
      }).encode("utf-8")
      headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
      if provider == "openrouter":
        headers["HTTP-Referer"] = "https://flowtrack.local"
        headers["X-Title"] = "Flowtrack"
      req = urllib.request.Request(
        chat_url,
        data=payload,
        headers=headers,
        method="POST",
      )
      with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
        text = data["choices"][0]["message"]["content"].strip()
        return (text if text else None, None if text else f"{provider} returned an empty response.")

    if provider == "anthropic":
      if not api_key:
        return None, "Anthropic API key is required."
      payload = json.dumps({
        "model": model,
        "max_tokens": 800,
        "messages": [{"role": "user", "content": prompt}],
      }).encode("utf-8")
      req = urllib.request.Request(
        base_url or ANTHROPIC_URL,
        data=payload,
        headers={
          "Content-Type": "application/json",
          "x-api-key": api_key,
          "anthropic-version": "2023-06-01",
        },
        method="POST",
      )
      with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        parts = data.get("content", [])
        text = parts[0].get("text", "").strip() if parts else ""
        return (text if text else None, None if text else "Anthropic returned an empty response.")

    if provider == "gemini":
      if not api_key:
        return None, "Gemini API key is required."
      url = (base_url or GEMINI_URL_TMPL).format(model=model, key=api_key)
      payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode("utf-8")
      req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
      with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return (text if text else None, None if text else "Gemini returned an empty response.")

    return None, f"Unsupported provider: {provider}"

  except urllib.error.HTTPError as exc:
    detail = ""
    try:
      detail = exc.read().decode("utf-8", errors="replace")[:400]
    except Exception:
      detail = str(exc)
    return None, f"HTTP {exc.code} from {provider}: {detail}"
  except urllib.error.URLError as exc:
    if provider == "ollama":
      return None, "Cannot reach Ollama at http://localhost:11434. Start Ollama and run: ollama run llama3"
    return None, f"Network error for {provider}: {exc.reason}"
  except (json.JSONDecodeError, OSError, KeyError, IndexError, TypeError) as exc:
    return None, f"{provider} request failed: {exc}"


def today_events() -> list[dict]:
    f = LOG_DIR / f"{datetime.date.today().isoformat()}.jsonl"
    if not f.exists():
        return []
    out = []
    try:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict):
                            out.append(entry)
                    except json.JSONDecodeError:
                        pass
    except OSError:
        return []
    return out


def recent_screenshots(limit: int = 12) -> list[str]:
    if not SCREENSHOTS_DIR.exists():
        return []
    files = []
    for pattern in ("*.jpg", "*.jpeg", "*.png"):
        files.extend(SCREENSHOTS_DIR.glob(pattern))
    dated_files = []
    for file_path in files:
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:jpe?g|png)", file_path.name, re.IGNORECASE):
            continue
        try:
            dated_files.append((file_path.stat().st_mtime, file_path.name))
        except OSError:
            continue
    dated_files.sort(reverse=True)
    return [name for _, name in dated_files[:max(0, limit)]]


def export_logs_by_date(start_date: str = "", end_date: str = "") -> dict:
    """Export logs for a date range. If empty, returns today's logs."""
    try:
        if not start_date:
            start_date = datetime.date.today().isoformat()
        if not end_date:
            end_date = datetime.date.today().isoformat()
        
        start = datetime.date.fromisoformat(start_date)
        end = datetime.date.fromisoformat(end_date)
        if start > end:
            return {"error": "Start date must be on or before end date."}
        if (end - start).days > 3660:
            return {"error": "Date range is too large (maximum 10 years)."}
        
        payload = {"exported_at": datetime.datetime.now().isoformat(timespec="seconds"), "logs": {}}
        
        current = start
        while current <= end:
            f = LOG_DIR / f"{current.isoformat()}.jsonl"
            if f.exists():
                payload["logs"][f.name] = f.read_text(encoding="utf-8")
            current += datetime.timedelta(days=1)
        
        return payload
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}


def logs_for_scope(backup_type: str, start_date: str = "", end_date: str = "") -> dict:
    if backup_type == "today":
        return export_logs_by_date()
    if backup_type == "all":
        return {
            "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "logs": _all_logs(),
        }
    if backup_type == "custom":
        if not start_date or not end_date:
            return {"error": "Both start and end dates are required."}
        return export_logs_by_date(start_date, end_date)
    return {"error": "Unknown backup type. Use today, all, or custom."}


def logs_as_jsonl(logs: dict[str, str]) -> bytes:
    lines = []
    for filename in sorted(logs):
        lines.extend(line for line in logs[filename].splitlines() if line.strip())
    content = "\n".join(lines)
    if content:
        content += "\n"
    return content.encode("utf-8")


def latest_report() -> str:
    if not REPORTS_DIR.exists():
        return ""
    reports = sorted(REPORTS_DIR.glob("analysis_*.txt"), reverse=True)
    return reports[0].read_text(encoding="utf-8") if reports else ""


def _quick_focus(entries: list[dict]) -> str:
    if not entries:
        return "N/A"
    try:
        # Keep the live card consistent with the full analyzer: repair legacy
        # cumulative rows and merge adjacent v2 interval segments first.
        from analyze import calculate_focus_score, normalize_entries

        normalized = normalize_entries(entries)
        if not any(float(entry.get("duration", 0)) > 0 for entry in normalized):
            return "N/A"
        return str(calculate_focus_score(normalized)["daily"])
    except Exception:
        return "N/A"


# ── Background analysis ────────────────────────────────────────────────────────

_lock    = threading.Lock()
_running = False
_result: dict = {"status": "idle", "output": "No analysis run yet. Click Run Analysis."}


def _start_analysis(use_ai: bool, provider: str = "", model: str = "", api_key: str = "") -> bool:
  global _running, _result

  provider = provider.lower().strip()
  if provider == "none":
    use_ai = False
  if use_ai and provider not in PROVIDER_DEFAULT_MODELS:
    _result = {"status": "error", "output": f"Unsupported analysis provider: {provider or '(empty)'}"}
    return False

  def _work() -> None:
    global _running, _result
    try:
      cmd = [_resolve_python_executable(), _resolve_analyze_script()]
      child_env = os.environ.copy()
      if not use_ai:
        cmd.append("--no-ai")
      elif provider:
        cmd.extend(["--provider", provider, "--model", model or PROVIDER_DEFAULT_MODELS[provider]])
        key_env = API_KEY_ENV_VARS.get(provider)
        if api_key and key_env:
          child_env[key_env] = api_key
      r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=child_env)
      out = r.stdout + (("\n\nSTDERR:\n" + r.stderr) if r.stderr else "")
      _result = {"status": "done" if r.returncode == 0 else "error", "output": out}
    except subprocess.TimeoutExpired:
      _result = {"status": "error", "output": "Analysis timed out after 180 s."}
    except Exception as exc:
      _result = {"status": "error", "output": str(exc)}
    finally:
      _running = False

  with _lock:
    if _running:
      return False
    _running = True
    _result = {"status": "running", "output": "Analysis in progress…"}
  threading.Thread(target=_work, daemon=True).start()
  return True


# ── Embedded HTML / CSS / JS ───────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flowtrack Dashboard</title>
<style>
:root{
  --bg:#111111;
  --surface:#1c1c1c;
  --surface-2:#242424;
  --border:#2e2e2e;
  --accent:#f97316;
  --accent-dim:rgba(249,115,22,.12);
  --accent-border:rgba(249,115,22,.35);
  --blue:#60a5fa;
  --blue-dim:rgba(96,165,250,.12);
  --success:#34d399;
  --success-dim:rgba(52,211,153,.12);
  --danger:#f87171;
  --danger-dim:rgba(248,113,113,.12);
  --warn:#fbbf24;
  --warn-dim:rgba(251,191,36,.12);
  --text:#f5f5f4;
  --muted:#a3a3a3;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg);
  color:var(--text);
  font-family:'Inter',system-ui,sans-serif;
  min-height:100vh;
  padding-bottom:48px;
}
/* Header */
header{
  background:var(--surface);
  border-bottom:1px solid var(--border);
  padding:14px 28px;
  display:flex;align-items:center;justify-content:space-between;
  position:sticky;top:0;z-index:100;
}
.logo{display:flex;align-items:center;gap:12px}
.logo-icon{
  width:40px;height:40px;
  background:var(--accent);
  border-radius:9px;display:flex;align-items:center;justify-content:center;
  font-size:18px;
}
.logo h1{font-size:19px;font-weight:700;letter-spacing:-0.3px;color:var(--text)}.logo h1 span{color:var(--accent)}
.hdr-right{display:flex;align-items:center;gap:20px}
.badge{display:flex;align-items:center;gap:7px;padding:5px 12px;border-radius:6px;font-size:11px;font-weight:600;border:1px solid}
.badge.active{border-color:rgba(52,211,153,.4);color:var(--success);background:var(--success-dim)}
.badge.inactive{border-color:rgba(248,113,113,.4);color:var(--danger);background:var(--danger-dim)}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex-shrink:0}
.badge.active .dot{animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
/* Main */
main{max-width:1440px;margin:0 auto;padding:28px 24px;display:flex;flex-direction:column;gap:20px}
/* Cards */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.card{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:12px;padding:20px 22px;
  transition:border-color .2s;
}
.card:hover{border-color:#444}
.card-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted);margin-bottom:10px}
.card-value{font-size:32px;font-weight:800;letter-spacing:-1px;line-height:1;color:var(--text);margin:8px 0}
.card-sub{font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.5}
.c-accent-1{color:var(--accent)}.c-accent-2{color:var(--blue)}.c-accent-3{color:var(--warn)}.c-success{color:var(--success)}.c-warn{color:var(--warn)}.c-danger{color:var(--danger)}
.two-col{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px}
/* Controls */
.controls{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:12px;padding:18px 22px;
}
.ctrl-row{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:12px}
.ctrl-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.2px;color:var(--muted)}
.btn{
  padding:8px 16px;border-radius:8px;font-size:12px;font-weight:500;
  border:1px solid transparent;cursor:pointer;
  transition:all .15s ease;display:inline-flex;align-items:center;gap:6px;white-space:nowrap;
  font-family:'Inter',system-ui,sans-serif;
}
.btn:active{opacity:.8}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-green,.btn-success{background:var(--success-dim);color:var(--success);border-color:rgba(52,211,153,.3)}
.btn-green:hover,.btn-success:hover{border-color:rgba(52,211,153,.6);background:rgba(52,211,153,.18)}
.btn-red,.btn-danger{background:var(--danger-dim);color:var(--danger);border-color:rgba(248,113,113,.3)}
.btn-red:hover,.btn-danger:hover{border-color:rgba(248,113,113,.6);background:rgba(248,113,113,.18)}
.btn-yellow,.btn-warn{background:var(--warn-dim);color:var(--warn);border-color:rgba(251,191,36,.3)}
.btn-yellow:hover,.btn-warn:hover{border-color:rgba(251,191,36,.6);background:rgba(251,191,36,.18)}
.btn-accent{background:var(--accent-dim);color:var(--accent);border-color:var(--accent-border)}
.btn-accent:hover{background:rgba(249,115,22,.2);border-color:rgba(249,115,22,.6)}
.btn-accent2{background:var(--blue-dim);color:var(--blue);border-color:rgba(96,165,250,.3)}
.btn-accent2:hover{background:rgba(96,165,250,.2);border-color:rgba(96,165,250,.6)}
.btn-muted{background:var(--surface-2);color:var(--muted);border-color:var(--border)}
.btn-muted:hover{background:#2e2e2e;color:var(--text);border-color:#444}
.sep{width:1px;height:28px;background:var(--border);margin:0 2px;flex-shrink:0}
/* Panel */
.panel{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:12px;overflow:hidden;
}
.panel-hdr{
  padding:13px 18px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  background:var(--surface-2);
}
.panel-title{font-size:12px;font-weight:700;display:flex;align-items:center;gap:9px;color:var(--text);text-transform:uppercase;letter-spacing:.6px}
.pill{font-size:10px;padding:3px 10px;border-radius:999px;font-weight:800;text-transform:uppercase}
.pill-success{background:rgba(74,222,128,.15);color:var(--success)}
.pill-accent{background:rgba(167,139,250,.18);color:var(--accent)}
/* Input fields */
input[type="text"],input[type="password"],input[type="email"],textarea,select{
  background:var(--surface-2);
  border:1px solid var(--border);
  color:var(--text);
  border-radius:8px;
  padding:9px 12px;
  font-size:12.5px;
  transition:border-color .15s;
  font-family:'Inter',system-ui,sans-serif;
}
input[type="text"]:focus,input[type="password"]:focus,textarea:focus,select:focus{
  outline:none;
  border-color:var(--accent);
  box-shadow:0 0 0 2px rgba(249,115,22,.15);
}
input[type="date"],input[type="datetime-local"]{
  background:var(--surface-2);
  border:1px solid var(--border);
  color:var(--text);
  border-radius:8px;
  padding:9px 12px;
  font-size:12.5px;
  font-family:'Inter',system-ui,sans-serif;
}
/* Log table */
.tbl-wrap{overflow-y:auto;max-height:420px}
table{width:100%;border-collapse:collapse;font-size:11.5px}
th{padding:9px 14px;text-align:left;color:var(--muted);font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--surface-2);font-family:'Inter',system-ui,sans-serif}
td{padding:8px 14px;border-bottom:1px solid var(--border);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px;font-family:'JetBrains Mono','Courier New',monospace;font-size:11px;color:var(--text)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.03)}
.ev-change{color:var(--accent);font-weight:600}.ev-interval{color:var(--muted)}.ev-ts{color:var(--muted);font-size:10px}
/* Screenshots */
.shots-grid{padding:12px;display:grid;grid-template-columns:repeat(3,1fr);gap:8px;overflow-y:auto;max-height:420px}
.shot-wrap{display:flex;flex-direction:column;gap:3px}
.shot{
  border-radius:8px;overflow:hidden;cursor:zoom-in;
  border:1px solid var(--border);
  transition:border-color .2s,transform .2s;
  aspect-ratio:16/10;
  background:var(--surface-2);
  padding:0;width:100%;color:inherit;
}
.shot:hover{border-color:#555;transform:scale(1.03)}
.shot img{width:100%;height:100%;object-fit:cover;display:block}
.shot-ts{font-size:9px;color:var(--muted);text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Analysis */
.analysis{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:12px;overflow:hidden;
  margin-bottom:20px;
}
.analysis-toolbar{
  padding:13px 18px;
  border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  background:var(--surface-2);
}
.analysis-output{
  padding:20px;
  font-family:'JetBrains Mono','Courier New',monospace;
  font-size:12px;
  line-height:1.8;
  white-space:pre-wrap;
  word-break:break-word;
  max-height:520px;
  overflow-y:auto;
  color:#a3a3a3;
}
.analysis-output.analysis-rich{font-family:'Inter',system-ui,sans-serif;line-height:1.55;color:var(--text)}
.report-metrics{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.report-chip{padding:5px 9px;border:1px solid var(--border);border-radius:999px;font-size:11px;background:var(--surface-2);color:var(--muted)}
.report-chip strong{color:var(--text);font-weight:700}
.report-card{background:var(--surface-2);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin:10px 0}
.report-card h4{margin:0 0 8px 0;font-size:12px;letter-spacing:.3px;text-transform:uppercase;color:var(--muted)}
.report-card p{margin:0 0 8px 0;font-size:13px;color:var(--text)}
.report-card ul{margin:0;padding-left:18px}
.report-card li{margin:6px 0;color:var(--text);font-size:13px}
.chat-thread{font-family:'Inter',system-ui,sans-serif;line-height:1.45;white-space:normal}
.msg{display:flex;margin:10px 0}
.msg.user{justify-content:flex-end}
.msg.assistant{justify-content:flex-start}
.msg-bubble{max-width:85%;padding:10px 12px;border-radius:10px;border:1px solid var(--border);font-size:12.5px;white-space:pre-wrap;word-break:break-word}
.msg.user .msg-bubble{background:var(--accent-dim);border-color:var(--accent-border);color:var(--text)}
.msg.assistant .msg-bubble{background:var(--surface-2);color:var(--text)}
.msg-meta{font-size:10px;color:var(--muted);margin-top:4px}
.typing{color:var(--warn);font-style:italic}
.ao-done{color:var(--text)}.ao-error{color:var(--danger)}.ao-running{color:var(--warn);animation:pulse-text 1.2s infinite}
@keyframes pulse-text{0%,100%{opacity:1}50%{opacity:.6}}
/* Modal */
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:999;align-items:center;justify-content:center}
.modal.open{display:flex;animation:fadeIn .2s ease}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.modal img{max-width:95vw;max-height:90vh;border-radius:10px;box-shadow:0 20px 60px rgba(0,0,0,.7);border:1px solid var(--border)}
.modal-x{position:fixed;top:20px;right:24px;background:var(--surface-2);border:1px solid var(--border);color:var(--text);width:36px;height:36px;border-radius:8px;cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;z-index:1000;transition:background .15s}
.modal-x:hover{background:#333;border-color:#555}
/* Misc */
.rtag{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:7px;font-weight:600}
.rdot{width:6px;height:6px;border-radius:50%;background:var(--border)}
.rdot.on{background:var(--success)}
.empty{text-align:center;color:var(--muted);padding:32px 16px;font-size:12px}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:#333;border-radius:3px}::-webkit-scrollbar-thumb:hover{background:#444}
@media(max-width:1100px){.cards{grid-template-columns:repeat(2,1fr)}.two-col{grid-template-columns:1fr}}
@media(max-width:640px){.cards{grid-template-columns:1fr}header{padding:12px 16px;gap:8px}.hdr-right{gap:8px}.rtag{display:none}main{padding:16px 12px}}
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-icon">⚡</div>
    <h1>Flow<span>track</span></h1>
  </div>
  <div class="hdr-right">
    <div class="rtag"><span class="rdot" id="rdot"></span> auto-refresh 3 s</div>
    <div class="badge inactive" id="svcBadge" role="status" aria-live="polite">
      <span class="dot"></span><span id="svcTxt">checking…</span>
    </div>
  </div>
</header>

<main>

  <!-- ── Stat cards ── -->
  <div class="cards">
    <div class="card">
      <div class="card-label">Focus Score</div>
      <div class="card-value c-accent-1" id="cScore">—</div>
      <div class="card-sub">out of 100 · based on today</div>
    </div>
    <div class="card">
      <div class="card-label">Tracker RAM</div>
      <div class="card-value c-success" id="cRam">—</div>
      <div class="card-sub" id="cRamSub">MB used by daemon</div>
    </div>
    <div class="card">
      <div class="card-label">Storage Used</div>
      <div class="card-value c-warn" id="cStorage">—</div>
      <div class="card-sub" id="cStorageSub">MB in ~/.focusaudit/</div>
    </div>
    <div class="card">
      <div class="card-label">Events Today</div>
      <div class="card-value c-accent-1" id="cEvents">—</div>
      <div class="card-sub">activity segments logged</div>
    </div>
  </div>

  <!-- ── Controls ── -->
  <div class="controls">
    <div class="ctrl-label">🎛️ Service &amp; Tools</div>
    <div class="ctrl-row">
      <button class="btn btn-green"  id="btnStart"   onclick="svc('start')">▶ Start Tracker</button>
      <button class="btn btn-red"    id="btnStop"    onclick="svc('stop')">■ Stop Tracker</button>
      <button class="btn btn-yellow" id="btnRestart" onclick="svc('restart')">↺ Restart</button>
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);margin-left:8px;cursor:pointer" title="Auto-start tracker and dashboard on login (Linux systemd only)">
        <input type="checkbox" id="autoStartToggle" onchange="toggleAutoStart(this.checked)" style="accent-color:var(--warn)">
        Auto-start on boot
      </label>
      <div class="sep"></div>
      <button class="btn btn-accent" onclick="runAnalysis(false)">📊 Run Text Analysis</button>
      <button class="btn btn-muted"  onclick="runAnalysis(true)">🤖 Run with Selected AI</button>
      <div class="sep"></div>
      <button class="btn btn-muted"  onclick="openFolder()">🗂 Open Screenshots</button>
      <button class="btn btn-muted"  onclick="window.open('/api/logs?limit=500&amp;token='+encodeURIComponent(FLOWTRACK_TOKEN),'_blank')">📄 Raw Log JSON</button>
    </div>
    <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
      <div class="ctrl-row" style="margin-top:0">
        <label style="font-size:11px;color:var(--muted);font-weight:700">📅 Download/Backup Logs:</label>
        <select id="backupType" class="btn btn-muted" onchange="updateBackupDateUI()" style="padding:7px 10px">
          <option value="today">Today only</option>
          <option value="all">All logs</option>
          <option value="custom">Custom date range</option>
        </select>
      </div>
      <div id="dateRangeDiv" style="display:none;margin-top:10px;gap:8px;flex-wrap:wrap;align-items:center">
        <label style="font-size:11px;color:var(--muted)">From:</label>
        <input id="backupStartDate" type="date" style="min-width:140px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:11px">
        <label style="font-size:11px;color:var(--muted)">To:</label>
        <input id="backupEndDate" type="date" style="min-width:140px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:11px">
      </div>
      <div class="ctrl-row" style="margin-top:10px;gap:8px">
        <button class="btn btn-accent" onclick="downloadBackup()">💾 Download to Laptop</button>
        <select id="uploadProvider" class="btn btn-muted" style="padding:7px 10px;min-width:140px" onchange="updateUploadUI()">
          <option value="none">No cloud upload</option>
          <option value="gist">GitHub secret Gist (unlisted)</option>
          <option value="webhook">Webhook URL</option>
        </select>
        <input id="uploadTarget" placeholder="GitHub token or webhook URL" aria-label="Cloud backup credential or URL" style="flex:1;min-width:200px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:11px;display:none" value="">
        <button class="btn btn-accent2" onclick="uploadBackup()" style="display:none" id="uploadBtn">☁ Upload to Cloud</button>
      </div>
      <div style="margin-top:8px;font-size:11px;color:var(--warn)">Cloud upload sends activity logs off this device. GitHub secret Gists are unlisted, not private; anyone with the URL can read them.</div>
      <div id="backupMsg" role="status" aria-live="polite" style="margin-top:8px;font-size:11px;color:var(--muted)"></div>
    </div>

  </div>

  <!-- ── Live Log + Screenshots ── -->
  <div class="two-col">

    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-title">📋 Live Activity Log <span class="pill pill-success">LIVE</span></div>
        <span style="font-size:11px;color:var(--muted)" id="logMeta">—</span>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr>
            <th>Time</th><th>Event</th><th>App</th><th style="max-width:none">Window Title</th><th>Sec</th>
          </tr></thead>
          <tbody id="logBody"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-title">📸 Recent Screenshots <span class="pill pill-accent" id="shotCount">0</span></div>
        <span style="font-size:11px;color:var(--muted)">48 h auto-purge</span>
      </div>
      <div class="shots-grid" id="shotsGrid">
        <div class="empty" style="grid-column:1/-1">Loading…</div>
      </div>
    </div>

  </div>

  <!-- ── AI Analysis output ── -->
  <div class="analysis">
    <div class="analysis-toolbar">
      <strong style="font-size:13px">🤖 AI Analysis Report</strong>
      <span style="font-size:11px;color:var(--muted)" id="analysisMeta" role="status" aria-live="polite">Idle</span>
      <button class="btn btn-muted" style="margin-left:auto" onclick="scrollToTop('analysisOut')">↑ Top</button>
    </div>
    <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <label style="font-size:11px;color:var(--muted);font-weight:700">Provider for analysis:</label>
      <select id="analysisProvider" class="btn btn-muted" onchange="updateAnalysisPlaceholders();refreshOllamaBar('analysis');updateModelList('analysis')" style="padding:7px 10px">
        <option value="ollama" selected>Ollama (default)</option>
        <option value="none">No AI (text only)</option>
        <option value="openai">OpenAI</option>
        <option value="anthropic">Anthropic</option>
        <option value="gemini">Gemini</option>
      </select>
      <select id="analysisModel" class="btn btn-muted" style="flex:1;min-width:220px;padding:7px 10px"></select>
      <button class="btn btn-muted" style="font-size:11px;padding:7px 10px" onclick="updateModelList('analysis', true)">↻ Models</button>
      <input id="analysisApiKey" type="password" placeholder="Not needed for Ollama (required for cloud providers)" style="flex:1;min-width:240px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
      <button class="btn btn-yellow" onclick="verifyAnalysisKey()">✓ Verify Key</button>
    </div>
    <div id="ollamaBarAnalysis" style="display:none;padding:8px 20px;border-bottom:1px solid var(--border);background:var(--surface-2);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span id="ollamaStatusAnalysis" style="font-size:11px;color:var(--muted)">Checking Ollama…</span>
      <button class="btn btn-muted" style="font-size:11px;padding:4px 10px" onclick="ollamaStart('analysis')">▶ Start</button>
      <button class="btn btn-warn" style="font-size:11px;padding:4px 10px" onclick="ollamaFreeRAM('analysis')">⬡ Free RAM</button>
    </div>
    <div id="analysisKeyStatus" style="padding:0 20px 8px 20px;font-size:11px;color:var(--muted);display:none"></div>
    <div class="analysis-output ao-done" id="analysisOut" aria-live="polite">No analysis run yet. Click "Run Analysis" above.</div>
  </div>

  <div class="analysis">
    <div class="analysis-toolbar">
      <strong style="font-size:13px">💬 Ask AI About Your Patterns</strong>
      <span style="font-size:11px;color:var(--muted)">Provider and key are used in-memory only</span>
    </div>
    <div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <select id="chatProvider" class="btn btn-muted" onchange="updateChatPlaceholders();refreshOllamaBar('chat');updateModelList('chat')" style="padding:7px 10px">
        <option value="ollama">Ollama (local, no key)</option>
        <option value="openai">OpenAI (requires API key)</option>
        <option value="xai">xAI Grok (requires API key)</option>
        <option value="openrouter">OpenRouter (requires API key)</option>
        <option value="together">Together AI</option>
        <option value="groq">Groq (requires API key)</option>
        <option value="anthropic">Anthropic (requires API key)</option>
        <option value="gemini">Gemini (requires API key)</option>
      </select>
      <select id="chatModel" class="btn btn-muted" style="min-width:220px;flex:1;padding:7px 10px"></select>
      <button class="btn btn-muted" style="font-size:11px;padding:7px 10px" onclick="updateModelList('chat', true)">↻ Models</button>
      <input id="chatApiKey" type="password" placeholder="API key (not needed for Ollama)" style="min-width:240px;flex:1;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
      <input id="chatBaseUrl" placeholder="Custom base URL (optional)" style="min-width:220px;flex:1;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 10px;font-size:12px">
    </div>
    <div style="padding:12px 20px;border-bottom:1px solid var(--border)">
      <textarea id="chatPrompt" placeholder="Ask about your focus behavior, app usage patterns, distraction triggers, routines, or Flowtrack settings." style="width:100%;min-height:88px;background:#0f172a;border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px;font-size:12px;resize:vertical"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button id="chatSendBtn" class="btn btn-accent" onclick="chatAsk()">Send</button>
        <button class="btn btn-muted" onclick="fillChatTemplate()">Use suggestion template</button>
        <button class="btn btn-muted" onclick="clearChat()">Clear Chat</button>
        <button class="btn btn-yellow" onclick="verifyChatKey()" style="margin-left:auto">✓ Test Key</button>
      </div>
    </div>
    <div id="ollamaBarChat" style="display:none;padding:8px 20px;border-bottom:1px solid var(--border);background:var(--surface-2);display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span id="ollamaStatusChat" style="font-size:11px;color:var(--muted)">Checking Ollama…</span>
      <button class="btn btn-muted" style="font-size:11px;padding:4px 10px" onclick="ollamaStart('chat')">▶ Start</button>
      <button class="btn btn-warn" style="font-size:11px;padding:4px 10px" onclick="ollamaFreeRAM('chat')">⬡ Free RAM</button>
    </div>
    <div id="chatKeyStatus" style="padding:0 20px 8px 20px;font-size:11px;color:var(--muted);display:none"></div>
    <div class="analysis-output chat-thread" id="chatOut" aria-live="polite"><div class="empty" style="padding:12px 4px">Ask your first question. Follow-ups are remembered in this session.</div></div>
  </div>

</main>

<!-- Modal -->
<div class="modal" id="modal" role="dialog" aria-modal="true" aria-hidden="true" aria-label="Screenshot preview" onclick="if(event.target===this) closeModal()">
  <button class="modal-x" id="modalClose" aria-label="Close screenshot preview" onclick="closeModal()">✕</button>
  <img id="modalImg" src="" alt="">
</div>

<script>
const FLOWTRACK_TOKEN = __FLOWTRACK_TOKEN__;
let pollTimer = null;

// ── API ───────────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const headers = new Headers(opts.headers || {});
  headers.set('X-Flowtrack-Token', FLOWTRACK_TOKEN);
  try { return await (await fetch(path, {...opts, headers})).json(); }
  catch { return null; }
}

// ── Refresh dot ───────────────────────────────────────────────────────────────
function flash() {
  const d = document.getElementById('rdot');
  d.classList.add('on');
  setTimeout(() => d.classList.remove('on'), 400);
}

// ── Status ────────────────────────────────────────────────────────────────────
async function fetchStatus() {
  const d = await api('/api/status');
  if (!d) return;

  const controlsSupported = d.service_controls_supported !== false;
  const badge = document.getElementById('svcBadge');
  badge.className = 'badge ' + (d.active ? 'active' : 'inactive');
  document.getElementById('svcTxt').textContent = controlsSupported
    ? (d.active ? 'Tracker Active' : 'Tracker Stopped')
    : 'Manual tracker mode';

  document.getElementById('btnStart').disabled   = !controlsSupported || d.active;
  document.getElementById('btnStop').disabled    = !controlsSupported || !d.active;
  document.getElementById('btnRestart').disabled = !controlsSupported || !d.active;

  document.getElementById('cRam').textContent     = d.ram_mb + ' MB';
  document.getElementById('cStorage').textContent = d.total_mb + ' MB';
  document.getElementById('cStorageSub').textContent =
    d.screenshots_mb + ' MB screenshots · ' + d.logs_kb + ' KB logs · ' + d.screenshot_count + ' files';

  flash();
}

// ── Logs ──────────────────────────────────────────────────────────────────────
async function fetchLogs() {
  const d = await api('/api/logs?limit=120');
  if (!d) return;

  document.getElementById('logMeta').textContent = d.total + ' events today';
  document.getElementById('cEvents').textContent = d.total;
  document.getElementById('cScore').textContent  = d.focus_score;

  const rows = [...d.entries].reverse();
  const tbody = document.getElementById('logBody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">No events yet. Tracker logs every 30 s or on window change.</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(e => {
    const tRaw = (e.ts || '').split('T')[1] || e.ts || '';
    const evtRaw = e.event || '';
    const appRaw = (e.app || 'unknown').substring(0, 16);
    const t = escHtml(tRaw);
    const evt = escHtml(evtRaw);
    const app = escHtml(appRaw);
    const cls = evtRaw === 'change' ? 'ev-change' : 'ev-interval';
    const dur = e.duration !== undefined ? Math.round(e.duration) : '';
    const ttl = escHtml((e.title || '').substring(0, 90));
    return `<tr>
      <td class="ev-ts">${t}</td>
      <td class="${cls}">${evt}</td>
      <td>${app}</td>
      <td style="max-width:280px" title="${ttl}">${ttl}</td>
      <td style="text-align:right;color:var(--muted)">${dur}</td>
    </tr>`;
  }).join('');
}

// ── Screenshots ───────────────────────────────────────────────────────────────
async function fetchShots() {
  const d = await api('/api/screenshots');
  if (!d) return;
  document.getElementById('shotCount').textContent = d.length;
  const grid = document.getElementById('shotsGrid');
  if (!d.length) {
    grid.innerHTML = '<div class="empty" style="grid-column:1/-1">No screenshots yet. They appear within 30 s of the tracker starting.</div>';
    return;
  }
  grid.innerHTML = d.map(name => {
    const safeName = escHtml(name);
    const encodedName = encodeURIComponent(name);
    const label = escHtml(name.replace(/\.(?:jpe?g|png)$/i, '').replace(/_/g, ' '));
    const screenshotUrl = `/screenshots/${encodedName}?token=${encodeURIComponent(FLOWTRACK_TOKEN)}`;
    return `<div class="shot-wrap">
      <button type="button" class="shot" aria-label="Open screenshot ${label}" onclick="openModal('${screenshotUrl}', '${safeName}')">
        <img src="${screenshotUrl}" loading="lazy" alt="Screenshot ${safeName}">
      </button>
      <div class="shot-ts">${label}</div>
    </div>`;
  }).join('');
}

// ── Service control ───────────────────────────────────────────────────────────
async function svc(action) {
  const labels = {start:'Starting…', stop:'Stopping…', restart:'Restarting…'};
  document.getElementById('svcTxt').textContent = labels[action] || action;
  const d = await api('/api/service', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action}),
  });
  if (!d || !d.ok) {
    document.getElementById('svcTxt').textContent = (d && d.error) || 'Service action failed';
    return;
  }
  setTimeout(fetchStatus, 1800);
}

async function toggleAutoStart(enable) {
  const action = enable ? 'enable' : 'disable';
  try {
    const d = await api('/api/service', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action}),
    });
    if (!d || !d.ok) {
      document.getElementById('svcTxt').textContent = (d && d.error) || 'Auto-start change failed';
      document.getElementById('autoStartToggle').checked = !enable;
    } else {
      document.getElementById('svcTxt').textContent = enable ? 'Auto-start enabled (starts on login)' : 'Auto-start disabled';
    }
  } catch(e) {
    document.getElementById('autoStartToggle').checked = !enable;
  }
}

async function checkAutoStart() {
  try {
    const d = await api('/api/autostart');
    if (!d) return;
    const cb = document.getElementById('autoStartToggle');
    if (cb) {
      cb.checked = d.enabled;
      cb.disabled = d.supported === false;
    }
  } catch(e) {}
}

// ── Analysis ──────────────────────────────────────────────────────────────────
async function runAnalysis(useAI) {
  setAnalysisUI('running', 'Starting analysis…');
  document.getElementById('analysisMeta').textContent = 'Running…';
  
  let provider = '';
  let model = '';
  let apiKey = '';
  
  if (useAI) {
    provider = document.getElementById('analysisProvider').value;
    model = normalizeModelValue(document.getElementById('analysisModel').value.trim());
    apiKey = document.getElementById('analysisApiKey').value.trim();
  }
  
  const started = await api('/api/analyze', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      ai: useAI,
      provider: provider,
      model: model,
      api_key: apiKey,
    }),
  });
  if (!started || !started.ok) {
    setAnalysisUI('error', (started && started.error) || 'Analysis could not be started.');
    document.getElementById('analysisMeta').textContent = 'Error ✗';
    return;
  }
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollAnalysis, 1500);
}

async function pollAnalysis() {
  const d = await api('/api/analysis');
  if (!d) return;
  setAnalysisUI(d.status, d.output);
  if (d.status !== 'running') {
    clearInterval(pollTimer);
    pollTimer = null;
    document.getElementById('analysisMeta').textContent =
      d.status === 'done' ? 'Complete ✓' : 'Error ✗';
  }
}

function setAnalysisUI(status, text) {
  const el = document.getElementById('analysisOut');
  if (status === 'done') {
    el.className = 'analysis-output analysis-rich ao-done';
    el.innerHTML = renderAnalysisReport(text || 'No report output.');
    return;
  }
  el.className = 'analysis-output ao-' + status;
  el.textContent = text;
}

function _escapeHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderAnalysisReport(text) {
  const raw = String(text || '').trim();
  const lines = raw.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
  if (!lines.length) return '<div class="empty" style="padding:8px 0">No report yet.</div>';

  const score = raw.match(/focus\s*score[^\d]{0,8}(\d+(?:\.\d+)?)/i);
  const events = raw.match(/(\d+)\s+events?/i);
  const switches = raw.match(/(\d+)\s+(?:app\s+)?switch(?:es)?/i);

  let html = '<div class="report-metrics">';
  if (score) html += `<span class="report-chip">Focus score: <strong>${_escapeHtml(score[1])}</strong></span>`;
  if (events) html += `<span class="report-chip">Events: <strong>${_escapeHtml(events[1])}</strong></span>`;
  if (switches) html += `<span class="report-chip">Switches: <strong>${_escapeHtml(switches[1])}</strong></span>`;
  html += '</div>';

  const bullets = [];
  const paras = [];
  for (const ln of lines) {
    if (/^(?:[-*]|\d+\.)\s+/.test(ln)) bullets.push(ln.replace(/^(?:[-*]|\d+\.)\s+/, ''));
    else paras.push(ln.replace(/^#+\s*/, '').replace(/\*\*/g, ''));
  }

  const summary = paras.shift() || 'Analysis summary';
  html += `<div class="report-card"><h4>Summary</h4><p>${_escapeHtml(summary)}</p></div>`;

  if (bullets.length) {
    html += '<div class="report-card"><h4>Key Findings</h4><ul>' + bullets.map(b => `<li>${_escapeHtml(b)}</li>`).join('') + '</ul></div>';
  }

  if (paras.length) {
    html += '<div class="report-card"><h4>Details</h4>' + paras.map(p => `<p>${_escapeHtml(p)}</p>`).join('') + '</div>';
  }
  return html;
}

function scrollToTop(id) {
  document.getElementById(id).scrollTop = 0;
}

// ── Open folder ───────────────────────────────────────────────────────────────
function openFolder() {
  api('/api/open-screenshots', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}',
  });
}

function fillChatTemplate() {
  document.getElementById('chatPrompt').value =
    'Analyze my recent Flowtrack behavior and give me: 1) top 3 focus problems with numbers, 2) practical fixes for tomorrow, 3) one simple rule I should enforce.';
}

let chatHistory = [];
const CHAT_MAX_TURNS = 12;

function escHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function copyCmd(btn) {
  const cmd = btn && btn.getAttribute('data-cmd');
  if (!cmd) return;
  navigator.clipboard.writeText(cmd).then(() => {
    btn.textContent = 'Copied!';
    setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
  });
}

function renderChat(runningText = '') {
  const out = document.getElementById('chatOut');
  if (!out) return;
  const blocks = [];
  for (const m of chatHistory) {
    blocks.push(`<div class="msg ${m.role === 'user' ? 'user' : 'assistant'}">\
      <div>\
        <div class="msg-bubble">${escHtml(m.content)}</div>\
        <div class="msg-meta">${m.role === 'user' ? 'You' : 'Flowtrack Coach'}</div>\
      </div>\
    </div>`);
  }
  if (runningText) {
    blocks.push(`<div class="msg assistant"><div><div class="msg-bubble typing">${escHtml(runningText)}</div><div class="msg-meta">Flowtrack Coach</div></div></div>`);
  }
  out.innerHTML = blocks.length ? blocks.join('') : '<div class="empty" style="padding:12px 4px">Ask your first question. Follow-ups are remembered in this session.</div>';
  out.scrollTop = out.scrollHeight;
}

function clearChat() {
  chatHistory = [];
  renderChat();
}

// ── Model presets per provider ───────────────────────────────────────────────
const MODEL_PRESETS = {
  ollama:    ['llama3', 'llama3.2', 'llama3.1', 'llama3.2:1b', 'gemma3', 'gemma2', 'mistral', 'phi4', 'phi3', 'deepseek-r1', 'qwen2.5', 'codellama'],
  openai:    ['gpt-4o-mini'],
  xai:       ['grok-4.5', 'grok-4.3', 'grok-latest'],
  groq:      ['openai/gpt-oss-20b', 'openai/gpt-oss-120b', 'qwen/qwen3.6-27b'],
  openrouter:['openai/gpt-4o-mini'],
  together:  ['meta-llama/Llama-3.3-70B-Instruct-Turbo', 'openai/gpt-oss-20b', 'openai/gpt-oss-120b'],
  anthropic: ['claude-haiku-4-5-20251001', 'claude-sonnet-4-6', 'claude-opus-4-7'],
  gemini:    ['gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-3.5-flash-lite', 'gemini-2.5-flash'],
};

function normalizeModelValue(value) {
  const v = (value || '').trim();
  return v === '__auto__' ? '' : v;
}

async function updateModelList(section, forceFetch = false) {
  const provider = document.getElementById(section + 'Provider').value;
  const list = document.getElementById(section + 'Model');
  const modelInput = document.getElementById(section + 'Model');
  const keyInput = document.getElementById(section + 'ApiKey');
  const baseInput = document.getElementById(section + 'BaseUrl');
  if (!list) return;
  const presets = MODEL_PRESETS[provider] || [];
  let options = [...presets];
  const apiKey = keyInput ? keyInput.value.trim() : '';
  const baseUrl = baseInput ? baseInput.value.trim() : '';

  // Load real provider models when possible.
  if (provider === 'ollama' || forceFetch || apiKey) {
    const d = await api('/api/models', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider, api_key: apiKey, base_url: baseUrl}),
    });
    if (d && d.ok && Array.isArray(d.models)) {
      d.models.forEach(m => { if (!options.includes(m)) options.unshift(m); });
    }
  }

  const autoLabel = provider === 'ollama' ? 'Auto (recommended local model)' : 'Auto (recommended by provider)';
  const previous = modelInput ? modelInput.value : '__auto__';
  let html = `<option value="__auto__">${autoLabel}</option>`;
  html += options.map(m => `<option value="${_escapeHtml(m)}">${_escapeHtml(m)}</option>`).join('');
  list.innerHTML = html;

  // Auto-set a sensible model only when field is empty or still generic.
  if (modelInput) {
    const current = (previous || '').trim();
    const isKnown = current && (current === '__auto__' || options.includes(current));
    if (isKnown) {
      modelInput.value = current;
    } else {
      modelInput.value = '__auto__';
    }
  }
}

function updateChatPlaceholders() {
  const provider = document.getElementById('chatProvider').value;
  const keyInput = document.getElementById('chatApiKey');
  const keyPH = {
    ollama: 'Not needed for Ollama — leave empty',
    openai: 'sk-... (OpenAI API key)',
    xai: 'xai-... (xAI API key)',
    groq: 'gsk_... (Groq API key)',
    openrouter: 'sk-or-... (OpenRouter API key)',
    together: 'Together API key',
    anthropic: 'sk-ant-... (Anthropic API key)',
    gemini: 'AIza... (Google AI API key)',
  };
  keyInput.placeholder = keyPH[provider] || 'API key';
}

function updateAnalysisPlaceholders() {
  const provider = document.getElementById('analysisProvider').value;
  const keyInput = document.getElementById('analysisApiKey');
  const keyPH = {
    ollama: 'Not needed for Ollama — leave empty',
    none:   'No AI selected',
    openai: 'sk-... (OpenAI API key)',
    xai: 'xai-... (xAI API key)',
    groq: 'gsk_... (Groq API key)',
    openrouter: 'sk-or-... (OpenRouter API key)',
    together: 'Together API key',
    anthropic: 'sk-ant-... (Anthropic API key)',
    gemini: 'AIza... (Google AI API key)',
  };
  keyInput.placeholder = keyPH[provider] || 'API key';
}

// ── Ollama on-demand controls ─────────────────────────────────────────────────
function _ollamaIds(section) {
  return {
    bar: document.getElementById('ollamaBar' + section.charAt(0).toUpperCase() + section.slice(1)),
    status: document.getElementById('ollamaStatus' + section.charAt(0).toUpperCase() + section.slice(1)),
    provider: document.getElementById(section + 'Provider'),
    model: document.getElementById(section + 'Model'),
  };
}

async function refreshOllamaBar(section) {
  const {bar, status, provider} = _ollamaIds(section);
  if (!bar) return;
  if (!provider || provider.value !== 'ollama') { bar.style.display = 'none'; return; }
  bar.style.display = 'flex';
  status.style.color = 'var(--muted)';
  status.textContent = 'Checking Ollama…';
  const d = await api('/api/ollama');
  if (!d) { status.textContent = 'Could not reach dashboard backend.'; return; }
  if (!d.running) {
    status.style.color = 'var(--danger)';
    status.textContent = '● Not running — click ▶ Start or just send a message (auto-starts).';
  } else {
    const loaded = d.loaded && d.loaded.length ? d.loaded.join(', ') : 'none loaded';
    const models = d.models && d.models.length ? d.models.join(', ') : 'none installed';
    status.style.color = 'var(--success)';
    status.textContent = `● Running · In RAM: ${loaded} · Installed: ${models}`;
  }
}

async function ollamaStart(section) {
  const {status} = _ollamaIds(section);
  status.style.color = 'var(--warn)';
  status.textContent = 'Starting Ollama…';
  const d = await api('/api/ollama', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'start'}),
  });
  if (d && d.ok) { refreshOllamaBar(section); }
  else { status.style.color = 'var(--danger)'; status.textContent = (d && d.error) || 'Failed to start.'; }
}

async function ollamaFreeRAM(section) {
  const {status, model} = _ollamaIds(section);
  const modelName = normalizeModelValue(model && model.value.trim());
  status.style.color = 'var(--warn)';
  status.textContent = `Unloading ${modelName || 'loaded model'} from RAM…`;
  const d = await api('/api/ollama', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'unload', model: modelName}),
  });
  if (d && d.ok) {
    status.style.color = 'var(--muted)';
    status.textContent = `✓ ${d.message}`;
    setTimeout(() => refreshOllamaBar(section), 1500);
  } else {
    status.style.color = 'var(--danger)';
    status.textContent = (d && d.error) || 'Unload failed.';
  }
}

function ollamaErrHtml(errMsg) {
  const m = errMsg && errMsg.match(/Run:\s*(ollama\s+\S+(?:\s+\S+)?)/i);
  if (!m) return null;
  const rawCmd = m[1];
  const cmd = escHtml(rawCmd);
  const cmdAttr = rawCmd.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const message = escHtml(errMsg.replace(m[1],'').replace('Run:','').trim());
  return `<span style="color:var(--danger)">${message}</span>
<div style="display:flex;align-items:center;gap:8px;margin-top:8px;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;">
  <code style="font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--accent);flex:1">Run: ${cmd}</code>
  <button data-cmd="${cmdAttr}" onclick="copyCmd(this)" style="background:var(--accent-dim);border:1px solid var(--accent-border);color:var(--accent);border-radius:5px;padding:3px 10px;font-size:11px;cursor:pointer;white-space:nowrap">Copy</button>
</div>`;
}

async function verifyChatKey() {
  const provider = document.getElementById('chatProvider').value;
  const model = normalizeModelValue(document.getElementById('chatModel').value.trim());
  const apiKey = document.getElementById('chatApiKey').value.trim();
  const baseUrl = document.getElementById('chatBaseUrl').value.trim();
  const statusDiv = document.getElementById('chatKeyStatus');
  
  if (provider !== 'ollama' && !apiKey) {
    statusDiv.style.color = 'var(--danger)';
    statusDiv.textContent = 'API key is required for ' + provider + '.';
    statusDiv.style.display = 'block';
    return;
  }
  
  statusDiv.style.color = 'var(--warn)';
  statusDiv.textContent = provider === 'ollama'
    ? 'Testing Ollama connection...'
    : ('Testing ' + provider + ' API key...');
  statusDiv.style.display = 'block';
  
  const testPrompt = 'Say "OK" and nothing else.';
  const d = await api('/api/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      provider,
      model,
      api_key: apiKey,
      base_url: baseUrl,
      prompt: testPrompt,
    }),
  });
  
  if (d && d.ok) {
    statusDiv.style.color = 'var(--success)';
    statusDiv.textContent = provider === 'ollama'
      ? 'Ollama connection verified! Response: ' + (d.reply ? d.reply.substring(0, 100) : 'OK')
      : ('API key verified! Response: ' + (d.reply ? d.reply.substring(0, 100) : 'OK'));
  } else {
    statusDiv.style.color = 'var(--danger)';
    statusDiv.textContent = 'Verification failed: ' + (d ? d.error : 'No response') + '. Check provider, model, and network.';
  }
}

async function verifyAnalysisKey() {
  const provider = document.getElementById('analysisProvider').value;
  const model = normalizeModelValue(document.getElementById('analysisModel').value.trim());
  const apiKey = document.getElementById('analysisApiKey').value.trim();
  const statusDiv = document.getElementById('analysisKeyStatus');
  
  if (provider === 'none') {
    statusDiv.textContent = '';
    statusDiv.style.display = 'none';
    return;
  }
  
  if (provider !== 'ollama' && !apiKey) {
    statusDiv.style.color = 'var(--danger)';
    statusDiv.textContent = 'API key is required for ' + provider + '.';
    statusDiv.style.display = 'block';
    return;
  }
  
  statusDiv.style.color = 'var(--warn)';
  statusDiv.textContent = provider === 'ollama'
    ? 'Testing Ollama connection for analysis...'
    : ('Testing ' + provider + ' API key for analysis...');
  statusDiv.style.display = 'block';
  
  const testPrompt = 'Say "OK" and nothing else.';
  const d = await api('/api/chat', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      provider,
      model,
      api_key: apiKey,
      prompt: testPrompt,
    }),
  });
  
  if (d && d.ok) {
    statusDiv.style.color = 'var(--success)';
    statusDiv.textContent = provider === 'ollama'
      ? 'Ollama connection verified for analysis!'
      : 'API key verified for analysis!';
  } else {
    statusDiv.style.color = 'var(--danger)';
    statusDiv.textContent = 'Verification failed: ' + (d ? d.error : 'No response') + '. Check provider, model, and network.';
  }
}

async function chatAsk() {
  const prompt = document.getElementById('chatPrompt').value.trim();
  if (!prompt) return;
  const out = document.getElementById('chatOut');
  const sendBtn = document.getElementById('chatSendBtn');
  chatHistory.push({role:'user', content: prompt});
  chatHistory = chatHistory.slice(-CHAT_MAX_TURNS * 2);
  renderChat('Thinking… 0s');
  if (sendBtn) { sendBtn.disabled = true; sendBtn.textContent = '…'; }
  document.getElementById('chatPrompt').value = '';

  // Elapsed-time counter so user knows it's working (Ollama on CPU can be slow).
  const t0 = Date.now();
  const ticker = setInterval(() => {
    renderChat(`Thinking… ${Math.round((Date.now()-t0)/1000)}s`);
  }, 1000);

  const controller = new AbortController();
  const abort = setTimeout(() => controller.abort(), 660000); // 11 min hard cap

  let d = null;
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Flowtrack-Token': FLOWTRACK_TOKEN},
      body: JSON.stringify({
        provider: document.getElementById('chatProvider').value,
        model: normalizeModelValue(document.getElementById('chatModel').value.trim()),
        api_key: document.getElementById('chatApiKey').value.trim(),
        base_url: document.getElementById('chatBaseUrl').value.trim(),
        history: chatHistory.slice(0, -1),
        prompt,
      }),
      signal: controller.signal,
    });
    d = await resp.json();
  } catch(e) {
    d = null;
  } finally {
    clearInterval(ticker);
    clearTimeout(abort);
    if (sendBtn) { sendBtn.disabled = false; sendBtn.textContent = 'Send'; }
  }

  if (!d) {
    chatHistory.push({role:'assistant', content:'Request timed out or no response from backend. For Ollama on CPU, try a shorter prompt or switch to a cloud provider.'});
    renderChat();
    return;
  }
  if (d.ok) {
    chatHistory.push({role:'assistant', content:d.reply});
    renderChat();
  } else {
    const errMsg = d.error || 'Chat failed.';
    chatHistory.push({role:'assistant', content:errMsg});
    renderChat();
  }
}

window.addEventListener('DOMContentLoaded', () => {
  const prompt = document.getElementById('chatPrompt');
  const chatApiKey = document.getElementById('chatApiKey');
  const chatBaseUrl = document.getElementById('chatBaseUrl');
  const analysisApiKey = document.getElementById('analysisApiKey');
  if (prompt) {
    prompt.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        chatAsk();
      }
    });
  }
  if (chatApiKey) {
    chatApiKey.addEventListener('change', () => updateModelList('chat', true));
    chatApiKey.addEventListener('blur', () => updateModelList('chat', true));
  }
  if (chatBaseUrl) {
    chatBaseUrl.addEventListener('change', () => updateModelList('chat', true));
    chatBaseUrl.addEventListener('blur', () => updateModelList('chat', true));
  }
  if (analysisApiKey) {
    analysisApiKey.addEventListener('change', () => updateModelList('analysis', true));
    analysisApiKey.addEventListener('blur', () => updateModelList('analysis', true));
  }
});

// ── Backup Date Range (new feature) ────────────────────────────────────────────
function updateBackupDateUI() {
  const backupType = document.getElementById('backupType').value;
  const dateRangeDiv = document.getElementById('dateRangeDiv');
  dateRangeDiv.style.display = backupType === 'custom' ? 'flex' : 'none';
}

function updateUploadUI() {
  const provider = document.getElementById('uploadProvider').value;
  const targetInput = document.getElementById('uploadTarget');
  const uploadBtn = document.getElementById('uploadBtn');
  
  if (provider === 'none') {
    targetInput.style.display = 'none';
    uploadBtn.style.display = 'none';
  } else {
    targetInput.style.display = 'block';
    uploadBtn.style.display = 'block';
    if (provider === 'gist') {
      targetInput.placeholder = 'GitHub personal access token (required)';
      targetInput.type = 'password';
    } else if (provider === 'webhook') {
      targetInput.placeholder = 'Webhook URL (required)';
      targetInput.type = 'text';
    }
  }
}

async function downloadBackup() {
  const backupType = document.getElementById('backupType').value;
  const msg = document.getElementById('backupMsg');
  msg.style.color = 'var(--muted)';
  msg.textContent = '⏳ Preparing download...';
  
  try {
    let startDate = '';
    let endDate = '';
    
    if (backupType === 'custom') {
      startDate = document.getElementById('backupStartDate').value;
      endDate = document.getElementById('backupEndDate').value;
      if (!startDate || !endDate) {
        msg.textContent = '✗ Please select both start and end dates.';
        msg.style.color = 'var(--danger)';
        return;
      }
    }
    
    const response = await fetch('/api/backup-download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Flowtrack-Token': FLOWTRACK_TOKEN},
      body: JSON.stringify({backup_type: backupType, start_date: startDate, end_date: endDate}),
    });
    
    if (!response.ok) {
      const err = await response.json();
      msg.textContent = '✗ Download failed: ' + (err.error || 'Unknown error');
      msg.style.color = 'var(--danger)';
      return;
    }
    
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `flowtrack-backup-${new Date().toISOString().slice(0,10)}.jsonl`;
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    document.body.removeChild(a);
    
    msg.textContent = '✓ Backup downloaded successfully!';
    msg.style.color = 'var(--success)';
  } catch (err) {
    msg.textContent = '✗ Download error: ' + err.message;
    msg.style.color = 'var(--danger)';
  }
}

async function uploadBackup() {
  const backupType = document.getElementById('backupType').value;
  const provider = document.getElementById('uploadProvider').value;
  const credential = document.getElementById('uploadTarget').value.trim();
  const msg = document.getElementById('backupMsg');
  msg.style.color = 'var(--muted)';
  msg.textContent = '⏳ Uploading...';
  
  if (!credential) {
    msg.textContent = '✗ Please enter ' + (provider === 'gist' ? 'GitHub token' : 'webhook URL');
    msg.style.color = 'var(--danger)';
    return;
  }
  
  try {
    let startDate = '';
    let endDate = '';
    
    if (backupType === 'custom') {
      startDate = document.getElementById('backupStartDate').value;
      endDate = document.getElementById('backupEndDate').value;
      if (!startDate || !endDate) {
        msg.textContent = '✗ Please select both start and end dates.';
        msg.style.color = 'var(--danger)';
        return;
      }
    }
    
    const payload = {
      backup_type: backupType,
      start_date: startDate,
      end_date: endDate,
      provider: provider,
      credential: credential,
    };
    
    const response = await fetch('/api/backup-upload', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Flowtrack-Token': FLOWTRACK_TOKEN},
      body: JSON.stringify(payload),
    });
    
    const data = await response.json();
    if (data.ok) {
      msg.textContent = '✓ ' + (data.message || 'Uploaded successfully!');
      if (data.url) msg.textContent += ' View: ' + data.url;
      msg.style.color = 'var(--success)';
    } else {
      msg.textContent = '✗ Upload failed: ' + (data.error || 'Unknown error');
      msg.style.color = 'var(--danger)';
    }
  } catch (err) {
    msg.textContent = '✗ Upload error: ' + err.message;
    msg.style.color = 'var(--danger)';
  }
}

// ── Modal ─────────────────────────────────────────────────────────────────────
let modalReturnFocus = null;
function openModal(src, label = 'Screenshot preview') {
  modalReturnFocus = document.activeElement;
  const image = document.getElementById('modalImg');
  const modal = document.getElementById('modal');
  image.src = src;
  image.alt = label;
  modal.classList.add('open');
  modal.setAttribute('aria-hidden', 'false');
  document.getElementById('modalClose').focus();
}
function closeModal() {
  const modal = document.getElementById('modal');
  const image = document.getElementById('modalImg');
  modal.classList.remove('open');
  modal.setAttribute('aria-hidden', 'true');
  image.src = '';
  image.alt = '';
  if (modalReturnFocus && modalReturnFocus.focus) modalReturnFocus.focus();
}
document.addEventListener('keydown', e => {
  const modal = document.getElementById('modal');
  if (!modal.classList.contains('open')) return;
  if (e.key === 'Escape') closeModal();
  if (e.key === 'Tab') {
    e.preventDefault();
    document.getElementById('modalClose').focus();
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────
function refresh() { fetchStatus(); fetchLogs(); }

fetchStatus();
fetchLogs();
fetchShots();
updateBackupDateUI();
updateUploadUI();
updateChatPlaceholders();
updateAnalysisPlaceholders();
checkAutoStart();
refreshOllamaBar('chat');
refreshOllamaBar('analysis');
updateModelList('chat');
updateModelList('analysis');

// Load latest saved report on first open
api('/api/analysis').then(d => {
  if (d && d.status !== 'idle') setAnalysisUI(d.status, d.output);
});

setInterval(refresh, 3000);
setInterval(fetchShots, 8000);
</script>
</body>
</html>"""


# ── HTTP Handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *_):
        pass  # silence access logs

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'",
        )

    def _json(self, data: dict | list, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: str) -> None:
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(b)

    def _launcher_proof(self, nonce: str) -> None:
        proof = hmac.new(
            DASHBOARD_TOKEN.encode("ascii"), nonce.encode("ascii"), hashlib.sha256
        ).hexdigest()
        body = json.dumps({"proof": proof}).encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Private file launchers have an opaque `null` origin. The proof
        # authenticates this server before the launcher sends its bearer token.
        self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Vary", "Origin")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _require_authentication(self, parsed=None, *, allow_query: bool = False) -> bool:
        candidate = self.headers.get(DASHBOARD_TOKEN_HEADER, "")
        if allow_query and parsed is not None and not candidate:
            candidate = parse_qs(parsed.query).get("token", [""])[0]
        if candidate and hmac.compare_digest(candidate, DASHBOARD_TOKEN):
            return True
        self._json(
            {
                "ok": False,
                "error": "Dashboard authentication required. Open it with the URL printed by dashboard.py or run the flowtrack command.",
            },
            code=401,
        )
        return False

    def _body(self) -> dict:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc
        if length < 0:
            raise ValueError("Content-Length cannot be negative.")
        if length > MAX_REQUEST_BYTES:
            raise OverflowError(f"Request body exceeds {MAX_REQUEST_BYTES} bytes.")
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        return body

    def _allow_local_request(self) -> bool:
        port = int(self.server.server_address[1])
        if _local_host_and_port(self.headers.get("Host", ""), port):
            return True
        self._json({"ok": False, "error": "Only direct localhost requests are allowed."}, code=403)
        return False

    def do_GET(self) -> None:
        if not self._allow_local_request():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/launcher-proof":
            nonce = parse_qs(parsed.query).get("nonce", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce):
                self._json({"ok": False, "error": "Invalid launcher nonce."}, code=400)
                return
            self._launcher_proof(nonce)
            return
        if not self._require_authentication(parsed, allow_query=True):
            return
        path   = parsed.path
        qs     = parse_qs(parsed.query)

        if path == "/":
            self._html(HTML.replace("__FLOWTRACK_TOKEN__", json.dumps(DASHBOARD_TOKEN)))

        elif path == "/api/status":
            svc  = service_status()
            stor = storage_stats()
            self._json({**svc, **stor})

        elif path == "/api/ollama":
            # Report Ollama state: running, installed models, currently loaded models.
            try:
                with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
                    tags = json.loads(r.read())
                models = [m.get("name") for m in tags.get("models", []) if m.get("name")]
                loaded: list[str] = []
                try:
                    with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=3) as r:
                        ps = json.loads(r.read())
                    loaded = [m.get("name", "") for m in ps.get("models", []) if m.get("name")]
                except Exception:
                    pass
                self._json({"running": True, "models": models, "loaded": loaded})
            except Exception:
                self._json({"running": False, "models": [], "loaded": []})

        elif path == "/api/autostart":
            if platform.system() != "Linux":
                self._json({"enabled": False, "supported": False})
                return
            result = _sh(["systemctl", "--user", "is-enabled", SERVICE_NAME])
            self._json({"enabled": result.strip() == "enabled", "supported": True})

        elif path == "/api/logs":
            try:
                limit = max(1, min(int(qs.get("limit", ["100"])[0]), 500))
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "limit must be an integer from 1 to 500."}, code=400)
                return
            entries = today_events()
            self._json({
                "total":       len(entries),
                "entries":     entries[-limit:],
                "focus_score": _quick_focus(entries),
            })

        elif path == "/api/screenshots":
            self._json(recent_screenshots(12))

        elif path.startswith("/screenshots/"):
            # ── Security: strict filename validation prevents path traversal ──
            name = path[len("/screenshots/"):]
            if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:jpe?g|png)", name, re.IGNORECASE):
                self._json({"ok": False, "error": "Invalid screenshot filename."}, code=400)
                return
            f = SCREENSHOTS_DIR / name
            if not f.exists() or not f.is_file():
                self._json({"ok": False, "error": "Screenshot not found."}, code=404)
                return
            try:
                data = f.read_bytes()
            except OSError:
                self._json({"ok": False, "error": "Screenshot could not be read."}, code=404)
                return
            self.send_response(200)
            content_type = "image/png" if f.suffix.lower() == ".png" else "image/jpeg"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600")
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)

        elif path == "/api/analysis":
            if _result["status"] == "idle":
                report = latest_report()
                if report:
                    self._json({"status": "done", "output": report})
                    return
            self._json(_result)

        else:
            self._json({"ok": False, "error": "Not found."}, code=404)

    def do_POST(self) -> None:
        if not self._allow_local_request():
            return
        if not self._require_authentication():
            return
        port = int(self.server.server_address[1])
        origin = self.headers.get("Origin", "")
        if origin and not _local_origin(origin, port):
            self._json({"ok": False, "error": "Cross-origin requests are not allowed."}, code=403)
            return
        if self.headers.get_content_type() != "application/json":
            self._json({"ok": False, "error": "Content-Type must be application/json."}, code=415)
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
        except OverflowError as exc:
            self._json({"ok": False, "error": str(exc)}, code=413)
            return
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, code=400)
            return

        if path == "/api/ollama":
            action = str(body.get("action", ""))
            model = str(body.get("model", "llama3")).strip()
            if action == "start":
                ok = _ensure_ollama_running()
                self._json({"ok": ok, "message": "Ollama started." if ok else "Failed to start Ollama. Is it installed?"})
            elif action == "unload":
                # Sending keep_alive=0 tells Ollama to immediately evict the model from RAM/VRAM.
                try:
                    models = [model] if model and model != "__auto__" else []
                    if not models:
                        with urllib.request.urlopen("http://localhost:11434/api/ps", timeout=5) as response:
                            running = json.loads(response.read())
                        models = [
                            item.get("name", "")
                            for item in running.get("models", [])
                            if isinstance(item, dict) and item.get("name")
                        ]
                    for loaded_model in models:
                        payload = json.dumps({"model": loaded_model, "keep_alive": 0}).encode()
                        req = urllib.request.Request(
                            "http://localhost:11434/api/generate",
                            data=payload,
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        urllib.request.urlopen(req, timeout=10).close()
                    message = "No model is currently loaded." if not models else f"Unloaded {', '.join(models)} — RAM freed."
                    self._json({"ok": True, "message": message})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)})
            else:
                self._json({"ok": False, "error": "Unknown action. Use start or unload."}, code=400)

        elif path == "/api/service":
            action = body.get("action", "")
            # Whitelist only safe systemctl actions
            if action in ("start", "stop", "restart"):
                if platform.system() != "Linux":
                    self._json({"ok": False, "error": "Service controls use systemd and are Linux-only. Run tracker manually on this OS."}, code=400)
                    return
                try:
                    subprocess.Popen(
                        ["systemctl", "--user", action, SERVICE_NAME],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError as exc:
                    self._json({"ok": False, "error": f"Service action failed: {exc}"}, code=500)
                    return
                self._json({"ok": True})
                return
            elif action in ("enable", "disable"):
                if platform.system() != "Linux":
                    self._json({"ok": False, "error": "Auto-start uses systemd and is Linux-only."}, code=400)
                    return
                units = [SERVICE_NAME, "flowtrack-dashboard.service"]
                failures = []
                for unit in units:
                    try:
                        result = subprocess.run(
                            ["systemctl", "--user", action, unit],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                        if result.returncode != 0:
                            failures.append(unit)
                    except OSError as exc:
                        self._json({"ok": False, "error": f"Auto-start change failed: {exc}"}, code=500)
                        return
                if failures:
                    self._json({"ok": False, "error": f"Could not {action}: {', '.join(failures)}"}, code=500)
                    return
                enabled = action == "enable"
                self._json({"ok": True, "enabled": enabled})
                return
            self._json({"ok": False, "error": "Unknown service action."}, code=400)

        elif path == "/api/analyze":
            provider = str(body.get("provider", ""))
            model = str(body.get("model", ""))
            api_key = str(body.get("api_key", ""))
            started = _start_analysis(bool(body.get("ai", False)), provider=provider, model=model, api_key=api_key)
            if started:
                self._json({"ok": True, "status": "started"})
            else:
                self._json({"ok": False, "error": _result.get("output", "Analysis is already running.")}, code=409)

        elif path == "/api/open-screenshots":
          ok, error = _open_folder(SCREENSHOTS_DIR)
          if not ok:
            self._json({"ok": False, "error": error}, code=500)
            return
          self._json({"ok": True})

        elif path == "/api/sync-json":
          provider = str(body.get("provider", "gist"))
          target = str(body.get("target", ""))
          api_key = str(body.get("api_key", ""))
          result = sync_json_to_cloud(provider=provider, target=target, api_key=api_key)
          self._json(result, code=200 if result.get("ok") else 400)

        elif path == "/api/models":
          provider = str(body.get("provider", "ollama")).strip().lower()
          api_key = str(body.get("api_key", ""))
          base_url = str(body.get("base_url", ""))
          models, err = fetch_provider_models(provider=provider, api_key=api_key, base_url=base_url)
          if err:
            self._json({"ok": False, "models": [], "error": err})
          else:
            self._json({"ok": True, "models": models})

        elif path == "/api/chat":
          provider = str(body.get("provider", "ollama")).strip().lower()
          model = str(body.get("model", "")).strip()
          api_key = str(body.get("api_key", ""))
          base_url = str(body.get("base_url", ""))
          if not model:
            model = CHAT_PROVIDER_DEFAULT_MODELS.get(provider, "")
          if not model:
            self._json({"ok": False, "error": f"Unsupported provider: {provider}"}, code=400)
            return
          prompt = str(body.get("prompt", "")).strip()
          if not prompt:
            self._json({"ok": False, "error": "Prompt is empty."}, code=400)
            return
          raw_history = body.get("history", [])
          history: list[dict] = []
          if isinstance(raw_history, list):
            for item in raw_history[-16:]:
              if not isinstance(item, dict):
                continue
              role = str(item.get("role", "")).lower().strip()
              content = str(item.get("content", "")).strip()
              if role not in ("user", "assistant") or not content:
                continue
              history.append({"role": role, "content": content[:2400]})

          # Add short context so model responses stay grounded.
          entries = today_events()
          context = {
            "events_today": len(entries),
            "focus_score": _quick_focus(entries),
            "top_titles": [e.get("title", "")[:120] for e in entries[-12:]],
          }
          convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history) if history else "(no prior turns)"
          final_prompt = (
            "You are Flowtrack Coach.\n"
            "Scope rules: only discuss user productivity behavior, app/window usage patterns, distraction control, routines, and Flowtrack project usage.\n"
            "If user asks unrelated topics, briefly refuse and ask a related follow-up question.\n"
            "Keep answers practical, concise, and actionable.\n\n"
            f"Context JSON:\n{json.dumps(context, ensure_ascii=False)}\n\n"
            f"Conversation so far:\n{convo}\n\n"
            f"User question:\n{prompt}\n"
          )
          reply, err = query_llm(
            final_prompt,
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            history=history,
          )
          if reply:
            self._json({"ok": True, "reply": reply})
          else:
            self._json({"ok": False, "error": err or "LLM request failed. Check provider, model, API key, and network."})

        elif path == "/api/backup-download":
          backup_type = str(body.get("backup_type", "today"))
          start_date = str(body.get("start_date", ""))
          end_date = str(body.get("end_date", ""))
          data = logs_for_scope(backup_type, start_date, end_date)
          if "error" in data:
            self._json({"ok": False, "error": data.get("error")}, code=400)
            return
          content = logs_as_jsonl(data.get("logs", {}))
          self.send_response(200)
          self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
          self.send_header("Content-Disposition", f'attachment; filename="flowtrack-backup-{datetime.datetime.now().strftime("%Y-%m-%d")}.jsonl"')
          self.send_header("Content-Length", str(len(content)))
          self._security_headers()
          self.end_headers()
          self.wfile.write(content)

        elif path == "/api/backup-upload":
          backup_type = str(body.get("backup_type", "today"))
          start_date = str(body.get("start_date", ""))
          end_date = str(body.get("end_date", ""))
          provider = str(body.get("provider", "gist"))
          credential = str(body.get("credential", ""))
          
          if not credential:
            self._json({"ok": False, "error": f"Missing credential for {provider}"}, code=400)
            return
          
          data = logs_for_scope(backup_type, start_date, end_date)
          
          if "error" in data:
            self._json({"ok": False, "error": data.get("error")}, code=400)
            return
          logs = data.get("logs", {})
          
          target = credential if provider == "webhook" else ""
          api_key = credential if provider == "gist" else ""
          result = sync_json_to_cloud(provider, target, api_key, logs=logs)
          self._json(result, code=200 if result.get("ok") else 400)

        elif path == "/api/backup-date-range":
          backup_type = str(body.get("backup_type", "today"))
          start_date = str(body.get("start_date", ""))
          end_date = str(body.get("end_date", ""))
          provider = str(body.get("provider", "gist"))
          target = str(body.get("target", ""))
          api_key = str(body.get("api_key", ""))
          
          data = logs_for_scope(backup_type, start_date, end_date)
          if "error" in data:
            self._json({"ok": False, "error": data.get("error")}, code=400)
            return
          result = sync_json_to_cloud(provider, target, api_key, logs=data.get("logs", {}))
          self._json(result, code=200 if result.get("ok") else 400)

        else:
            self._json({"ok": False, "error": "Not found."}, code=404)

    def do_OPTIONS(self) -> None:
        if not self._allow_local_request():
            return
        if not self._require_authentication():
            return
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self._security_headers()
        self.end_headers()


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    server = ThreadedServer((HOST, PORT), Handler)
    url    = f"http://{HOST}:{PORT}"
    print(f"Flowtrack Dashboard → {url}")
    print("Press Ctrl+C to stop.")
    # Manual runs open a browser; the systemd unit opts out to avoid opening a
    # new tab on every service restart.
    no_browser = os.environ.get("FLOWTRACK_NO_BROWSER", "").lower() in {"1", "true", "yes"}
    if no_browser:
        print(f"Use the flowtrack command, or open the private launcher at {DASHBOARD_LAUNCHER}.")
    else:
        print(f"Authenticated launcher → {DASHBOARD_LAUNCHER}")
        launcher_url = DASHBOARD_LAUNCHER.resolve().as_uri()
        threading.Thread(target=_open_browser, args=(launcher_url,), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
