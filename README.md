# border0-router (Border0 Pi)

Border0 Router transforms a Raspberry Pi into a secure Wi-Fi gateway and captive-portal with integrated Border0 VPN. It provides:
  - Custom Raspberry Pi OS image build with pre-installed Border0 CLI and Web UI
  - Hostapd-based Wi-Fi Access Point and captive portal
  - Border0 VPN onboarding: organization setup, authentication, install & start node
  - Historical system metrics collection service
  - Flask-based Gateway Admin Panel for network, VPN, and system management

## Table of Contents
  - [Features](#features)
  - [Prerequisites](#prerequisites)
  - [Quick Start](#quick-start)
  - [First Boot & Access](#first-boot--access)
  - [Web UI Development](#web-ui-development)
  - [Customization & Configuration](#customization--configuration)
  - [Directory Structure](#directory-structure)
  - [Troubleshooting](#troubleshooting)

## Features
- Automated image build: injects Border0 CLI, Python Web UI, systemd services
- Captive-portal Wi-Fi AP (`border0` SSID) with static DHCP/DNS (dnsmasq)
- VPN configuration without shell: full onboarding via Web UI
- Background metrics collector for CPU, memory, disk, network (24 h history)

## Prerequisites
### Host Machine (build environment)
- Git, Bash, coreutils, curl, sed, xz-utils
- `losetup`, `mount`, `chroot`, `qemu-user-static` (for ARM64 chroot)
- Make (optional): Makefile provides shortcuts (`make download-iso`, `make build-iso`)
- **Raspberry Pi Imager** (Linux, macOS, Windows) for flashing SD cards
  - Download from: https://www.raspberrypi.com/software/
  - On Debian/Ubuntu: `sudo apt update && sudo apt install rpi-imager`
  - On Ubuntu (snap): `sudo snap install rpi-imager`

### Raspberry Pi (target device)
- Raspberry Pi 3/4/CM4 or newer
- microSD card (≥ 16 GB), power supply
- Ethernet or Wi-Fi client device for connecting to Admin Panel

## Quick Start
[![Watch the video](https://img.youtube.com/vi/W6hoqRWbjvo/maxresdefault.jpg)](https://youtu.be/W6hoqRWbjvo)

1. Clone the repo:
   ```bash
   git clone https://github.com/borderzero/border0-router.git
   cd border0-router
   ```
2. Download the stock Raspberry Pi OS ARM64 Lite image:
   ```bash
   make download-iso
   ```
3. Build the custom Border0 image (requires sudo for loop mounts):
   ```bash
   sudo make build-iso
   ```
   - To generate a compressed `.img.xz`, prefix with `CREATE_XZ=true`:
     ```bash
     sudo CREATE_XZ=true make build-iso
     ```
4. Flash to microSD using **Raspberry Pi Imager**:
   1. Launch **Imager**, click **CHOOSE OS → Use custom**, select `iso/*-border0-*.img`
   2. Click **CHOOSE STORAGE**, select your SD card
   3. Click **WRITE** and wait for completion
   
   <small>Or use `dd`:
   ```bash
   sudo dd if=$(ls iso/*-border0-*.img | head -1) of=/dev/sdX bs=4M conv=fsync status=progress
   ```</small>

## First Boot & Access
1. Insert the SD card into your Raspberry Pi and power it on.
2. The device will provision services (hostapd, dnsmasq, Border0 CLI, Web UI) and reboot.
3. Connect a client to the `border0` Wi-Fi SSID.
4. In your browser, navigate to **http://gateway.border0**(http://10.10.10.10) or follow the captive portal.
5. Log in with your SSO credentials straight into your Border0 organization.
6. Configure your Wi-Fi password, select border0 [exit-node](https://docs.border0.com/docs/exit-node), view metrics, reboot, upgrade and more...

## Web UI Development
-```bash
cd webui
./setup.sh        # create venv, install Python deps
source venv/bin/activate
./run.sh          # launch development server

# For production deployment, install and use Gunicorn:
pip install gunicorn
gunicorn --workers 3 --bind 0.0.0.0:80 --factory gateway_admin.app:create_app
```
- Access at `http://localhost:5000`
- Python code under `webui/gateway_admin/`, static templates under `webui/static/`.

## Customization & Configuration
- Override the session secret via environment variable:
  ```bash
  export SECRET_KEY=$(openssl rand -hex 16)
  ```
- To adjust build packages, edit `EXTRA_PKGS` and `REMOVE_PKGS` in `build/build_iso.sh`.

- To override the default LAN/WAN interfaces baked into the image, set the environment variables before building:
  ```bash
  export DEFAULT_LAN_IFACE=eth1
  export DEFAULT_WAN_IFACE=eth0
  sudo make build-iso
  ```

## Directory Structure
```
. ├── build        # Image build scripts & systemd templates
    ├── download_iso.sh
    ├── build_iso.sh
    └── templates
  ├── iso          # Stock & custom Raspberry Pi OS images
  ├── webui        # Flask admin panel & assets
  ├── requirements.txt
  └── Makefile     # Build & deploy shortcuts
```

## Troubleshooting
- **Missing qemu-user-static**: `sudo apt install qemu-user-static`
- **Permission denied** mounting loop devices: rerun build with `sudo`
- **Web UI unreachable**: check `border0-webui` status:
  ```bash
  sudo systemctl status border0-webui
  ```

<!-- EOF -->