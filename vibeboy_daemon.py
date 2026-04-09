#!/usr/bin/env python3
"""VibeBoy Daemon - HTTP API for managing tmux sessions and Claude Code."""

import json
import subprocess
import sys
from pathlib import Path

from flask import Flask, jsonify, request

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
    """List all tmux sessions with metadata."""
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
        sessions[name] = {
            "name": name,
            "created": int(parts[1]) if parts[1].isdigit() else 0,
            "windows": int(parts[2]) if parts[2].isdigit() else 1,
            "attached": parts[3] == "1",
            "terminal": capture_pane(name),
        }
    return sessions


def capture_pane(session_name: str) -> str:
    """Capture visible terminal output from a session's active pane."""
    try:
        return tmux("capture-pane", "-t", session_name, "-p")
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
