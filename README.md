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
  - [Networking](#networking)
  - [Web UI Development](#web-ui-development)
  - [Customization & Configuration](#customization--configuration)
  - [Directory Structure](#directory-structure)
  - [Troubleshooting](#troubleshooting)

## Features
- Automated image build: injects Border0 CLI, Python Web UI, systemd services
- Bridge/zone networking like a modern router: multiple LAN bridges, a WAN zone
  with a selectable active uplink, and WiFi radios that can each be an AP or an
  upstream client — all driven by one declarative model
  (see [Networking architecture](docs/networking.md))
- Per-bridge DHCP/DNS (dnsmasq), NAT, and the `border0`/`gateway.border0` portal
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
- Raspberry Pi 3B+ or newer
- microSD card (≥ 8 GB), power supply
- Ethernet or Wi-Fi client device for connecting to Admin Panel

## Quick Start
[Click here to watch the installation guide](https://youtu.be/W6hoqRWbjvo)
[![Watch the video](https://img.youtube.com/vi/W6hoqRWbjvo/maxresdefault.jpg)](https://youtu.be/W6hoqRWbjvo)

## First Boot & Access
1. Insert the SD card into your Raspberry Pi and power it on.
2. The device will provision services (hostapd, dnsmasq, Border0 CLI, Web UI) and reboot.
3. Connect a client to the `border0` Wi-Fi SSID.
4. In your browser, navigate to **http://gateway.border0** or **http://10.10.10.10** or follow the captive portal.
5. Log in with your Border0 Account credentials.
6. Configure network (LAN bridges / WAN zone / WiFi), Border0 VPN (exit-node), view metrics, reboot, upgrade.

Out of the box the image ships one LAN bridge `lan0` (192.168.42.1/24, DHCP) with
the `wlan0` AP `border0` (open — secure it from the **WiFi** page) and `eth0` as
the WAN uplink. All of this lives in `/etc/border0/network.json`.

## Build the Image and Develop locally
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


## Networking

The network is described declaratively in `/etc/border0/network.json` (LAN
bridges, the WAN zone, per-radio WiFi mode). An apply engine renders and applies
the underlying bridge/DHCP/NAT/hostapd/wpa_supplicant config idempotently. The
web UI only edits `network.json` and never touches `/etc` directly.

Full reference — the model, the schema, what gets rendered, and hardware notes —
is in **[docs/networking.md](docs/networking.md)**. To apply by hand:
```bash
python3 -m gateway_admin.netconfig apply      # reconcile to network.json
python3 -m gateway_admin.netconfig validate   # check the model
```

## Web UI Development
```bash
cd webui
./setup.sh        # create venv, install Python deps
source venv/bin/activate
./run.sh          # launch dev server
```
- Access at `http://localhost:5000`
- Python under `webui/gateway_admin/`; HTML under `webui/templates/`, config
  templates under `webui/gateway_admin/templates/config/`.
- `netconfig.py` is the apply engine, `netutils.py` the shared interface
  discovery/scan/lease helpers. The UI writes `network.json` and calls
  `netconfig.apply()` — it does not write `/etc` itself.

## Customization & Configuration
- Override defaults via environment variables:
  ```bash
  export ADMIN_USERNAME=myadmin
  export ADMIN_PASSWORD=mysecurepass
  export SECRET_KEY=$(openssl rand -hex 16)
  ```
- To adjust build packages, edit `EXTRA_PKGS` and `REMOVE_PKGS` in `build/build_iso.sh`.
- To change the network the image ships with, edit the default `network.json`
  seeded in `build/build_iso.sh` (it's rendered into real config at build time by
  the apply engine). See [docs/networking.md](docs/networking.md).

## Directory Structure
```
.
├── build           # Image build scripts & systemd templates
│   ├── download_iso.sh
│   ├── build_iso.sh
│   └── templates   # systemd units (incl. border0-dnsmasq@), hostapd default
├── docs            # networking.md and other reference docs
├── iso             # Stock & custom Raspberry Pi OS images
├── webui           # Flask admin panel & assets
│   └── gateway_admin
│       ├── netconfig.py   # declarative network apply engine
│       ├── netutils.py    # interface discovery / scan / DHCP-lease helpers
│       └── templates/config  # bridge/wan/hostapd/wpa/dnsmasq/firewall templates
├── requirements.txt
└── Makefile        # build-iso, release, deploy shortcuts
```

## Troubleshooting
- **Missing qemu-user-static**: `sudo apt install qemu-user-static`
- **Permission denied** mounting loop devices: rerun build with `sudo`
- **Web UI unreachable**: check `border0-webui` status:
  ```bash
  sudo systemctl status border0-webui
  ```

## License

This project is licensed under the Apache License, Version 2.0. See the [LICENSE](LICENSE) file for details.

<!-- EOF -->
