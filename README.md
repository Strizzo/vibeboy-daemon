# vibeboy-daemon

A lightweight HTTP API that lets you manage tmux sessions on a remote server from your [CartridgeOS](https://github.com/Strizzo/Cartridge) handheld via the [VibeBoy cartridge](https://github.com/Strizzo/vibeboy-cartridge).

## Quick Start

On your server (the machine running tmux sessions):

```bash
git clone https://github.com/Strizzo/vibeboy-daemon.git
cd vibeboy-daemon
pip install -r requirements.txt
python3 vibeboy_daemon.py
```

The daemon listens on `127.0.0.1:8766` by default. It only binds to localhost, so it's not exposed to the network. Use SSH tunneling from VibeBoy to reach it securely.

### Run as a service (recommended)

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/vibeboy.service << 'EOF'
[Unit]
Description=VibeBoy Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/vibeboy-daemon
ExecStart=/usr/bin/python3 vibeboy_daemon.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now vibeboy
loginctl enable-linger $USER   # keeps service running after logout
```

Verify it's running: `curl http://127.0.0.1:8766/api/health`

## Connecting from VibeBoy

1. Install VibeBoy on your CartridgeOS device from the Store
2. Copy your SSH key to `Cartridge/ssh/` on the SD card
3. In VibeBoy: enter your server IP, enable SSH, connect

See the [VibeBoy cartridge README](https://github.com/Strizzo/vibeboy-cartridge) for detailed setup instructions.

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/state` | GET | All tmux sessions with terminal output |
| `/api/action` | POST | Send commands, interrupt, create/kill sessions |
| `/api/health` | GET | Health check |

### Actions

```json
{"action": "send_keys", "session_id": "mysession", "payload": {"keys": "ls -la"}}
{"action": "send_response", "session_id": "mysession", "payload": {"text": "yes"}}
{"action": "interrupt", "session_id": "mysession"}
{"action": "new_session", "payload": {"name": "work"}}
{"action": "kill_session", "session_id": "mysession"}
```
