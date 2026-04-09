#!/usr/bin/env python3
"""VibeBoy Daemon - HTTP API for managing tmux sessions and Claude Code."""

import json
import re
import subprocess
import sys
from pathlib import Path

from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Terminal output cleaning
# ---------------------------------------------------------------------------

# Match ANSI escape sequences (colors, cursor movement, OSC, etc.)
_ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[a-zA-Z]'       # CSI sequences (colors, cursor)
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC sequences
    r'|\x1b[()][AB012]'             # Character set selection
    r'|\x1b[>=<]'                   # Keypad modes
    r'|\x1b\[[\?]?[0-9;]*[hlm]'    # Mode set/reset
    r'|\x1b.'                       # Any other escape
    r'|\x0f|\x0e'                   # SI/SO
    r'|\r'                          # Carriage returns
)


def clean_terminal(text: str) -> str:
    """Clean terminal output: strip ANSI, ensure pure ASCII."""
    text = _ANSI_RE.sub('', text)
    # Replace non-ASCII with approximations or strip
    result = []
    for ch in text:
        code = ord(ch)
        if ch == '\n' or ch == '\t':
            result.append(ch)
        elif 32 <= code < 127:
            result.append(ch)
        elif ch in ('\u2500', '\u2502', '\u250c', '\u2510', '\u2514', '\u2518',
                     '\u251c', '\u2524', '\u252c', '\u2534', '\u253c'):
            # Box drawing -> ASCII
            result.append({'─': '-', '│': '|', '┌': '+', '┐': '+',
                           '└': '+', '┘': '+', '├': '+', '┤': '+',
                           '┬': '+', '┴': '+', '┼': '+'}.get(ch, '+'))
        elif ch in ('•', '·', '●', '○'):
            result.append('*')
        elif ch in ('→', '▶', '►', '▸'):
            result.append('>')
        elif ch in ('←', '◀', '◄', '◂'):
            result.append('<')
        elif ch in ('↑', '▲'):
            result.append('^')
        elif ch in ('↓', '▼'):
            result.append('v')
        elif ch in ('✓', '✔'):
            result.append('[ok]')
        elif ch in ('✗', '✘', '✕'):
            result.append('[x]')
        elif ch in ('\u2026',):  # ellipsis
            result.append('...')
        elif ch in ('\u2018', '\u2019'):  # smart quotes
            result.append("'")
        elif ch in ('\u201c', '\u201d'):
            result.append('"')
        elif ch in ('\u2013', '\u2014'):  # dashes
            result.append('-')
        elif code >= 128:
            pass  # drop other non-ASCII silently
    return ''.join(result)


# ---------------------------------------------------------------------------
# Terminal analysis - detect prompts and suggest actions
# ---------------------------------------------------------------------------

def analyze_session(terminal: str, name: str) -> dict:
    """Analyze terminal content and return session metadata with smart actions."""
    lines = terminal.strip().split('\n') if terminal.strip() else []
    last_lines = lines[-10:] if lines else []
    last_text = '\n'.join(last_lines).lower()
    last_line = lines[-1].strip() if lines else ""

    session_type = "idle_shell"
    status = "idle"
    options = []

    # Detect Claude Code
    is_claude = any(
        x in last_text for x in ('claude', 'anthropic', 'thinking', 'tool_use')
    ) or any(
        x in name.lower() for x in ('claude', 'cc-', 'cc_')
    )

    # Detect Y/N or yes/no prompts
    yn_match = re.search(r'\[([yY]/[nN]|[nN]/[yY])\]', last_line) or \
               re.search(r'\(yes/no\)', last_line, re.IGNORECASE) or \
               re.search(r'\(y/n\)', last_line, re.IGNORECASE)

    # Detect generic question (ends with ?)
    is_question = last_line.endswith('?')

    # Detect shell prompt ($ or # at end, possibly with path)
    is_shell = bool(re.search(r'[$#]\s*$', last_line))

    # Detect running process (no prompt visible)
    has_activity = len(last_line) > 0 and not is_shell

    if is_claude:
        session_type = "claude_code"
        if yn_match or is_question:
            status = "waiting"
            options.append({"text": "Yes", "category": "approve"})
            options.append({"text": "No", "category": "deny"})
        elif 'thinking' in last_text or 'running' in last_text:
            status = "thinking"
        elif is_shell:
            status = "idle"
        else:
            status = "running"
    elif yn_match:
        session_type = "interactive_prompt"
        status = "waiting"
        options.append({"text": "y", "category": "approve"})
        options.append({"text": "n", "category": "deny"})
    elif is_question:
        session_type = "interactive_prompt"
        status = "waiting"
        options.append({"text": "yes", "category": "approve"})
        options.append({"text": "no", "category": "deny"})
    elif is_shell:
        session_type = "idle_shell"
        status = "idle"
    elif has_activity:
        session_type = "running_process"
        status = "running"

    # Always add common actions
    options.append({"text": "Interrupt (Ctrl+C)", "category": "danger", "keys": "C-c"})

    if session_type == "idle_shell":
        options.insert(0, {"text": "Run: ls -la", "category": "custom", "keys": "ls -la"})
        options.insert(0, {"text": "Run: git status", "category": "custom", "keys": "git status"})
        options.insert(0, {"text": "Run: htop", "category": "custom", "keys": "htop"})

    return {
        "session_type": session_type,
        "status": status,
        "response_options": options,
    }


app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {"host": "127.0.0.1", "port": 8766}


def load_config():
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return dict(DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------


def tmux(*args: str) -> str:
    """Run a tmux command and return stdout."""
    result = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    return result.stdout.strip()


def tmux_ok(*args: str) -> bool:
    """Run a tmux command and return whether it succeeded."""
    result = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    return result.returncode == 0


def list_sessions() -> dict:
    """List all tmux sessions with metadata and analysis."""
    try:
        raw = tmux(
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_created}\t#{session_windows}\t#{session_attached}",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}

    sessions = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name = parts[0]
        terminal = capture_pane(name)
        analysis = analyze_session(terminal, name)

        sessions[name] = {
            "name": name,
            "created": int(parts[1]) if parts[1].isdigit() else 0,
            "windows": int(parts[2]) if parts[2].isdigit() else 1,
            "attached": parts[3] == "1",
            "terminal": terminal,
            **analysis,
        }
    return sessions


def capture_pane(session_name: str) -> str:
    """Capture visible terminal output from a session's active pane."""
    try:
        raw = tmux("capture-pane", "-t", session_name, "-p")
        return clean_terminal(raw)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@app.route("/api/state", methods=["GET"])
def get_state():
    """Return full daemon state including all tmux sessions."""
    sessions = list_sessions()
    return jsonify({"sessions": sessions})


@app.route("/api/action", methods=["POST"])
def post_action():
    """Execute an action on a tmux session."""
    data = request.get_json(silent=True) or {}
    action = data.get("action", "")
    session_id = data.get("session_id", "")
    payload = data.get("payload", {})

    if not action:
        return jsonify({"error": "missing action"}), 400

    if action == "send_keys":
        keys = payload.get("keys", "")
        if not keys or not session_id:
            return jsonify({"error": "missing keys or session_id"}), 400
        # Don't add Enter for control sequences
        if keys.startswith("C-") or keys in ("Enter", "Up", "Down", "Left", "Right"):
            tmux("send-keys", "-t", session_id, keys)
        else:
            tmux("send-keys", "-t", session_id, keys, "Enter")
        return jsonify({"ok": True})

    elif action == "send_response":
        text = payload.get("text", "")
        if not session_id:
            return jsonify({"error": "missing session_id"}), 400
        tmux("send-keys", "-t", session_id, text, "Enter")
        return jsonify({"ok": True})

    elif action == "interrupt":
        if not session_id:
            return jsonify({"error": "missing session_id"}), 400
        tmux("send-keys", "-t", session_id, "C-c", "")
        return jsonify({"ok": True})

    elif action == "new_session":
        name = payload.get("name", "")
        if name:
            tmux_ok("new-session", "-d", "-s", name)
        else:
            tmux_ok("new-session", "-d")
        return jsonify({"ok": True})

    elif action == "kill_session":
        if not session_id:
            return jsonify({"error": "missing session_id"}), 400
        tmux_ok("kill-session", "-t", session_id)
        return jsonify({"ok": True})

    else:
        return jsonify({"error": f"unknown action: {action}"}), 400


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    config = load_config()
    host = config["host"]
    port = config["port"]
    print(f"VibeBoy daemon listening on {host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
