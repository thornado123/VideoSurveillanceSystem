#!/usr/bin/env python3
"""
Pi Camera Client
================
Runs on each Raspberry Pi. Connects to a local Hikvision camera via ethernet,
and relays the video stream + camera controls to the central server over WiFi.

Usage:
    python3 pi_camera_client.py --server http://SERVER_IP:5000 \
                                 --camera-ip 192.168.2.100 \
                                 --camera-user admin \
                                 --camera-pass yourpassword \
                                 --pi-user pi_livingroom \
                                 --pi-pass secretkey123
"""

import argparse
import io
import json
import logging
import signal
import sys
import threading
import time
from datetime import datetime

import cv2
import requests
from flask import Flask, Response, jsonify, request
from requests.auth import HTTPDigestAuth

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("pi-client")

app = Flask(__name__)

# Globals filled by CLI args
CONFIG = {
    "server_url": "",
    "camera_ip": "",
    "camera_user": "",
    "camera_pass": "",
    "camera_rtsp_port": 554,
    "camera_http_port": 80,
    "pi_user": "",
    "pi_pass": "",
    "pi_port": 8554,
    "stream_channel": "101",  # 101 = main stream, 102 = sub stream
}

# ---------------------------------------------------------------------------
# Camera connection
# ---------------------------------------------------------------------------

class CameraStream:
    """Manages the RTSP connection to the Hikvision camera."""

    def __init__(self):
        self.cap = None
        self.lock = threading.Lock()
        self.running = False
        self.last_frame = None
        self.frame_count = 0
        self.fps = 0
        self._fps_time = time.time()

    def start(self):
        rtsp_url = (
            f"rtsp://{CONFIG['camera_user']}:{CONFIG['camera_pass']}"
            f"@{CONFIG['camera_ip']}:{CONFIG['camera_rtsp_port']}"
            f"/Streaming/Channels/{CONFIG['stream_channel']}"
        )
        log.info(f"Connecting to camera RTSP: {CONFIG['camera_ip']}")

        self.cap = cv2.VideoCapture(rtsp_url)
        if not self.cap.isOpened():
            log.error("Failed to open RTSP stream!")
            return False

        self.running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        log.info("Camera stream started")
        return True

    def _read_loop(self):
        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                log.warning("Frame read failed, reconnecting in 5s...")
                time.sleep(5)
                self.cap.release()
                rtsp_url = (
                    f"rtsp://{CONFIG['camera_user']}:{CONFIG['camera_pass']}"
                    f"@{CONFIG['camera_ip']}:{CONFIG['camera_rtsp_port']}"
                    f"/Streaming/Channels/{CONFIG['stream_channel']}"
                )
                self.cap = cv2.VideoCapture(rtsp_url)
                continue

            with self.lock:
                self.last_frame = frame
                self.frame_count += 1

            # Calculate FPS every 5 seconds
            now = time.time()
            if now - self._fps_time >= 5:
                self.fps = self.frame_count / (now - self._fps_time)
                self.frame_count = 0
                self._fps_time = now

    def get_frame_jpeg(self, quality=80):
        with self.lock:
            if self.last_frame is None:
                return None
            _, jpeg = cv2.imencode(
                ".jpg", self.last_frame, [cv2.IMWRITE_JPEG_QUALITY, quality]
            )
            return jpeg.tobytes()

    def stop(self):
        self.running = False
        if self.cap:
            self.cap.release()


camera_stream = CameraStream()

# ---------------------------------------------------------------------------
# Hikvision ISAPI helpers (for zoom control)
# ---------------------------------------------------------------------------

def _isapi_auth():
    return HTTPDigestAuth(CONFIG["camera_user"], CONFIG["camera_pass"])

def _isapi_url(path):
    return f"http://{CONFIG['camera_ip']}:{CONFIG['camera_http_port']}{path}"

def camera_get_zoom():
    """Get current zoom/focus status."""
    try:
        r = requests.get(
            _isapi_url("/ISAPI/Image/channels/1/focusConfiguration"),
            auth=_isapi_auth(),
            timeout=5,
        )
        return r.text
    except Exception as e:
        log.error(f"Failed to get zoom status: {e}")
        return None

def camera_set_zoom(action, speed=50):
    """
    Control motorized zoom.
    action: 'zoomIn', 'zoomOut', 'focusNear', 'focusFar', 'stop'
    """
    try:
        data = f"""<?xml version="1.0" encoding="UTF-8"?>
        <FocusData>
            <autoFocusEnabled>false</autoFocusEnabled>
        </FocusData>"""

        if action == "stop":
            # Stop zoom/focus movement
            r = requests.put(
                _isapi_url("/ISAPI/PTZCtrl/channels/1/continuous"),
                auth=_isapi_auth(),
                data="""<PTZData><pan>0</pan><tilt>0</tilt><zoom>0</zoom></PTZData>""",
                timeout=5,
            )
        elif action == "zoomIn":
            r = requests.put(
                _isapi_url("/ISAPI/PTZCtrl/channels/1/continuous"),
                auth=_isapi_auth(),
                data=f"""<PTZData><pan>0</pan><tilt>0</tilt><zoom>{speed}</zoom></PTZData>""",
                timeout=5,
            )
        elif action == "zoomOut":
            r = requests.put(
                _isapi_url("/ISAPI/PTZCtrl/channels/1/continuous"),
                auth=_isapi_auth(),
                data=f"""<PTZData><pan>0</pan><tilt>0</tilt><zoom>-{speed}</zoom></PTZData>""",
                timeout=5,
            )
        elif action == "autoFocus":
            r = requests.put(
                _isapi_url("/ISAPI/Image/channels/1/focusConfiguration"),
                auth=_isapi_auth(),
                data="""<?xml version="1.0" encoding="UTF-8"?>
                <FocusConfiguration>
                    <focusStyle>AUTO</focusStyle>
                </FocusConfiguration>""",
                timeout=5,
            )
        else:
            return {"error": f"Unknown action: {action}"}

        return {"status": "ok", "action": action}
    except Exception as e:
        log.error(f"Zoom control error: {e}")
        return {"error": str(e)}

def camera_get_snapshot():
    """Get a high-quality snapshot directly from the camera."""
    try:
        r = requests.get(
            _isapi_url(f"/ISAPI/Streaming/channels/{CONFIG['stream_channel']}/picture"),
            auth=_isapi_auth(),
            timeout=10,
            stream=True,
        )
        return r.content
    except Exception as e:
        log.error(f"Snapshot error: {e}")
        return None

def camera_get_device_info():
    """Get camera model/firmware info."""
    try:
        r = requests.get(
            _isapi_url("/ISAPI/System/deviceInfo"),
            auth=_isapi_auth(),
            timeout=5,
        )
        return r.text
    except Exception as e:
        return f"<error>{e}</error>"

# ---------------------------------------------------------------------------
# Motion detection (simple frame differencing)
# ---------------------------------------------------------------------------

class MotionDetector:
    def __init__(self, threshold=25, min_area=5000, cooldown=10):
        self.threshold = threshold
        self.min_area = min_area
        self.cooldown = cooldown
        self.prev_gray = None
        self.last_alert_time = 0
        self.running = False
        self.events = []

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._detect_loop, daemon=True)
        self._thread.start()
        log.info("Motion detection started")

    def _detect_loop(self):
        while self.running:
            frame_jpeg = camera_stream.get_frame_jpeg()
            if frame_jpeg is None:
                time.sleep(0.5)
                continue

            # Decode and convert to grayscale
            nparr = __import__("numpy").frombuffer(frame_jpeg, __import__("numpy").uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)

            if self.prev_gray is None:
                self.prev_gray = gray
                time.sleep(0.5)
                continue

            # Frame difference
            delta = cv2.absdiff(self.prev_gray, gray)
            thresh = cv2.threshold(delta, self.threshold, 255, cv2.THRESH_BINARY)[1]
            thresh = cv2.dilate(thresh, None, iterations=2)
            contours, _ = cv2.findContours(
                thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )

            motion_detected = any(
                cv2.contourArea(c) > self.min_area for c in contours
            )

            now = time.time()
            if motion_detected and (now - self.last_alert_time) > self.cooldown:
                self.last_alert_time = now
                event = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "type": "motion",
                    "message": "Motion detected",
                }
                self.events.append(event)
                # Keep only last 1000 events locally
                if len(self.events) > 1000:
                    self.events = self.events[-500:]

                # Notify server
                self._notify_server(event)
                log.info("Motion detected!")

            self.prev_gray = gray
            time.sleep(0.5)  # Check ~2x per second

    def _notify_server(self, event):
        try:
            requests.post(
                f"{CONFIG['server_url']}/api/events",
                json={
                    "pi_user": CONFIG["pi_user"],
                    "event": event,
                },
                auth=(CONFIG["pi_user"], CONFIG["pi_pass"]),
                timeout=5,
            )
        except Exception as e:
            log.warning(f"Could not notify server: {e}")

    def stop(self):
        self.running = False


motion_detector = MotionDetector()

# ---------------------------------------------------------------------------
# Flask endpoints (served by the Pi)
# ---------------------------------------------------------------------------

def check_auth():
    """Verify request credentials."""
    auth = request.authorization
    if not auth:
        return False
    return auth.username == CONFIG["pi_user"] and auth.password == CONFIG["pi_pass"]

def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not check_auth():
            return Response("Unauthorized", 401,
                          {"WWW-Authenticate": 'Basic realm="Pi Camera"'})
        return f(*args, **kwargs)
    return decorated


@app.route("/stream")
@require_auth
def stream():
    """MJPEG stream endpoint."""
    def generate():
        while True:
            frame = camera_stream.get_frame_jpeg(quality=70)
            if frame is None:
                time.sleep(0.1)
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            )
            time.sleep(0.04)  # ~25 fps max

    return Response(
        generate(), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/snapshot")
@require_auth
def snapshot():
    """Single JPEG frame."""
    frame = camera_stream.get_frame_jpeg(quality=95)
    if frame is None:
        return "No frame available", 503
    return Response(frame, mimetype="image/jpeg")


@app.route("/status")
@require_auth
def status():
    """Pi and camera health status."""
    return jsonify({
        "online": True,
        "camera_connected": camera_stream.cap is not None and camera_stream.cap.isOpened(),
        "fps": round(camera_stream.fps, 1),
        "timestamp": datetime.utcnow().isoformat(),
        "motion_events_count": len(motion_detector.events),
    })


@app.route("/zoom", methods=["POST"])
@require_auth
def zoom_control():
    """Control motorized zoom."""
    data = request.get_json()
    action = data.get("action", "stop")
    speed = data.get("speed", 50)
    result = camera_set_zoom(action, speed)
    return jsonify(result)


@app.route("/device_info")
@require_auth
def device_info():
    """Get camera device info."""
    info = camera_get_device_info()
    return Response(info, mimetype="application/xml")


@app.route("/events")
@require_auth
def events():
    """Get recent motion events."""
    limit = request.args.get("limit", 50, type=int)
    return jsonify(motion_detector.events[-limit:])


# ---------------------------------------------------------------------------
# Server registration
# ---------------------------------------------------------------------------

def register_with_server():
    """Register this Pi with the central server on startup."""
    import socket

    local_ip = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "unknown"

    payload = {
        "pi_user": CONFIG["pi_user"],
        "pi_pass": CONFIG["pi_pass"],
        "pi_ip": local_ip,
        "pi_port": CONFIG["pi_port"],
        "camera_model": "Hikvision DS-2CD2743G2-IZS",
    }

    for attempt in range(5):
        try:
            r = requests.post(
                f"{CONFIG['server_url']}/api/register",
                json=payload,
                timeout=10,
            )
            if r.status_code == 200:
                log.info(f"Registered with server as '{CONFIG['pi_user']}' (IP: {local_ip})")
                return True
            else:
                log.warning(f"Registration failed: {r.status_code} {r.text}")
        except Exception as e:
            log.warning(f"Registration attempt {attempt+1} failed: {e}")
        time.sleep(5)

    log.error("Could not register with server after 5 attempts")
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Pi Camera Client")
    parser.add_argument("--server", required=True, help="Server URL (e.g. http://192.168.1.50:5000)")
    parser.add_argument("--camera-ip", default="192.168.2.100", help="Camera IP")
    parser.add_argument("--camera-user", default="admin", help="Camera username")
    parser.add_argument("--camera-pass", required=True, help="Camera password")
    parser.add_argument("--camera-rtsp-port", type=int, default=554)
    parser.add_argument("--camera-http-port", type=int, default=80)
    parser.add_argument("--pi-user", required=True, help="Username for this Pi (used for server auth)")
    parser.add_argument("--pi-pass", required=True, help="Password for this Pi")
    parser.add_argument("--pi-port", type=int, default=8554, help="Port for Pi's local server")
    parser.add_argument("--channel", default="101", help="RTSP channel (101=main, 102=sub)")
    parser.add_argument("--no-motion", action="store_true", help="Disable motion detection")
    args = parser.parse_args()

    CONFIG.update({
        "server_url": args.server.rstrip("/"),
        "camera_ip": args.camera_ip,
        "camera_user": args.camera_user,
        "camera_pass": args.camera_pass,
        "camera_rtsp_port": args.camera_rtsp_port,
        "camera_http_port": args.camera_http_port,
        "pi_user": args.pi_user,
        "pi_pass": args.pi_pass,
        "pi_port": args.pi_port,
        "stream_channel": args.channel,
    })

    # Start camera stream
    if not camera_stream.start():
        log.error("Could not start camera stream. Exiting.")
        sys.exit(1)

    # Start motion detection
    if not args.no_motion:
        motion_detector.start()

    # Register with server
    reg_thread = threading.Thread(target=register_with_server, daemon=True)
    reg_thread.start()

    # Start Flask server
    log.info(f"Starting Pi relay server on port {CONFIG['pi_port']}")
    app.run(host="0.0.0.0", port=CONFIG["pi_port"], threaded=True)


if __name__ == "__main__":
    main()
