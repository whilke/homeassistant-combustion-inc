#!/bin/bash
# Host system setup script for Bluetooth passthrough to devcontainer
# Run this on your HOST machine (not inside the container)

set -e

echo "🔧 Setting up host system for Bluetooth passthrough..."

# Check if running as root for system changes
if [ "$EUID" -ne 0 ]; then
    echo "Some commands may require sudo. You may be prompted for your password."
fi

# Install Bluetooth packages
echo "📦 Installing Bluetooth packages..."
sudo apt-get update
sudo apt-get install -y bluez bluetooth libbluetooth-dev

# Enable and start Bluetooth service
echo "🔌 Enabling Bluetooth service..."
sudo systemctl enable bluetooth
sudo systemctl start bluetooth

# Enable and start D-Bus
echo "🚌 Ensuring D-Bus is running..."
sudo systemctl enable dbus
sudo systemctl start dbus

# Add current user to bluetooth group
echo "👤 Adding user to bluetooth group..."
sudo usermod -aG bluetooth "$USER"

# Check for Bluetooth adapters
echo ""
echo "🔍 Checking for Bluetooth adapters..."
if hciconfig 2>/dev/null | grep -q "hci"; then
    echo "✅ Bluetooth adapter found!"
    hciconfig -a
else
    echo "⚠️  No Bluetooth adapter detected!"
    echo ""
    echo "To use this integration, you need a Bluetooth adapter."
    echo "Recommended USB Bluetooth 5.0+ adapters:"
    echo "  - TP-Link UB500 (Bluetooth 5.0)"
    echo "  - ASUS USB-BT500 (Bluetooth 5.0)"
    echo "  - Any adapter with RTL8761B or similar chipset"
    echo ""
    echo "After plugging in a Bluetooth adapter, run:"
    echo "  sudo hciconfig hci0 up"
fi

# Set permissions for D-Bus socket
echo ""
echo "🔐 Setting up D-Bus permissions..."
if [ -d /run/dbus ]; then
    sudo chmod 755 /run/dbus
fi

echo ""
echo "✅ Host setup complete!"
echo ""
echo "Next steps:"
echo "1. If you just added yourself to the bluetooth group, log out and back in"
echo "2. Plug in a Bluetooth adapter if you haven't already"
echo "3. Rebuild your devcontainer: Ctrl+Shift+P -> 'Dev Containers: Rebuild Container'"
echo ""
echo "To verify Bluetooth is working:"
echo "  bluetoothctl show"
echo "  hciconfig -a"
