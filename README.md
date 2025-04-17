# Border0 Pi

Border0 Pi transforms a Raspberry Pi into a secure Wi‑Fi gateway with a captive‑portal setup and Border0 VPN integration.

## Prerequisites
- Linux host (Debian/Ubuntu) with: `make`, `bash`, `curl`, `losetup`, `mount`, `sudo`, `qemu-user-static`, `dd`
- Raspberry Pi 4 (or compatible), SD card (8 GB+)
- Internet connection to download base image and dependencies

## Quickstart Guide
1. Clone this repository:
   ```sh
   git clone https://github.com/border0/border0-pi.git
   cd border0-pi
   ```
2. Build the web UI daemon for ARM64 and AMD64:
   ```sh
   make build-all
   ```
3. Download the Raspberry Pi OS Lite (ARM64) base image:
   ```sh
   make download-iso
   ```
4. Customize and assemble the Border0 image (requires sudo):
   ```sh
   sudo make build-iso
   ```
   This script will:
   - Decompress and mount the stock OS image under `/mnt`
   - Install/remove packages (hostapd, dnsmasq, iptables, etc.)
   - Copy the Border0 binary, web UI, and systemd services into the image
   - Configure network interfaces, captive‑portal rules, and hostapd defaults
   - Unmount, detach, and save a timestamped Border0 image in `iso/`
5. Flash the generated image to your SD card (replace `/dev/sdX`):
   ```sh
   sudo dd if=iso/2024-11-19-raspios-bookworm-arm64-lite-border0-<timestamp>.img \
       of=/dev/sdX bs=4M status=progress && sync
   ```
6. Insert the SD card into the Raspberry Pi and power it on.

## Initial Setup
- On first boot, the Pi creates an AP:
  - SSID: `border0`
  - PSK: `border0123`
- Connect a laptop or mobile to this Wi‑Fi network.
- Open a browser to `http://gateway.border0` and follow the captive‑portal steps:
  1. Set your home Wi‑Fi SSID and password.
  2. The Pi will reboot into client mode and join your network.

## Web UI Features
- Wi‑Fi configuration (SSID/PSK)
- Internet access toggle (iptables flush)
- Border0 authentication token entry
- Exit‑node listing and selection via the Border0 CLI
- System status and logs
- Factory reset back to defaults

## Further Reading
- Inspect build scripts in `build/`
- Web UI source: `webui/daemon/`, `webui/ui/`
- Systemd services templates: `build/templates/`
  
For detailed developer notes, see the source code and inline comments.

