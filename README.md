# Surveillance HQ

A self-hosted surveillance system using Raspberry Pis as camera relay hubs and a central Flask server for viewing, recording, and control.

## Architecture

```
[Hikvision Camera] --ethernet--> [Raspberry Pi] --WiFi--> [Server]
[Hikvision Camera] --ethernet--> [Raspberry Pi] --WiFi--> [Server]
...
```

Each Pi connects directly to a camera via ethernet and relays the stream over WiFi to the server.

## Features

- **Live View Grid** — See all cameras at once, click to enlarge
- **48-Hour Rolling Recording** — Server records all streams via FFmpeg, auto-deletes old footage
- **Playback** — Browse and play back recorded segments by camera
- **Motion Detection** — Pi-side frame differencing with events pushed to server
- **Event Log** — Filterable log of motion events, recording status, registrations
- **Remote Zoom Control** — Control the Hikvision motorized zoom lens from the server UI
- **Camera Management** — Add, rename, remove cameras from the web UI
- **Auto-Registration** — Pis register themselves with the server on startup

---

## Setup

### 1. Server Setup

The server can run on any machine (desktop, NAS, cloud VM, even another Pi).

```bash
cd server/

# Install dependencies
pip install -r requirements.txt

# Make sure FFmpeg is installed
sudo apt install ffmpeg   # Debian/Ubuntu
brew install ffmpeg        # macOS

# Run
python3 server.py --port 5000 --recordings-dir ./recordings
```

Open `http://YOUR_SERVER_IP:5000` in a browser.

**Options:**
- `--port` — Server port (default: 5000)
- `--recordings-dir` — Where to store video segments (default: ./recordings)
- `--max-age-hours` — Rolling window in hours (default: 48)
- `--host` — Bind address (default: 0.0.0.0)

### 2. Pi Setup (per camera)

On each Raspberry Pi:

```bash
cd pi-client/

# Run the setup script (configures networking, installs deps)
chmod +x setup.sh
./setup.sh

# Start the client
source ~/pi-camera-env/bin/activate
python3 pi_camera_client.py \
    --server http://SERVER_IP:5000 \
    --camera-ip 192.168.2.100 \
    --camera-user admin \
    --camera-pass YOUR_CAMERA_PASSWORD \
    --pi-user pi_frontdoor \
    --pi-pass YOUR_SHARED_SECRET
```

**Options:**
- `--server` — URL of the central server
- `--camera-ip` — IP of the Hikvision camera on the ethernet side (default: 192.168.2.100)
- `--camera-user` / `--camera-pass` — Hikvision camera credentials
- `--pi-user` / `--pi-pass` — Credentials for this Pi (used for server ↔ Pi auth)
- `--pi-port` — Port for the Pi's local relay server (default: 8554)
- `--channel` — RTSP channel: 101 = main stream, 102 = sub stream
- `--no-motion` — Disable motion detection (saves CPU)

### 3. Hardware Wiring

```
[PoE Switch/Injector] --ethernet--> [Hikvision Camera]
         |
    [Raspberry Pi eth0]
         |
    [Pi WiFi] ~~~wireless~~~> [Your Router] --> [Server]
```

- The camera gets power + data over ethernet (PoE)
- The Pi's eth0 is configured with static IP 192.168.2.1
- The Pi runs a DHCP server so the camera auto-gets an IP (192.168.2.100+)
- The Pi's WiFi connects to your home network to reach the server

---

## Camera Compatibility

Tested with: **Hikvision DS-2CD2743G2-IZS** (motorized zoom)

Should work with any Hikvision IP camera that supports:
- RTSP streaming
- ISAPI (for zoom/focus control)

Also works with any ONVIF-compatible camera (Dahua, Reolink, Amcrest, etc.) — just the zoom controls are Hikvision-specific.

---

## Storage Estimates

| Cameras | Quality   | 48hr Storage |
|---------|-----------|-------------|
| 1       | 2 Mbps    | ~43 GB      |
| 1       | 4 Mbps    | ~86 GB      |
| 4       | 2 Mbps    | ~173 GB     |
| 4       | 4 Mbps    | ~346 GB     |

Use `--channel 102` (sub stream) on the Pi to reduce bandwidth/storage.

---

## Running as a Service

### Pi (systemd)

```bash
sudo tee /etc/systemd/system/pi-camera.service > /dev/null << EOF
[Unit]
Description=Pi Camera Client
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/pi-client
ExecStart=/home/pi/pi-camera-env/bin/python3 pi_camera_client.py \
    --server http://SERVER_IP:5000 \
    --camera-ip 192.168.2.100 \
    --camera-user admin \
    --camera-pass CAMERA_PASS \
    --pi-user pi_frontdoor \
    --pi-pass PI_SECRET
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable pi-camera
sudo systemctl start pi-camera
```

### Server (systemd)

```bash
sudo tee /etc/systemd/system/surveillance-server.service > /dev/null << EOF
[Unit]
Description=Surveillance Server
After=network.target

[Service]
User=youruser
WorkingDirectory=/path/to/server
ExecStart=/usr/bin/python3 server.py --port 5000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable surveillance-server
sudo systemctl start surveillance-server
```
