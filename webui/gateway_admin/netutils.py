"""Shared network-discovery helpers.

Pure read-only system inspection: sysfs walks, hostapd config parsing, a couple
of best-effort subprocess calls. No Flask, no request/flash — this has to be
importable and unit-testable off-device. Everything degrades gracefully: a
missing file or absent command returns ''/[]/None, never raises. These run on
dev boxes too, where none of /sys/class/net/wlan*, hostapd or iw exist.
"""

import os
import re
import json
import socket
import subprocess
import time

import psutil


# --- Wi-Fi constants -------------------------------------------------------

# Selectable Wi-Fi channels per band. 5 GHz is restricted to non-DFS channels
# because the AP config runs with ieee80211h=0 — DFS channels (52-144) need
# radar handling we don't enable. ACS (channel=0) is intentionally NOT offered:
# the Pi's WiFi drivers (brcmfmac, rtw_8821cu) don't implement the nl80211
# survey it needs, so it starts and then silently fails to bring up the AP.
CHANNELS_2G = [str(c) for c in range(1, 12)]                  # 1-11 (US)
CHANNELS_5G = ['36', '40', '44', '48', '149', '153', '157', '161']
DEFAULT_CHANNEL = {'g': '6', 'a': '36'}
# VHT 80 MHz center-frequency segment index per 5 GHz channel: the center of
# the 80 MHz block the channel belongs to (36-48 -> 42, 149-161 -> 155). A
# channel must set this to match its block or the radio won't come up.
VHT80_SEG0 = {
    '36': '42', '40': '42', '44': '42', '48': '42',
    '149': '155', '153': '155', '157': '155', '161': '155',
}


def _allowed_channels(hw_mode):
    return CHANNELS_2G if hw_mode == 'g' else CHANNELS_5G


def _display_channel(raw, hw_mode):
    """Return a valid channel to preselect in the UI for a stored config.

    Falls back to the band default if the stored value is empty/invalid (e.g.
    channel=0 left by an old ACS config, or a DFS channel we no longer list).
    """
    band = hw_mode if hw_mode in ('g', 'a') else 'g'
    raw = (raw or '').strip()
    return raw if raw in _allowed_channels(band) else DEFAULT_CHANNEL[band]


# --- Interface discovery ---------------------------------------------------

_NET_DIR = '/sys/class/net'
_IFACE_RE = re.compile(r'^(eth|wlan)\d+$')


def list_interfaces(kinds=('eth', 'wlan')):
    """Physical iface names matching ^(eth|wlan)\\d+$ from sysfs, sorted.

    `kinds` narrows the set (e.g. ('wlan',) for wireless only).
    """
    out = []
    if not os.path.isdir(_NET_DIR):
        return out
    try:
        names = os.listdir(_NET_DIR)
    except Exception:
        return out
    for iface in sorted(names):
        if not _IFACE_RE.match(iface):
            continue
        if not any(iface.startswith(k) for k in kinds):
            continue
        out.append(iface)
    return out


def is_wireless(iface):
    """True if the iface has a wireless sysfs node."""
    return os.path.isdir(f'{_NET_DIR}/{iface}/wireless')


def iface_type(iface):
    return 'WiFi' if is_wireless(iface) else 'Ethernet'


def iface_phy(iface):
    """Basename of the iface's phy80211 link, for same-radio detection.

    Two vifs on one chip share a phy; None if not wireless / no phy node.
    """
    if not is_wireless(iface):
        return None
    link = f'{_NET_DIR}/{iface}/phy80211'
    try:
        return os.path.basename(os.path.realpath(link))
    except Exception:
        return None


def iface_status(iface):
    """{status, ipv4, ipv6} from psutil. Empty strings when unknown."""
    stats = psutil.net_if_stats().get(iface)
    addrs = psutil.net_if_addrs().get(iface, [])
    status = 'up' if (stats and stats.isup) else 'down'
    ipv4 = next((a.address for a in addrs if a.family == socket.AF_INET), '')
    ipv6 = next((a.address for a in addrs if a.family == socket.AF_INET6), '')
    return {'status': status, 'ipv4': ipv4, 'ipv6': ipv6}


def _iface_mode(iface):
    """Wireless mode (AP/managed) via iwconfig, else None. Best-effort."""
    if not is_wireless(iface):
        return None
    try:
        result = subprocess.run(['iwconfig', iface], capture_output=True, text=True, timeout=2)
        text = result.stdout or result.stderr or ''
    except Exception:
        return None
    m = re.search(r'Mode:(\S+)', text)
    if not m:
        return None
    mode = m.group(1)
    # iwconfig says "Master" for an AP; normalize to the friendlier label.
    return 'AP' if mode == 'Master' else mode


def iface_table(names):
    """Rows for the "Available Interfaces" table: name/type/status/ip/mode."""
    iface_stats = psutil.net_if_stats()
    iface_addrs = psutil.net_if_addrs()
    rows = []
    for iface in names:
        is_up = iface_stats.get(iface).isup if iface in iface_stats else False
        status = 'UP' if is_up else 'no_carrier'
        ip_addr = 'none'
        for addr in iface_addrs.get(iface, []):
            if addr.family == socket.AF_INET:
                ip_addr = addr.address
                break
        rows.append({
            'name': iface,
            'type': iface_type(iface),
            'status': status,
            'ip': ip_addr,
            'mode': _iface_mode(iface),
        })
    return rows


def list_wifi_radios():
    """wlanX iface names that actually expose a wireless node."""
    return [i for i in list_interfaces(kinds=('wlan',)) if is_wireless(i)]


# --- hostapd / service state ----------------------------------------------

_HOSTAPD_DIR = '/etc/hostapd'


def read_hostapd(iface):
    """Parse /etc/hostapd/<iface>.conf -> {ssid,hw_mode,channel,wpa_passphrase}.

    None if the file is missing/unreadable. Plain key=value, comments skipped.
    """
    cfg_file = os.path.join(_HOSTAPD_DIR, f'{iface}.conf')
    if not os.path.isfile(cfg_file):
        return None
    ssid = hw_mode = wpa_passphrase = channel = ''
    try:
        with open(cfg_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                if k == 'ssid':
                    ssid = v
                elif k == 'hw_mode':
                    hw_mode = v
                elif k == 'wpa_passphrase':
                    wpa_passphrase = v
                elif k == 'channel':
                    channel = v
    except Exception:
        return None
    return {'ssid': ssid, 'hw_mode': hw_mode, 'channel': channel,
            'wpa_passphrase': wpa_passphrase}


def hostapd_service_state(iface):
    """{enabled, active} for hostapd@<iface> via systemctl. False if it errors."""
    service = f'hostapd@{iface}'
    try:
        en = subprocess.run(['systemctl', 'is-enabled', service], capture_output=True, text=True, timeout=2)
        enabled = en.stdout.strip() == 'enabled'
    except Exception:
        enabled = False
    try:
        act = subprocess.run(['systemctl', 'is-active', service], capture_output=True, text=True, timeout=2)
        active = act.stdout.strip() == 'active'
    except Exception:
        active = False
    return {'enabled': enabled, 'active': active}


def iwconfig_stats(iface):
    """Raw `iwconfig <iface>` output, best-effort. Empty-ish string on failure."""
    try:
        result = subprocess.run(['iwconfig', iface], capture_output=True, text=True, timeout=2)
        return result.stdout or result.stderr
    except Exception:
        return 'Unable to retrieve interface statistics'


# --- Wi-Fi scan (NEW) ------------------------------------------------------

def scan_wifi(iface):
    """Scan for nearby APs -> [{ssid, signal, security}]. [] on any failure.

    Runs `iw dev <iface> scan`, which needs root and the iface up. NEW code with
    no existing equivalent — NEEDS ON-DEVICE TESTING; the parse below is built
    against typical iw output and may want tweaking once we see real radios.
    """
    try:
        result = subprocess.run(['iw', 'dev', iface, 'scan'],
                                capture_output=True, text=True, timeout=15)
    except Exception:
        return []
    if result.returncode != 0 or not result.stdout:
        return []

    networks = []
    cur = None
    for raw in result.stdout.splitlines():
        line = raw.strip()
        # Each AP starts with a "BSS <mac>" header.
        if line.startswith('BSS '):
            if cur is not None:
                networks.append(cur)
            cur = {'ssid': '', 'signal': '', 'security': 'open'}
            continue
        if cur is None:
            continue
        if line.startswith('SSID:'):
            cur['ssid'] = line[len('SSID:'):].strip()
        elif line.startswith('signal:'):
            # e.g. "signal: -57.00 dBm"
            m = re.search(r'(-?\d+(?:\.\d+)?)', line)
            if m:
                cur['signal'] = m.group(1)
        elif line.startswith('RSN:'):
            cur['security'] = 'WPA2/WPA3'
        elif line.startswith('WPA:') and cur['security'] == 'open':
            cur['security'] = 'WPA'
    if cur is not None:
        networks.append(cur)
    return networks


# --- DHCP clients ----------------------------------------------------------

_CACHE_DIR = '/etc/border0'
_LEASE_TTL = 3600  # cache stale after 60 min


def human(n):
    """Human-readable byte count."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def dhcp_clients(iface, refresh=False):
    """Leases seen on <iface>: dnsmasq log + neigh/arp, with a TTL cache.

    Merges DHCPACK lines from /var/log/dnsmasq_<iface>.log with the live
    neighbor table (`ip neigh`, /proc/net/arp fallback). Cache lives at
    /etc/border0/dhcp_clients_<iface>.json; rebuilt when stale or refresh=True.
    Returns a list of lease dicts; [] on total failure.
    """
    dhcp_cache_file = os.path.join(_CACHE_DIR, f'dhcp_clients_{iface}.json')
    # stale if missing or older than the TTL, or on explicit refresh
    stale = False
    try:
        if time.time() - os.path.getmtime(dhcp_cache_file) > _LEASE_TTL:
            stale = True
    except Exception:
        stale = True
    if refresh:
        stale = True

    leases = {}
    if not stale:
        try:
            with open(dhcp_cache_file) as f:
                leases = {c['mac']: c for c in json.load(f)}
        except Exception:
            leases = {}
    if stale:
        log_file = f'/var/log/dnsmasq_{iface}.log'
        try:
            output = subprocess.check_output(['tail', '-n', '10000', log_file], text=True)
            for line in reversed(output.splitlines()):
                m = re.search(r'DHCPACK\([^)]*\)\s+(\d+\.\d+\.\d+\.\d+)\s+([0-9A-Fa-f:]+)\s+(\S+)', line)
                if m:
                    ip, mac, hostname = m.group(1), m.group(2).lower(), m.group(3)
                    if mac not in leases:
                        leases[mac] = {'hostname': hostname, 'ip': ip, 'mac': mac}
            try:
                import manuf
                parser = manuf.MacParser()
                for v in leases.values():
                    v['manufacturer'] = parser.get_manuf(v['mac']) or ''
            except Exception:
                for v in leases.values():
                    v['manufacturer'] = ''
            tmp = dhcp_cache_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(list(leases.values()), f)
            os.replace(tmp, dhcp_cache_file)
        except Exception:
            pass

    try:
        # fetch neighbor table JSON; ignore non-zero exit codes so we still parse entries even if some are FAILED
        proc = subprocess.run(
            ['ip', '-j', 'neigh', 'show', 'dev', iface],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True
        )
        neigh_entries = json.loads(proc.stdout or '[]')
        for entry in neigh_entries:
            # if JSON includes device, skip other interfaces; otherwise assume filtered by 'show dev'
            dev = entry.get('dev')
            if dev and dev != iface:
                continue
            dst = entry.get('dst')
            # only IPv4
            if not dst or ':' in dst:
                continue
            mac = entry.get('lladdr', '').lower()
            if not mac:
                continue
            state = entry.get('state')
            if isinstance(state, list) and state:
                state = state[0]
            # skip unreachable entries
            if state == 'FAILED':
                continue
            existing = leases.get(mac)
            if existing:
                # update only when IP matches or to track IP changes
                if existing.get('ip') == dst:
                    existing['state'] = state
                else:
                    existing['state'] = state
                    existing['ip'] = dst
            else:
                leases[mac] = {
                    'hostname': None,
                    'ip': dst,
                    'mac': mac,
                    'manufacturer': '',
                    'state': state
                }
    except Exception:
        try:
            with open('/proc/net/arp') as arp_f:
                lines = arp_f.readlines()[1:]
            for line in lines:
                parts = line.split()
                if parts[5] == iface:
                    ip_addr, mac_addr = parts[0], parts[3].lower()
                    if mac_addr not in leases:
                        leases[mac_addr] = {
                            'hostname': None,
                            'ip': ip_addr,
                            'mac': mac_addr,
                            'manufacturer': ''
                        }
        except Exception:
            pass

    return list(leases.values())
