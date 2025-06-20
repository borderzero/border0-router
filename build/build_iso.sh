#!/bin/bash
# This command configures the shell to:
# - Exit immediately if a command exits with a non-zero status (-e)
# - Treat unset variables as an error when substituting (-u)
# - Prevent errors in a pipeline from being masked (-o pipefail)
# set -euo pipefail

# timestamp
TIMESTAMP=$(date +%Y-%m-%d-%H-%M)

# ========= CONFIGURATION =========

# Dynamically detect the stock Raspberry Pi OS image in the ../iso directory.
STOCK_ISO_XZ=$(find ../iso -maxdepth 1 -type f -name '*.img.xz' ! -name '*-border0-*' | head -n1)
if [ -z "${STOCK_ISO_XZ}" ]; then
    echo "Error: No stock image .img.xz file found in ../iso directory."
    exit 1
fi
ISO_BASE=$(basename "${STOCK_ISO_XZ}" .img.xz)
IMG_XZ="../iso/${ISO_BASE}.img.xz"
IMG="../iso/${ISO_BASE}.img"
BORDER0_IMG="../iso/${ISO_BASE}-border0-${TIMESTAMP}.img"

# Directories containing your additional files; adjust as needed.
SYSTEMD_UNITS_SRC="./templates"

# List additional packages to install (space separated)
EXTRA_PKGS="hostapd iptables dnsmasq tcpdump jq openssh-server"
# List unwanted packages to remove (space separated)
REMOVE_PKGS="modemmanager rsyslog"

# Default LAN/WAN interface names, override via environment variables
DEFAULT_LAN_IFACE="${DEFAULT_LAN_IFACE:-wlan0}"
DEFAULT_WAN_IFACE="${DEFAULT_WAN_IFACE:-eth0}"
# ==================================

# Mount point directories
MNT_ROOT="/mnt/rpi/root"
MNT_BOOT="${MNT_ROOT}/boot"

# 0. Check for qemu-aarch64-static on the host.
if [ ! -f "/usr/bin/qemu-aarch64-static" ]; then
    if [ "$(id -u)" -ne 0 ]; then
        echo "Installing qemu-user-static using sudo..."
        sudo apt-get install -y qemu-user-static
    else
        echo "Installing qemu-user-static..."
        apt-get install -y qemu-user-static
    fi
fi

# 1. Decompress the image if not already decompressed.
if [ ! -f "${IMG}" ]; then
    echo "Decompressing ${IMG_XZ}..."
    xz -d -k "${IMG_XZ}"
fi

# 2. Setup a loop device with partition scanning.
LOOP_DEV=$(losetup --find --partscan --show "${IMG}")
if [ -z "${LOOP_DEV}" ]; then
    echo "Error: Unable to setup loop device."
    # exit 1
fi
echo "Using loop device: ${LOOP_DEV}"

# Typically, Raspberry Pi OS images have two partitions:
# - Partition 1: Boot partition
# - Partition 2: Root filesystem
BOOT_PART="${LOOP_DEV}p1"
ROOT_PART="${LOOP_DEV}p2"

# 3. Create mount directories and mount partitions.
mkdir -p "${MNT_ROOT}"
mkdir -p "${MNT_BOOT}"

echo "Mounting root filesystem (${ROOT_PART}) to ${MNT_ROOT}..."
mount "${ROOT_PART}" "${MNT_ROOT}"

echo "Mounting boot partition (${BOOT_PART}) to ${MNT_BOOT}..."
mount "${BOOT_PART}" "${MNT_BOOT}"

# 4. Prepare the chroot environment.
echo "Mounting pseudo-filesystems..."
mount -t proc /proc "${MNT_ROOT}/proc"
mount -t sysfs /sys "${MNT_ROOT}/sys"
mount --bind /dev "${MNT_ROOT}/dev"
# Copy DNS resolution settings for networking in chroot.
cp -v /etc/resolv.conf "${MNT_ROOT}/etc/resolv.conf"
echo "nameserver 8.8.8.8" >> "${MNT_ROOT}/etc/resolv.conf"
echo "nameserver 8.8.4.4" >> "${MNT_ROOT}/etc/resolv.conf"

# Bind-mount qemu-aarch64-static into the chroot.
echo "Copy qemu-aarch64-static into the image..."
mkdir -p "${MNT_ROOT}/usr/bin"
cp -v /usr/bin/qemu-aarch64-static "${MNT_ROOT}/usr/bin/"


# create a /opt/border0 directory into the chroot
mkdir -p "${MNT_ROOT}/opt/border0"
# make etc bin sbin in opt/border0
mkdir -p "${MNT_ROOT}/opt/border0/etc"
mkdir -p "${MNT_ROOT}/opt/border0/bin"
mkdir -p "${MNT_ROOT}/opt/border0/sbin"

BORDER0_BIN="${MNT_ROOT}/usr/local/bin/border0"
BORDER0_CACHE="../iso/border0"

# Create cache directory if it doesn't exist
mkdir -p "$(dirname "${BORDER0_CACHE}")"

# Download only if not in cache
if [ ! -f "${BORDER0_CACHE}" ]; then
    echo "Downloading border0 binary to cache..."
    curl -s https://download.border0.com/linux_arm64/border0 -o "${BORDER0_CACHE}"
    chmod +x "${BORDER0_CACHE}"
fi

# Copy from cache to target location
cp "${BORDER0_CACHE}" "${BORDER0_BIN}"
chmod +x "${BORDER0_BIN}"

echo "Copying webui directory into the image..."
cp -vr ../webui "${MNT_ROOT}/opt/border0/"

echo "copy requirements.txt into the image..."
cp -v ../requirements.txt "${MNT_ROOT}/opt/border0/"

# copy ../templates into opt/border0/templates
echo "Copying templates into the image..."
cp -rv "./templates" "${MNT_ROOT}/opt/border0/templates"


# copy the border0 systemd unit file into the chroot
echo "Copying systemd unit files into the image..."
cp -v "${SYSTEMD_UNITS_SRC}/border0-webui.service" "${MNT_ROOT}/etc/systemd/system/border0-webui.service"
cp -v "${SYSTEMD_UNITS_SRC}/border0-device.service" "${MNT_ROOT}/etc/systemd/system/border0-device.service"
cp -v "${SYSTEMD_UNITS_SRC}/border0-metrics.service" "${MNT_ROOT}/etc/systemd/system/border0-metrics.service"


# 5. Create a modification script inside the chroot.
CHROOT_SCRIPT="${MNT_ROOT}/tmp/chroot_mod.sh"
cat <<'EOF' > "${CHROOT_SCRIPT}"
#!/bin/bash
set -euo pipefail
# Update package lists.
apt-get update

# Install additional packages.
apt-get install -y PACKAGE_LIST_PLACEHOLDER

# Remove unwanted packages.
apt-get remove -y UNWANTED_PKGS_PLACEHOLDER

cp /opt/border0/templates/hostapd.conf.default /etc/hostapd/wlan0.conf

apt-get purge -y man-db manpages manpages-posix libx11-doc

systemctl stop NetworkManager
# systemctl disable NetworkManager
apt purge -y network-manager network-manager-gnome

apt install -y ifupdown
mkdir -p /etc/network/interfaces.d

echo """
auto lo
iface lo inet loopback

# interfaces(5) file used by ifup(8) and ifdown(8)
# Include files from /etc/network/interfaces.d:
source /etc/network/interfaces.d/*
""" > /etc/network/interfaces

echo """
allow-hotplug eth0
auto eth0
iface eth0 inet dhcp
""" > /etc/network/interfaces.d/eth0.conf

echo """
allow-hotplug wlan0
auto wlan0
iface wlan0 inet static
    pre-up /usr/sbin/rfkill unblock wlan
    post-up /usr/sbin/dnsmasq -I lo -i wlan0 --bind-interfaces -K -z -F wlan0,192.168.42.10,192.168.42.250,5m --dhcp-option=3,192.168.42.1 --dhcp-option=6,192.168.42.1 --address=/gateway.border0/10.10.10.10 --log-dhcp --log-facility=/var/log/dnsmasq_wlan0.log
    post-up /sbin/iptables -t nat -A POSTROUTING -s 192.168.42.0/24 -o eth+ -j MASQUERADE
    post-up /sbin/iptables -t nat -A POSTROUTING -s 192.168.42.0/24 -o utun+ -j MASQUERADE
    post-down /sbin/iptables -t nat -D POSTROUTING -s 192.168.42.0/24 -o eth+ -j MASQUERADE
    post-down /sbin/iptables -t nat -D POSTROUTING -s 192.168.42.0/24 -o utun+ -j MASQUERADE
    address 192.168.42.1
    netmask 255.255.255.0
    # gateway 192.168.42.1
    # dns-nameservers 208.67.220.220
    broadcast 192.168.42.255
""" > /etc/network/interfaces.d/wlan0.conf

echo """
auto dummy0
iface dummy0 inet static
    pre-up /usr/sbin/modprobe dummy
    pre-up /sbin/ip link add dummy0 type dummy || true
    address 10.10.10.10
    netmask 255.255.255.255
""" > /etc/network/interfaces.d/dummy0.conf

mkdir -p /etc/sysconfig
echo """
SHELL=/bin/bash
LOGNAME=root
HOME=/root
USER=root
""" > /etc/sysconfig/border0-gw

echo "Copying border0 config files into the image..."
mkdir -p /etc/border0
echo "wlan0" > /etc/border0/lan_interface 
echo "eth0" > /etc/border0/wan_interface 

sync

echo "Cleaning out APT caches..."
apt-get autoremove -y
apt-get clean

echo "Removing any leftover files..."
rm -rf /usr/share/man/*
rm -rf /usr/share/doc/*
rm -rf /usr/share/info/*


# enable forwarding
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
echo "net.ipv6.conf.all.forwarding=1" >> /etc/sysctl.conf


echo "Copying authorized_keys into the image..."
mkdir -p /root/.ssh
echo "# $(date)" >> /root/.ssh/authorized_keys

mkdir -p /home/pi/.ssh
cat /root/.ssh/authorized_keys >> /home/pi/.ssh/authorized_keys
chown -R pi:pi /home/pi/.ssh


echo "Running webui setup script..."
cd /opt/border0/webui
./setup.sh

echo "Copy all files to /opt/border0/defaults for factory reset restoration"
cp -rv /etc/network/interfaces.d/dummy0.conf /opt/border0/defaults/etc/network/interfaces.d/dummy0.conf
cp -rv /etc/network/interfaces.d/wlan0.conf /opt/border0/defaults/etc/network/interfaces.d/wlan0.conf
cp -rv /etc/network/interfaces.d/eth0.conf /opt/border0/defaults/etc/network/interfaces.d/eth0.conf
cp -rv /etc/hostapd/wlan0.conf /opt/border0/defaults/etc/hostapd/wlan0.conf




echo "done"
EOF

# Replace placeholders with actual package lists.
sed -i "s/PACKAGE_LIST_PLACEHOLDER/${EXTRA_PKGS}/g" "${CHROOT_SCRIPT}"
sed -i "s/UNWANTED_PKGS_PLACEHOLDER/${REMOVE_PKGS}/g" "${CHROOT_SCRIPT}"
# Override default LAN/WAN interface in chroot modification script
sed -i "s|echo \"wlan0\".*|echo \"${DEFAULT_LAN_IFACE}\" > /etc/border0/lan_interface|" "${CHROOT_SCRIPT}"
sed -i "s|echo \"eth0\".*|echo \"${DEFAULT_WAN_IFACE}\" > /etc/border0/wan_interface|" "${CHROOT_SCRIPT}"

chmod +x "${CHROOT_SCRIPT}"

# 6. Chroot into the image (using QEMU via the bind mount) and run the modification script.
echo "Entering chroot to modify the image..."
chroot "${MNT_ROOT}" /bin/bash /tmp/chroot_mod.sh

echo "Enabling systemd units in the image..."
systemctl --root="${MNT_ROOT}" unmask hostapd
echo "disabled hostapd"
systemctl --root="${MNT_ROOT}" disable hostapd.service
echo "enabled hostapd@wlan0"
systemctl --root="${MNT_ROOT}" enable hostapd@wlan0
echo "disabled dnsmasq"
systemctl --root="${MNT_ROOT}" disable dnsmasq
echo "enabled border0-webui"
systemctl --root="${MNT_ROOT}" enable border0-webui
echo "enabled border0-device"
systemctl --root="${MNT_ROOT}" enable border0-device
echo "enabled border0-metrics"
systemctl --root="${MNT_ROOT}" enable border0-metrics
echo "disabled triggerhappy"
systemctl --root="${MNT_ROOT}" disable triggerhappy.service
echo "disabled avahi-daemon"
systemctl --root="${MNT_ROOT}" disable avahi-daemon.service
echo "disabled rpcbind"
systemctl --root="${MNT_ROOT}" disable rpcbind.service
echo "disabled bluetooth"
systemctl --root="${MNT_ROOT}" disable bluetooth.service
echo "disabled bluetooth-data-storage"
systemctl --root="${MNT_ROOT}" enable ssh
echo "enabled ssh"



if [ "${EDIT_CHROOT:-false}" = "true" ]; then
    echo "Dropping into chroot shell..."
    chroot "${MNT_ROOT}" /bin/bash
    PS1='(chroot) \u@\h:\w\$ '
fi
# # and then run the modification script with the following command:
# # /tmp/chroot_mod.sh

echo "Copying border0 config files into the image..."
mkdir -p "${MNT_ROOT}/etc/border0"
echo "wlan0" > "${MNT_ROOT}/etc/border0/lan_interface"
echo "eth0" > "${MNT_ROOT}/etc/border0/wan_interface"

echo "lan_interface: $(cat "${MNT_ROOT}/etc/border0/lan_interface")"
echo "wan_interface: $(cat "${MNT_ROOT}/etc/border0/wan_interface")"

# if INSTALL_LOCAL_SSH_KEY is true install ssh key from ~/.ssh/id_ed25519.pub into authorized_keys
if [ "${INSTALL_LOCAL_SSH_KEY:-false}" = "true" ]; then
    echo "Installing ssh key from ~/.ssh/id_ed25519.pub into authorized_keys"
    mkdir -p "${MNT_ROOT}/root/.ssh"
    if [ -f ~/.ssh/id_ed25519.pub ]; then
        cat ~/.ssh/id_ed25519.pub >> "${MNT_ROOT}/root/.ssh/authorized_keys"
    elif [ -f ~/.ssh/id_rsa.pub ]; then 
        cat ~/.ssh/id_rsa.pub >> "${MNT_ROOT}/root/.ssh/authorized_keys"
    else
        echo "Error: ~/.ssh/id_ed25519.pub or ~/.ssh/id_rsa.pub not found"
    fi
fi

# 8. Cleanup: Unmount pseudo-filesystems, the qemu bind mount, and partitions.
echo "Cleaning up chroot environment..."
umount "${MNT_ROOT}/proc"
umount "${MNT_ROOT}/sys"
umount "${MNT_ROOT}/dev"
rm -f "${MNT_ROOT}/usr/bin/qemu-aarch64-static"

echo "Unmounting boot and root partitions..."
umount "${MNT_BOOT}"
umount "${MNT_ROOT}"

# Detach the loop device.
echo "Detaching loop device ${LOOP_DEV}..."
losetup -d "${LOOP_DEV}"

sync

mv "${IMG}" "${BORDER0_IMG}"
echo "Modified image saved as ${BORDER0_IMG}"


if [ "${CREATE_XZ:-false}" = "true" ]; then
    echo "Creating highly compressed image with multi-threading..."
    xz -9 -k -T0 "${BORDER0_IMG}"
    echo "Compressed image saved as ${BORDER0_IMG}.xz"
fi

echo "Image modification complete. The modified image is ready to be written to an SD card."
