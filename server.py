#!/usr/bin/env python3
"""
Surveillance Server
===================
Central Flask server that manages multiple Pi camera clients.

Features:
- Camera management (add/remove/rename Pi cameras)
- Live view grid with click-to-enlarge
- 48-hour rolling recording from each camera stream
- Playback of recorded footage
- Motion/event alerts log
- Remote zoom control per camera

Usage:
    python3 server.py [--port 5000] [--recordings-dir ./recordings]
"""

import argparse
import hashlib
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from flask import (
    Flask, Response, abort, jsonify, redirect, render_template,
    request, send_file, url_for,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("server")

BASE_DIR = Path(__file__).parent.resolve()
DB_PATH = BASE_DIR / "surveillance.db"
DEFAULT_RECORDINGS_DIR = BASE_DIR / "recordings"

app = Flask(__name__)
app.secret_key = os.urandom(32)

RECORDINGS_DIR = DEFAULT_RECORDINGS_DIR
SEGMENT_DURATION = 600  # 10-minute segments
MAX_AGE_HOURS = 48
HEALTH_CHECK_INTERVAL = 30  # seconds

# Active FFmpeg recording processes
recording_processes = {}  # camera_id -> subprocess.Popen
recording_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cameras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            pi_user TEXT UNIQUE NOT NULL,
            pi_pass_hash TEXT NOT NULL,
            pi_ip TEXT,
            pi_port INTEGER DEFAULT 8554,
            camera_model TEXT,
            is_online INTEGER DEFAULT 0,
            last_seen TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            zoom_capable INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            camera_id INTEGER,
            event_type TEXT NOT NULL,
            message TEXT,
            timestamp TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (camera_id) REFERENCES cameras(id)
        );

        CREATE INDEX IF NOT EXISTS idx_events_camera ON events(camera_id);
        CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
    """)
    conn.commit()
    conn.close()
    log.info("Database initialized")

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# ---------------------------------------------------------------------------
# Camera management
# ---------------------------------------------------------------------------

def get_all_cameras():
    conn = get_db()
    cameras = conn.execute("SELECT * FROM cameras ORDER BY name").fetchall()
    conn.close()
    return [dict(c) for c in cameras]

def get_camera(camera_id):
    conn = get_db()
    cam = conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
    conn.close()
    return dict(cam) if cam else None

def get_camera_by_user(pi_user):
    conn = get_db()
    cam = conn.execute("SELECT * FROM cameras WHERE pi_user = ?", (pi_user,)).fetchone()
    conn.close()
    return dict(cam) if cam else None

# ---------------------------------------------------------------------------
# Recording management (FFmpeg)
# ---------------------------------------------------------------------------

def start_recording(camera):
    """Start FFmpeg recording for a camera."""
    cam_id = camera["id"]

    with recording_lock:
        if cam_id in recording_processes:
            proc = recording_processes[cam_id]
            if proc.poll() is None:
                log.info(f"Recording already active for camera {cam_id}")
                return

    cam_dir = RECORDINGS_DIR / str(cam_id)
    cam_dir.mkdir(parents=True, exist_ok=True)

    stream_url = (
        f"http://{camera['pi_user']}:{_get_pi_pass(camera)}@"
        f"{camera['pi_ip']}:{camera['pi_port']}/stream"
    )

    output_pattern = str(cam_dir / "seg_%Y%m%d_%H%M%S.mp4")

    cmd = [
        "ffmpeg",
        "-y",
        "-i", stream_url,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-f", "segment",
        "-segment_time", str(SEGMENT_DURATION),
        "-strftime", "1",
        "-reset_timestamps", "1",
        "-an",  # no audio
        output_pattern,
    ]

    log.info(f"Starting recording for camera {cam_id}: {camera['name']}")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with recording_lock:
            recording_processes[cam_id] = proc

        # Log event
        _log_event(cam_id, "recording_start", "Recording started")
    except Exception as e:
        log.error(f"Failed to start recording for camera {cam_id}: {e}")


def stop_recording(camera_id):
    """Stop FFmpeg recording for a camera."""
    with recording_lock:
        proc = recording_processes.pop(camera_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.info(f"Stopped recording for camera {camera_id}")
        _log_event(camera_id, "recording_stop", "Recording stopped")


def cleanup_old_recordings():
    """Delete recording segments older than MAX_AGE_HOURS."""
    cutoff = time.time() - (MAX_AGE_HOURS * 3600)
    count = 0
    for cam_dir in RECORDINGS_DIR.iterdir():
        if not cam_dir.is_dir():
            continue
        for seg_file in cam_dir.glob("seg_*.mp4"):
            if seg_file.stat().st_mtime < cutoff:
                seg_file.unlink()
                count += 1
    if count > 0:
        log.info(f"Cleaned up {count} old recording segments")


def _get_pi_pass(camera):
    """Retrieve plain password. In production use proper secret management."""
    conn = get_db()
    row = conn.execute(
        "SELECT pi_pass_hash FROM cameras WHERE id = ?", (camera["id"],)
    ).fetchone()
    conn.close()
    # We store the actual password for Pi communication (hashed for display only)
    # In a real system you'd use a vault or encrypted storage
    return camera.get("_plain_pass", "")


# We keep a runtime cache of plain passwords for Pi communication
_pi_passwords = {}  # pi_user -> plain password


def _log_event(camera_id, event_type, message):
    conn = get_db()
    conn.execute(
        "INSERT INTO events (camera_id, event_type, message, timestamp) VALUES (?, ?, ?, ?)",
        (camera_id, event_type, message, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

def health_check_loop():
    """Periodically check if Pi cameras are online."""
    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        cameras = get_all_cameras()
        conn = get_db()
        for cam in cameras:
            if not cam["pi_ip"]:
                continue
            pi_pass = _pi_passwords.get(cam["pi_user"], "")
            try:
                r = requests.get(
                    f"http://{cam['pi_ip']}:{cam['pi_port']}/status",
                    auth=(cam["pi_user"], pi_pass),
                    timeout=5,
                )
                if r.status_code == 200:
                    conn.execute(
                        "UPDATE cameras SET is_online = 1, last_seen = ? WHERE id = ?",
                        (datetime.utcnow().isoformat(), cam["id"]),
                    )
                    # Ensure recording is running
                    with recording_lock:
                        if cam["id"] not in recording_processes or \
                           recording_processes[cam["id"]].poll() is not None:
                            cam["_plain_pass"] = pi_pass
                            threading.Thread(
                                target=start_recording, args=(cam,), daemon=True
                            ).start()
                else:
                    conn.execute("UPDATE cameras SET is_online = 0 WHERE id = ?", (cam["id"],))
            except Exception:
                conn.execute("UPDATE cameras SET is_online = 0 WHERE id = ?", (cam["id"],))
        conn.commit()
        conn.close()

        # Clean old recordings every cycle
        cleanup_old_recordings()


def start_background_tasks():
    threading.Thread(target=health_check_loop, daemon=True).start()
    log.info("Background tasks started")

# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/register", methods=["POST"])
def api_register():
    """Called by Pi clients to register/update themselves."""
    data = request.get_json()
    pi_user = data.get("pi_user")
    pi_pass = data.get("pi_pass")
    pi_ip = data.get("pi_ip")
    pi_port = data.get("pi_port", 8554)
    camera_model = data.get("camera_model", "")

    if not pi_user or not pi_pass:
        return jsonify({"error": "pi_user and pi_pass required"}), 400

    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM cameras WHERE pi_user = ?", (pi_user,)
    ).fetchone()

    if existing:
        # Update existing camera
        conn.execute(
            """UPDATE cameras SET pi_ip = ?, pi_port = ?, camera_model = ?,
               is_online = 1, last_seen = ? WHERE pi_user = ?""",
            (pi_ip, pi_port, camera_model, datetime.utcnow().isoformat(), pi_user),
        )
        cam_id = existing["id"]
    else:
        # New camera — auto-register with pi_user as default name
        cursor = conn.execute(
            """INSERT INTO cameras (name, pi_user, pi_pass_hash, pi_ip, pi_port, camera_model,
               is_online, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (pi_user, pi_user, hash_password(pi_pass), pi_ip, pi_port, camera_model,
             datetime.utcnow().isoformat()),
        )
        cam_id = cursor.lastrowid

    conn.commit()
    conn.close()

    # Cache plain password for Pi communication
    _pi_passwords[pi_user] = pi_pass

    # Start recording
    cam = get_camera(cam_id)
    if cam:
        cam["_plain_pass"] = pi_pass
        threading.Thread(target=start_recording, args=(cam,), daemon=True).start()

    _log_event(cam_id, "registered", f"Pi registered from {pi_ip}")
    log.info(f"Camera registered: {pi_user} @ {pi_ip}:{pi_port}")

    return jsonify({"status": "ok", "camera_id": cam_id})


@app.route("/api/cameras", methods=["GET"])
def api_cameras():
    """List all cameras."""
    return jsonify(get_all_cameras())


@app.route("/api/cameras", methods=["POST"])
def api_add_camera():
    """Manually add a camera."""
    data = request.get_json()
    name = data.get("name", "New Camera")
    pi_user = data.get("pi_user")
    pi_pass = data.get("pi_pass")
    pi_ip = data.get("pi_ip", "")
    pi_port = data.get("pi_port", 8554)

    if not pi_user or not pi_pass:
        return jsonify({"error": "pi_user and pi_pass required"}), 400

    conn = get_db()
    try:
        cursor = conn.execute(
            """INSERT INTO cameras (name, pi_user, pi_pass_hash, pi_ip, pi_port)
               VALUES (?, ?, ?, ?, ?)""",
            (name, pi_user, hash_password(pi_pass), pi_ip, pi_port),
        )
        conn.commit()
        cam_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"error": "pi_user already exists"}), 409
    conn.close()

    _pi_passwords[pi_user] = pi_pass
    return jsonify({"status": "ok", "camera_id": cam_id})


@app.route("/api/cameras/<int:cam_id>", methods=["PUT"])
def api_update_camera(cam_id):
    """Update camera name/settings."""
    data = request.get_json()
    conn = get_db()
    cam = conn.execute("SELECT * FROM cameras WHERE id = ?", (cam_id,)).fetchone()
    if not cam:
        conn.close()
        return jsonify({"error": "Camera not found"}), 404

    name = data.get("name", cam["name"])
    pi_ip = data.get("pi_ip", cam["pi_ip"])
    pi_port = data.get("pi_port", cam["pi_port"])

    conn.execute(
        "UPDATE cameras SET name = ?, pi_ip = ?, pi_port = ? WHERE id = ?",
        (name, pi_ip, pi_port, cam_id),
    )

    if "pi_pass" in data:
        conn.execute(
            "UPDATE cameras SET pi_pass_hash = ? WHERE id = ?",
            (hash_password(data["pi_pass"]), cam_id),
        )
        _pi_passwords[cam["pi_user"]] = data["pi_pass"]

    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/cameras/<int:cam_id>", methods=["DELETE"])
def api_delete_camera(cam_id):
    """Remove a camera."""
    stop_recording(cam_id)
    conn = get_db()
    conn.execute("DELETE FROM events WHERE camera_id = ?", (cam_id,))
    conn.execute("DELETE FROM cameras WHERE id = ?", (cam_id,))
    conn.commit()
    conn.close()

    # Clean up recordings
    cam_dir = RECORDINGS_DIR / str(cam_id)
    if cam_dir.exists():
        import shutil
        shutil.rmtree(cam_dir)

    return jsonify({"status": "ok"})


@app.route("/api/cameras/<int:cam_id>/stream")
def api_camera_stream(cam_id):
    """Proxy the live MJPEG stream from a Pi camera."""
    cam = get_camera(cam_id)
    if not cam or not cam["pi_ip"]:
        abort(404)

    pi_pass = _pi_passwords.get(cam["pi_user"], "")

    def proxy_stream():
        try:
            r = requests.get(
                f"http://{cam['pi_ip']}:{cam['pi_port']}/stream",
                auth=(cam["pi_user"], pi_pass),
                stream=True,
                timeout=30,
            )
            for chunk in r.iter_content(chunk_size=4096):
                yield chunk
        except Exception as e:
            log.error(f"Stream proxy error for camera {cam_id}: {e}")

    return Response(proxy_stream(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/cameras/<int:cam_id>/snapshot")
def api_camera_snapshot(cam_id):
    """Get a snapshot from a Pi camera."""
    cam = get_camera(cam_id)
    if not cam or not cam["pi_ip"]:
        abort(404)

    pi_pass = _pi_passwords.get(cam["pi_user"], "")
    try:
        r = requests.get(
            f"http://{cam['pi_ip']}:{cam['pi_port']}/snapshot",
            auth=(cam["pi_user"], pi_pass),
            timeout=10,
        )
        return Response(r.content, mimetype="image/jpeg")
    except Exception:
        abort(503)


@app.route("/api/cameras/<int:cam_id>/zoom", methods=["POST"])
def api_camera_zoom(cam_id):
    """Control camera zoom through Pi relay."""
    cam = get_camera(cam_id)
    if not cam or not cam["pi_ip"]:
        abort(404)

    pi_pass = _pi_passwords.get(cam["pi_user"], "")
    data = request.get_json()

    try:
        r = requests.post(
            f"http://{cam['pi_ip']}:{cam['pi_port']}/zoom",
            json=data,
            auth=(cam["pi_user"], pi_pass),
            timeout=5,
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.route("/api/cameras/<int:cam_id>/recordings")
def api_camera_recordings(cam_id):
    """List available recording segments for a camera."""
    cam_dir = RECORDINGS_DIR / str(cam_id)
    if not cam_dir.exists():
        return jsonify([])

    segments = []
    for f in sorted(cam_dir.glob("seg_*.mp4"), reverse=True):
        stat = f.stat()
        segments.append({
            "filename": f.name,
            "size_mb": round(stat.st_size / (1024 * 1024), 1),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "url": f"/api/cameras/{cam_id}/recordings/{f.name}",
        })

    return jsonify(segments)


@app.route("/api/cameras/<int:cam_id>/recordings/<filename>")
def api_camera_recording_file(cam_id, filename):
    """Serve a recording segment file."""
    cam_dir = RECORDINGS_DIR / str(cam_id)
    filepath = cam_dir / filename
    if not filepath.exists() or not filepath.is_file():
        abort(404)
    return send_file(filepath, mimetype="video/mp4")


@app.route("/api/events", methods=["GET"])
def api_events_list():
    """List events with optional filters."""
    camera_id = request.args.get("camera_id", type=int)
    event_type = request.args.get("type")
    limit = request.args.get("limit", 100, type=int)

    conn = get_db()
    query = """
        SELECT e.*, c.name as camera_name
        FROM events e LEFT JOIN cameras c ON e.camera_id = c.id
        WHERE 1=1
    """
    params = []

    if camera_id:
        query += " AND e.camera_id = ?"
        params.append(camera_id)
    if event_type:
        query += " AND e.event_type = ?"
        params.append(event_type)

    query += " ORDER BY e.timestamp DESC LIMIT ?"
    params.append(limit)

    events = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(e) for e in events])


@app.route("/api/events", methods=["POST"])
def api_events_create():
    """Receive events from Pi cameras."""
    data = request.get_json()
    pi_user = data.get("pi_user")
    event = data.get("event", {})

    cam = get_camera_by_user(pi_user)
    if not cam:
        return jsonify({"error": "Unknown camera"}), 404

    _log_event(
        cam["id"],
        event.get("type", "unknown"),
        event.get("message", ""),
    )
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global RECORDINGS_DIR

    parser = argparse.ArgumentParser(description="Surveillance Server")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--recordings-dir", default=str(DEFAULT_RECORDINGS_DIR))
    parser.add_argument("--max-age-hours", type=int, default=48)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    RECORDINGS_DIR = Path(args.recordings_dir)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    global MAX_AGE_HOURS
    MAX_AGE_HOURS = args.max_age_hours

    init_db()
    start_background_tasks()

    log.info(f"Starting surveillance server on {args.host}:{args.port}")
    log.info(f"Recordings directory: {RECORDINGS_DIR}")
    log.info(f"Rolling window: {MAX_AGE_HOURS} hours")

    app.run(host=args.host, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
