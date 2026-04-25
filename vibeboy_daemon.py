#!/usr/bin/env python3
"""VibeBoy Daemon - HTTP API for managing tmux sessions and Claude Code."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import threading
from pathlib import Path

from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Terminal output cleaning
# ---------------------------------------------------------------------------

# Match ANSI escape sequences (colors, cursor movement, OSC, etc.)
_ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[a-zA-Z]'
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'
    r'|\x1b[()][AB012]'
    r'|\x1b[>=<]'
    r'|\x1b\[[\?]?[0-9;]*[hlm]'
    r'|\x1b.'
    r'|\x0f|\x0e'
    r'|\r'
)

_UNICODE_TO_ASCII = {
    '─': '-', '│': '|', '┌': '+', '┐': '+', '└': '+', '┘': '+',
    '├': '+', '┤': '+', '┬': '+', '┴': '+', '┼': '+',
    '━': '-', '┃': '|', '┏': '+', '┓': '+', '┗': '+', '┛': '+',
    '•': '*', '·': '*', '●': '*', '○': '*', '◯': '*',
    '→': '>', '▶': '>', '►': '>', '▸': '>', '➜': '>',
    '←': '<', '◀': '<', '◄': '<', '◂': '<',
    '↑': '^', '▲': '^', '⬆': '^',
    '↓': 'v', '▼': 'v', '⬇': 'v',
    '✓': '[ok]', '✔': '[ok]', '☑': '[ok]',
    '✗': '[x]', '✘': '[x]', '✕': '[x]', '☒': '[x]',
    '…': '...', '⋯': '...',
    '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
    '\u2013': '-', '\u2014': '-',
    '❯': '>', '❮': '<',
    '✻': '*', '✼': '*', '✽': '*',
}


def clean_terminal(text: str) -> str:
    """Clean terminal output: strip ANSI, ensure pure ASCII."""
    text = _ANSI_RE.sub('', text)
    result = []
    for ch in text:
        if ch in _UNICODE_TO_ASCII:
            result.append(_UNICODE_TO_ASCII[ch])
        elif ch == '\n' or ch == '\t':
            result.append(ch)
        elif 32 <= ord(ch) < 127:
            result.append(ch)
        # drop other non-ASCII silently
    return ''.join(result)


# ---------------------------------------------------------------------------
# LLM-based prompt suggestions (via claude CLI)
# ---------------------------------------------------------------------------

# Find claude CLI in common locations
def _find_claude_cli():
    candidates = [
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/.npm-global/bin/claude"),
        "/usr/local/bin/claude",
        "/usr/bin/claude",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # Fallback: check PATH
    try:
        result = subprocess.run(
            ["bash", "-lc", "command -v claude"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


CLAUDE_CLI = os.environ.get('VIBEBOY_CLAUDE_CLI') or _find_claude_cli()
LLM_TIMEOUT = int(os.environ.get('VIBEBOY_LLM_TIMEOUT', '20'))

# Cache: session_name -> (terminal_hash, suggestions, timestamp)
_suggestion_cache = {}
_suggestion_lock = threading.Lock()
_inflight = set()
_inflight_lock = threading.Lock()

CACHE_TTL_SECONDS = 60


def _terminal_hash(terminal: str) -> str:
    """Hash the meaningful part of terminal content (last 2KB)."""
    return hashlib.md5(terminal[-2000:].encode('utf-8', errors='replace')).hexdigest()


def _fetch_llm_suggestions(session_name: str, terminal: str):
    """Run `claude -p` to generate contextual prompt suggestions."""
    if not CLAUDE_CLI:
        return None

    # Use a generous slice of terminal content for rich conversational context
    context = terminal[-8000:]
    prompt = (
        "You are helping me work with my Claude Code session remotely from a "
        "small handheld device where typing is hard. Below is the recent terminal "
        "output of my Claude Code session, including both my messages (lines "
        "starting with '>') and Claude's responses (often starting with '*' or "
        "containing tool output).\n\n"
        "Suggest 6 SPECIFIC next messages I might want to send to Claude. "
        "Requirements:\n"
        "- Each prompt must be a complete, useful message (1-2 sentences)\n"
        "- Length: between 30 and 140 characters each\n"
        "- Make them genuinely contextual: reference specific things from the "
        "conversation (file names, errors, decisions, options Claude offered)\n"
        "- Mix types: follow-up questions, refinements, next steps, course "
        "corrections, requests for explanations\n"
        "- Avoid generic prompts like 'continue' or 'explain' unless that's "
        "really the most useful thing\n\n"
        "Output ONLY a JSON array of strings. Nothing before or after.\n\n"
        f"Session output:\n```\n{context}\n```\n\nJSON array:"
    )

    try:
        result = subprocess.run(
            [CLAUDE_CLI, "-p", prompt],
            capture_output=True, text=True, timeout=LLM_TIMEOUT,
        )
        if result.returncode != 0:
            print(f"claude CLI failed ({result.returncode}): {result.stderr[:200]}",
                  file=sys.stderr)
            return None

        text = result.stdout.strip()
        # Extract JSON array (greedy to capture multi-line strings)
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if not m:
            return None

        suggestions = json.loads(m.group())
        if not isinstance(suggestions, list):
            return None

        opts = []
        for s in suggestions[:6]:
            if isinstance(s, str) and s.strip():
                # Hard cap at 200 chars to prevent runaway responses
                opts.append({
                    "text": s.strip()[:200],
                    "category": "custom",
                })
        return opts if opts else None

    except subprocess.TimeoutExpired:
        print(f"claude CLI timed out for {session_name}", file=sys.stderr)
    except Exception as e:
        print(f"claude CLI error for {session_name}: {e}", file=sys.stderr)
    return None


def _refresh_suggestions_async(session_name: str, terminal: str):
    """Refresh LLM suggestions in a background thread (non-blocking)."""
    with _inflight_lock:
        if session_name in _inflight:
            return
        _inflight.add(session_name)

    def _worker():
        try:
            opts = _fetch_llm_suggestions(session_name, terminal)
            if opts:
                h = _terminal_hash(terminal)
                with _suggestion_lock:
                    _suggestion_cache[session_name] = (h, opts, time.time())
        finally:
            with _inflight_lock:
                _inflight.discard(session_name)

    threading.Thread(target=_worker, daemon=True).start()


def get_user_input_suggestions(session_name: str, terminal: str):
    """Return LLM-generated suggestions for a Claude Code waiting state."""
    fallback = [
        {"text": "continue", "category": "custom"},
        {"text": "what should I do next?", "category": "custom"},
        {"text": "explain", "category": "custom"},
        {"text": "show the result", "category": "custom"},
    ]

    if not CLAUDE_CLI:
        return fallback

    h = _terminal_hash(terminal)
    now = time.time()
    with _suggestion_lock:
        cached = _suggestion_cache.get(session_name)

    # Cached and fresh -> return cached
    if cached and cached[0] == h and now - cached[2] < CACHE_TTL_SECONDS:
        return cached[1]

    # Cached but stale or different content -> kick off refresh, return cached or fallback
    _refresh_suggestions_async(session_name, terminal)
    if cached:
        return cached[1]
    return fallback


# ---------------------------------------------------------------------------
# Session state analysis
# ---------------------------------------------------------------------------

def analyze_session(terminal: str, name: str) -> dict:
    """Analyze terminal content and return session metadata with smart actions."""
    if not terminal.strip():
        return {
            "session_type": "idle_shell",
            "status": "idle",
            "response_options": [
                {"text": "Refresh", "category": "custom", "keys": "Enter"},
                {"text": "Interrupt", "category": "danger", "keys": "C-c"},
            ],
        }

    lines = terminal.split('\n')
    last_line = lines[-1].rstrip() if lines else ''
    full_text = terminal.lower()

    # Detect Claude Code by its UI footer
    is_claude_ui = (
        'bypass permissions on' in full_text or
        'shift+tab to cycle' in full_text or
        'ctrl-g to edit prompt' in full_text or
        'esc to interrupt' in full_text
    )

    # Plain shell session (not Claude Code)
    if not is_claude_ui:
        if re.search(r'[$#]\s*$', last_line):
            return {
                "session_type": "idle_shell",
                "status": "idle",
                "response_options": [
                    {"text": "ls -la", "category": "custom", "keys": "ls -la"},
                    {"text": "git status", "category": "custom", "keys": "git status"},
                    {"text": "git log --oneline -10", "category": "custom", "keys": "git log --oneline -10"},
                    {"text": "claude", "category": "custom", "keys": "claude"},
                    {"text": "Interrupt", "category": "danger", "keys": "C-c"},
                ],
            }
        else:
            return {
                "session_type": "running_process",
                "status": "running",
                "response_options": [
                    {"text": "Interrupt (Ctrl+C)", "category": "danger", "keys": "C-c"},
                    {"text": "Suspend (Ctrl+Z)", "category": "custom", "keys": "C-z"},
                ],
            }

    # Claude Code session — detect specific state

    # Permission prompt: numbered list with Yes/No/Allow/Deny variants
    has_numbered_options = bool(re.search(
        r'^\s*[1-9]\.\s+(Yes|No|Approve|Deny|Allow)',
        terminal, re.MULTILINE | re.IGNORECASE
    ))
    has_do_you_want = 'do you want to' in full_text

    if has_numbered_options or has_do_you_want:
        return {
            "session_type": "claude_code",
            "status": "permission",
            "response_options": [
                {"text": "Approve (1)", "category": "approve", "keys": "1"},
                {"text": "Approve always (2)", "category": "approve", "keys": "2"},
                {"text": "Deny (3)", "category": "deny", "keys": "3"},
                {"text": "Cancel (Esc)", "category": "danger", "keys": "Escape"},
            ],
        }

    # Thinking / processing state
    is_thinking = (
        'esc to interrupt' in full_text or
        re.search(r'\b(thinking|processing|generating|searching)\b', full_text) is not None
    )
    if is_thinking and 'esc to interrupt' in full_text:
        return {
            "session_type": "claude_code",
            "status": "thinking",
            "response_options": [
                {"text": "Interrupt (Esc)", "category": "danger", "keys": "Escape"},
                {"text": "Force kill (Ctrl+C)", "category": "danger", "keys": "C-c"},
            ],
        }

    # Default: Claude Code waiting for user input
    suggestions = get_user_input_suggestions(name, terminal)
    suggestions = list(suggestions)
    suggestions.append({"text": "Interrupt", "category": "danger", "keys": "C-c"})

    return {
        "session_type": "claude_code",
        "status": "waiting",
        "response_options": suggestions,
    }


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)

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
    result = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    return result.stdout.strip()


def tmux_ok(*args: str) -> bool:
    result = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    return result.returncode == 0


def list_sessions() -> dict:
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
    sessions = list_sessions()
    return jsonify({"sessions": sessions, "llm_enabled": CLAUDE_CLI is not None})


@app.route("/api/action", methods=["POST"])
def post_action():
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
        # Don't append Enter for control sequences or named keys
        named = ("Enter", "Up", "Down", "Left", "Right", "Tab", "Escape", "BSpace")
        if keys.startswith("C-") or keys.startswith("M-") or keys in named or len(keys) <= 2:
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
        tmux("send-keys", "-t", session_id, "C-c")
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
    return jsonify({"status": "ok", "llm_enabled": CLAUDE_CLI is not None, "claude_cli": CLAUDE_CLI})


def main():
    config = load_config()
    host = config["host"]
    port = config["port"]
    llm_status = f"enabled ({CLAUDE_CLI})" if CLAUDE_CLI else "disabled (claude CLI not found)"
    print(f"VibeBoy daemon listening on {host}:{port} (LLM suggestions: {llm_status})")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
