#!/bin/bash
# Pi Camera Client - Setup Script
# Run this on each Raspberry Pi

set -e

echo "=== Pi Camera Client Setup ==="

# Update system
sudo apt update
sudo apt install -y python3-pip python3-venv ffmpeg dnsmasq

# Create virtual environment
python3 -m venv ~/pi-camera-env
source ~/pi-camera-env/bin/activate

# Install Python dependencies
pip install -r requirements.txt

echo ""
echo "=== Network Setup ==="
echo "Configuring static IP on eth0 for camera connection..."

# Backup existing config
sudo cp /etc/dhcpcd.conf /etc/dhcpcd.conf.bak 2>/dev/null || true

# Add static IP for ethernet (camera side)
if ! grep -q "interface eth0" /etc/dhcpcd.conf; then
    echo "" | sudo tee -a /etc/dhcpcd.conf
    echo "# Pi Camera Client - static IP for camera" | sudo tee -a /etc/dhcpcd.conf
    echo "interface eth0" | sudo tee -a /etc/dhcpcd.conf
    echo "static ip_address=192.168.2.1/24" | sudo tee -a /etc/dhcpcd.conf
    echo "nolink" | sudo tee -a /etc/dhcpcd.conf
fi

# Configure dnsmasq for DHCP on ethernet
sudo tee /etc/dnsmasq.d/camera.conf > /dev/null << EOF
interface=eth0
dhcp-range=192.168.2.100,192.168.2.200,255.255.255.0,24h
EOF

sudo systemctl restart dnsmasq

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Usage:"
echo "  source ~/pi-camera-env/bin/activate"
echo "  python3 pi_camera_client.py \\"
echo "    --server http://YOUR_SERVER_IP:5000 \\"
echo "    --camera-ip 192.168.2.100 \\"
echo "    --camera-user admin \\"
echo "    --camera-pass YOUR_CAMERA_PASSWORD \\"
echo "    --pi-user pi_frontdoor \\"
echo "    --pi-pass YOUR_PI_SECRET"
echo ""
echo "Connect the Hikvision camera to the Pi's ethernet port."
echo "The camera will get an IP in the 192.168.2.x range."
echo "The Pi uses WiFi to connect to your server."
