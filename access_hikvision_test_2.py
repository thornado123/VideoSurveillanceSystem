#!/usr/bin/env python3
"""
Hikvision Camera Test
======================
Tests connectivity from your Raspberry Pi 5 to a Hikvision DS-2CD2743G2-IZS.
Tests raw HTTP/RTSP, the hikvisionapi library, and ONVIF via python-onvif-zeep.

Usage:
    pip install requests opencv-python-headless hikvisionapi onvif-zeep
    python3 test_camera.py --ip 192.168.2.100 --user admin --pass yourpassword
"""

import argparse
import sys
import subprocess

def sep(title):
    print(f"\n{'='*50}")
    print(f"  {title}")
    print(f"{'='*50}")

def test_ping(ip):
    sep("1. PING TEST")
    result = subprocess.run(["ping", "-c", "3", "-W", "2", ip], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"✅ Camera at {ip} is reachable")
        print(result.stdout.strip().split('\n')[-1])  # summary line
        return True
    else:
        print(f"❌ Cannot ping {ip}")
        print("   Check: ethernet cable, PoE power, camera IP config")
        print("   Your Pi's eth0 should be on the same subnet (e.g. 192.168.2.1/24)")
        return False

def test_http(ip, port=80):
    sep("2. HTTP PORT TEST")
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect((ip, port))
        print(f"✅ HTTP port {port} is open")
        sock.close()
        return True
    except Exception as e:
        print(f"❌ Cannot connect to port {port}: {e}")
        sock.close()
        return False

def test_isapi(ip, user, password):
    sep("3. ISAPI DEVICE INFO")
    import requests
    from requests.auth import HTTPDigestAuth
    try:
        r = requests.get(
            f"http://{ip}/ISAPI/System/deviceInfo",
            auth=HTTPDigestAuth(user, password),
            timeout=5,
        )
        if r.status_code == 200:
            print(f"✅ ISAPI authentication successful")
            # Parse basic info from XML
            text = r.text
            for tag in ["deviceName", "model", "serialNumber", "firmwareVersion"]:
                start = text.find(f"<{tag}>")
                end = text.find(f"</{tag}>")
                if start != -1 and end != -1:
                    val = text[start + len(tag) + 2 : end]
                    print(f"   {tag}: {val}")
            return True
        elif r.status_code == 401:
            print(f"❌ Authentication failed (401) — wrong username/password")
            return False
        else:
            print(f"❌ Unexpected response: {r.status_code}")
            return False
    except Exception as e:
        print(f"❌ ISAPI request failed: {e}")
        return False

def test_snapshot(ip, user, password):
    sep("4. SNAPSHOT TEST")
    import requests
    from requests.auth import HTTPDigestAuth
    try:
        r = requests.get(
            f"http://{ip}/ISAPI/Streaming/channels/101/picture",
            auth=HTTPDigestAuth(user, password),
            timeout=10,
            stream=True,
        )
        if r.status_code == 200:
            with open("test_snapshot.jpg", "wb") as f:
                for chunk in r.iter_content(1024):
                    f.write(chunk)
            import os
            size = os.path.getsize("test_snapshot.jpg")
            print(f"✅ Snapshot saved: test_snapshot.jpg ({size:,} bytes)")
            return True
        else:
            print(f"❌ Snapshot failed: {r.status_code}")
            return False
    except Exception as e:
        print(f"❌ Snapshot error: {e}")
        return False

def test_rtsp(ip, user, password):
    sep("5. RTSP STREAM TEST")
    try:
        import cv2
    except ImportError:
        print("⚠️  opencv not installed, skipping (pip install opencv-python-headless)")
        return None

    from urllib.parse import quote
    encoded_pass = quote(password, safe='')
    url = f"rtsp://{user}:{encoded_pass}@{ip}:554/Streaming/Channels/101"
    print(f"   Connecting to RTSP...")
    cap = cv2.VideoCapture(url)

    if not cap.isOpened():
        print(f"❌ Cannot open RTSP stream")
        print(f"   URL: rtsp://{user}:****@{ip}:554/Streaming/Channels/101")
        return False

    ret, frame = cap.read()
    cap.release()

    if ret:
        h, w = frame.shape[:2]
        cv2.imwrite("test_frame.jpg", frame)
        print(f"✅ RTSP stream working — {w}x{h}")
        print(f"   Frame saved: test_frame.jpg")
        return True
    else:
        print(f"❌ Connected but couldn't read a frame")
        return False

def test_zoom(ip, user, password):
    sep("6. MOTORIZED ZOOM TEST")
    import requests
    from requests.auth import HTTPDigestAuth
    try:
        # Try reading current focus config
        r = requests.get(
            f"http://{ip}/ISAPI/Image/channels/1/focusConfiguration",
            auth=HTTPDigestAuth(user, password),
            timeout=5,
        )
        if r.status_code == 200:
            print(f"✅ Zoom/focus endpoint accessible")
            if "focusStyle" in r.text:
                start = r.text.find("<focusStyle>") + 12
                end = r.text.find("</focusStyle>")
                print(f"   Focus mode: {r.text[start:end]}")
            return True
        else:
            print(f"⚠️  Zoom endpoint returned {r.status_code} (may not support this path)")
            return False
    except Exception as e:
        print(f"❌ Zoom test error: {e}")
        return False

def test_hikvisionapi(ip, user, password):
    sep("7. HIKVISIONAPI LIBRARY")
    try:
        from hikvisionapi import Client
    except ImportError:
        print("⚠️  hikvisionapi not installed, skipping (pip install hikvisionapi)")
        return None

    try:
        cam = Client(f"http://{ip}", user, password, timeout=10)

        # Device info
        info = cam.System.deviceInfo(method='get')
        print(f"✅ hikvisionapi connected successfully")

        # Try to extract info from response
        if hasattr(info, 'text'):
            text = info.text
        elif isinstance(info, dict):
            text = str(info)
        else:
            text = str(info)

        for tag in ["deviceName", "model", "serialNumber"]:
            if tag in text:
                start = text.find(f"<{tag}>")
                end = text.find(f"</{tag}>")
                if start != -1 and end != -1:
                    val = text[start + len(tag) + 2 : end]
                    print(f"   {tag}: {val}")

        # Snapshot via library
        print(f"   Fetching snapshot via library...")
        response = cam.Streaming.channels[101].picture(method='get', type='opaque_data')
        with open("test_hikvisionapi_snapshot.jpg", "wb") as f:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
        import os
        size = os.path.getsize("test_hikvisionapi_snapshot.jpg")
        if size > 0:
            print(f"   ✅ Snapshot via library saved: test_hikvisionapi_snapshot.jpg ({size:,} bytes)")
        else:
            print(f"   ⚠️  Snapshot file empty (channel 102 may not be configured)")

        # List streaming channels
        print(f"   Checking available streaming channels...")
        try:
            channels = cam.Streaming.channels(method='get')
            if hasattr(channels, 'text') and 'StreamingChannel' in channels.text:
                count = channels.text.count('<StreamingChannel>')
                print(f"   ✅ Found {count} streaming channel(s)")
        except Exception:
            print(f"   ⚠️  Could not list channels (non-critical)")

        return True

    except Exception as e:
        print(f"❌ hikvisionapi error: {e}")
        print(f"   This can happen if the library version doesn't match your firmware.")
        print(f"   The raw ISAPI tests above confirm the camera works regardless.")
        return False


def test_onvif(ip, user, password):
    sep("8. ONVIF (python-onvif-zeep)")
    try:
        from onvif import ONVIFCamera
    except ImportError:
        print("⚠️  onvif-zeep not installed, skipping (pip install onvif-zeep)")
        return None

    try:
        print(f"   Connecting via ONVIF (this can take 10-20s)...")
        cam = ONVIFCamera(ip, 80, user, password)

        # Device info
        devicemgmt = cam.devicemgmt
        info = devicemgmt.GetDeviceInformation()
        print(f"✅ ONVIF connected")
        print(f"   Manufacturer: {info.Manufacturer}")
        print(f"   Model: {info.Model}")
        print(f"   Firmware: {info.FirmwareVersion}")
        print(f"   Serial: {info.SerialNumber}")

        # Media profiles
        media_service = cam.create_media_service()
        profiles = media_service.GetProfiles()
        print(f"   ✅ Found {len(profiles)} media profile(s)")
        for i, p in enumerate(profiles):
            name = p.Name
            try:
                w = p.VideoEncoderConfiguration.Resolution.Width
                h = p.VideoEncoderConfiguration.Resolution.Height
                enc = p.VideoEncoderConfiguration.Encoding
                print(f"      [{i}] {name}: {w}x{h} ({enc})")
            except Exception:
                print(f"      [{i}] {name}")

        # Get RTSP URI via ONVIF
        token = profiles[0].token
        stream_setup = {
            'Stream': 'RTP-Unicast',
            'Transport': {'Protocol': 'RTSP'}
        }
        uri_obj = media_service.GetStreamUri({
            'StreamSetup': stream_setup,
            'ProfileToken': token
        })
        print(f"   ✅ RTSP URI: {uri_obj.Uri}")

        # Check ONVIF imaging service (zoom/focus)
        try:
            imaging = cam.create_imaging_service()
            video_sources = media_service.GetVideoSources()
            if video_sources:
                vs_token = video_sources[0].token
                img_settings = imaging.GetImagingSettings({'VideoSourceToken': vs_token})
                if hasattr(img_settings, 'Focus'):
                    mode = img_settings.Focus.AutoFocusMode
                    print(f"   ✅ Imaging/Focus accessible (mode: {mode})")
                else:
                    print(f"   ⚠️  No focus info in imaging settings")
        except Exception as e:
            print(f"   ⚠️  Imaging service: {e}")

        return True

    except Exception as e:
        print(f"❌ ONVIF error: {e}")
        print(f"   Make sure ONVIF is enabled in the camera's web UI:")
        print(f"   Configuration → Network → Advanced → Integration Protocol → ONVIF")
        return False


def main():
    parser = argparse.ArgumentParser(description="Test Hikvision camera connectivity")
    parser.add_argument("--ip", default="192.168.2.100", help="Camera IP (default: 192.168.2.100)")
    parser.add_argument("--user", default="admin", help="Camera username (default: admin)")
    parser.add_argument("--pass", dest="password", required=True, help="Camera password")
    args = parser.parse_args()

    print("Hikvision DS-2CD2743G2-IZS — Full Connection Test")
    print(f"Target: {args.ip}  User: {args.user}")
    print(f"Testing: raw HTTP, RTSP, hikvisionapi, ONVIF")

    results = {}
    results["ping"]     = test_ping(args.ip)
    results["http"]     = test_http(args.ip) if results["ping"] else False
    results["isapi"]    = test_isapi(args.ip, args.user, args.password) if results["http"] else False
    results["snapshot"] = test_snapshot(args.ip, args.user, args.password) if results["isapi"] else False
    results["rtsp"]     = test_rtsp(args.ip, args.user, args.password) if results["isapi"] else False
    results["zoom"]         = test_zoom(args.ip, args.user, args.password) if results["isapi"] else False
    results["hikvisionapi"] = test_hikvisionapi(args.ip, args.user, args.password) if results["isapi"] else False
    results["onvif"]        = test_onvif(args.ip, args.user, args.password) if results["http"] else False

    sep("SUMMARY")
    for test, passed in results.items():
        icon = "✅" if passed else ("⚠️ " if passed is None else "❌")
        print(f"  {icon} {test}")

    all_ok = all(v is True for v in results.values() if v is not None)
    print(f"\n{'🎉 All good! Camera is ready.' if all_ok else '⚠️  Some tests failed — check output above.'}")


if __name__ == "__main__":
    main()
