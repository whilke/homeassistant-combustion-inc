#!/bin/bash
set -e

echo "🏠 Setting up Home Assistant Custom Component Development Environment"

# Configure git
git config --global --add safe.directory "${GITHUB_WORKSPACE:-/workspaces/homeassistant-combustion-inc}"
if [ -n "${GIT_EMAIL}" ]; then
    git config --global user.email "${GIT_EMAIL}"
fi
if [ -n "${GIT_NAME}" ]; then
    git config --global user.name "${GIT_NAME}"
fi

# Install Bluetooth system dependencies
echo "📡 Installing Bluetooth dependencies..."
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    bluez \
    libbluetooth-dev \
    libdbus-1-dev \
    libglib2.0-dev \
    libdbus-1-3

# Install Python dependencies
echo "📦 Installing Python dependencies..."
pip3 install --upgrade pip

# Install Home Assistant and other requirements
pip3 install -r requirements.txt

# Install additional packages for Bluetooth BLE support
pip3 install \
    bleak \
    bleak-retry-connector \
    bluetooth-adapters \
    bluetooth-auto-recovery \
    bluetooth-data-tools \
    dbus-fast \
    habluetooth

# Install development/debugging tools
pip3 install \
    debugpy \
    pytest \
    pytest-asyncio \
    pytest-cov \
    pylint \
    black \
    isort

# Install test requirements if they exist
if [ -f "requirements.test.txt" ]; then
    pip3 install -r requirements.test.txt
fi

# Create symlink for custom_components in HA config
echo "🔗 Setting up custom_components symlink..."
mkdir -p config/custom_components
if [ ! -L "config/custom_components/combustion_custom" ]; then
    ln -sf "$(pwd)/custom_components/combustion_custom" "config/custom_components/combustion_custom"
fi

echo "✅ Development environment setup complete!"
echo ""
echo "To run Home Assistant with debugging:"
echo "  1. Press F5 or use 'Run and Debug' in VS Code"
echo "  2. Or run: hass -c config --debug"
echo ""
echo "Home Assistant will be available at: http://localhost:8123"
