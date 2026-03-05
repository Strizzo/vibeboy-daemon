# vibeboy-daemon

HTTP API for managing tmux sessions and Claude Code on remote servers.

## Setup

```bash
pip install -r requirements.txt
cp config.example.json config.json  # edit as needed
```

## Usage

```bash
python vibeboy_daemon.py
```

Listens on `127.0.0.1:8766` by default. Use SSH tunneling to access remotely (the [vibeboy-cartridge](https://github.com/Strizzo/vibeboy-cartridge) has built-in SSH tunnel support).

## API

### `GET /api/state`

Returns all tmux sessions with terminal output.

### `POST /api/action`

Execute an action on a session. Body:

```json
{
    "action": "send_keys|send_response|interrupt|new_session|kill_session",
    "session_id": "session-name",
    "payload": {}
}
```

### `GET /api/health`

Health check.
