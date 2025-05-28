import os
import re
import ipaddress
import subprocess
from flask import Blueprint, render_template, request, flash, redirect, url_for, current_app, abort
from flask_login import login_required
import socket
import psutil

lan_bp = Blueprint('lan', __name__, url_prefix='/lan')

# Default static configuration for LAN (used as initial values)
DEFAULT_LAN_STATIC_CFG = {
    'address': '192.168.42.1',
    'netmask': '255.255.255.0',
    'gateway': '192.168.42.1',
    'dns-nameservers': '208.67.220.220 8.8.8.8',
    'broadcast': '192.168.42.255'
}

@lan_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    # Discover LAN interfaces: only ethX or wlanX, excluding selected WAN
    net_dir = '/sys/class/net'
    interfaces = []
    wan_iface = None
    wan_iface_path = current_app.config.get('WAN_IFACE_PATH')
    try:
        with open(wan_iface_path) as f:
            wan_iface = f.read().strip()
    except Exception:
        wan_iface = None
    if os.path.isdir(net_dir):
        for iface in sorted(os.listdir(net_dir)):
            # only physical ethX or wlanX
            if not re.match(r'^(eth|wlan)\d+$', iface):
                continue
            if iface == wan_iface:
                continue
            interfaces.append(iface)

    # Load current LAN interface selection and static config
    current_iface = None
    static_cfg = DEFAULT_LAN_STATIC_CFG.copy()
    lan_iface_path = current_app.config.get('LAN_IFACE_PATH')
    try:
        with open(lan_iface_path) as f:
            current_iface = f.read().strip()
    except Exception:
        current_iface = None

    if request.method == 'POST':
        action = request.form.get('action')
        iface = request.form.get('iface')
        # Restart interface
        if action == 'restart':
            if iface not in interfaces:
                flash('Invalid interface selected for LAN', 'warning')
                return redirect(url_for('lan.index'))
            try:
                subprocess.run(['ifdown', iface], capture_output=True, text=True, timeout=10)
                subprocess.run(['ifup', iface], capture_output=True, text=True, timeout=10)
                flash(f'LAN interface {iface} restarted', 'success')
            except Exception as e:
                flash(f'Failed to restart LAN interface: {e}', 'danger')
            return redirect(url_for('lan.index'))
        # Save new configuration
        if iface not in interfaces:
            flash('Invalid interface selected for LAN', 'warning')
            return redirect(url_for('lan.index'))
        # Read form fields: network (fixed /24) and separate DNS entries
        network_str = request.form.get('network', '').strip()
        dns1 = request.form.get('dns1', '').strip()
        dns2 = request.form.get('dns2', '').strip()
        dns_list = [ip for ip in (dns1, dns2) if ip]
        dns = ' '.join(dns_list)
        if not network_str:
            flash('Network is required for LAN', 'warning')
            return redirect(url_for('lan.index'))
        try:
            network = ipaddress.IPv4Network(f"{network_str}/24", strict=True)
        except Exception:
            flash('Invalid LAN network; must be a valid /24 network address ending in .0', 'warning')
            return redirect(url_for('lan.index'))
        # Only allow RFC1918 private subnets
        if not network.is_private:
            flash('Subnet must be within RFC1918 private ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)', 'warning')
            return redirect(url_for('lan.index'))
        address = str(network.network_address + 1)
        netmask = '255.255.255.0'
        gateway = address
        broadcast = str(network.broadcast_address)
        # Validate optional DNS server addresses
        for ip in dns_list:
            try:
                ipaddress.IPv4Address(ip)
            except Exception:
                flash(f'Invalid LAN DNS address: {ip}', 'warning')
                return redirect(url_for('lan.index'))
        # Write configuration file
        cfg_dir = '/etc/network/interfaces.d'
        os.makedirs(cfg_dir, exist_ok=True)
        cfg_file = os.path.join(cfg_dir, f'{iface}.conf')
        template_name = 'config/interfaces-static.conf.j2'
        context = {
            'iface': iface,
            'address': address,
            'netmask': netmask,
            'gateway': gateway,
            'dns': dns,
            'broadcast': broadcast,
            'wan_iface': wan_iface
        }
        try:
            content = render_template(template_name, **context)
            with open(cfg_file, 'w') as f:
                f.write(content)
            os.makedirs(os.path.dirname(lan_iface_path), exist_ok=True)
            with open(lan_iface_path, 'w') as f:
                f.write(iface + '\n')
            flash(f'LAN interface {iface} configured statically', 'success')
        except Exception as e:
            flash(f'Failed to save LAN config: {e}', 'danger')
        return redirect(url_for('lan.index'))

    # On GET, load existing static config of selected LAN iface
    if current_iface and current_iface in interfaces:
        cfg_file = os.path.join('/etc/network/interfaces.d', f'{current_iface}.conf')
        if os.path.isfile(cfg_file):
            try:
                text = open(cfg_file).read()
                # Parse address and netmask
                m = re.search(r'^\s*address\s+(.+)', text, re.M)
                if m:
                    static_cfg['address'] = m.group(1).strip()
                m = re.search(r'^\s*netmask\s+(.+)', text, re.M)
                if m:
                    static_cfg['netmask'] = m.group(1).strip()
                # Parse DNS nameservers (commented or not)
                m = re.search(r'^\s*#?dns-nameservers\s+(.+)', text, re.M)
                if m:
                    static_cfg['dns-nameservers'] = m.group(1).strip()
            except Exception:
                pass
    # Compute network and prefix for display
    try:
        net = ipaddress.IPv4Network(f"{static_cfg['address']}/{static_cfg['netmask']}", strict=False)
        static_cfg['network'] = str(net.network_address)
        static_cfg['prefix'] = net.prefixlen
        static_cfg['gateway'] = str(net.network_address + 1)
        static_cfg['broadcast'] = str(net.broadcast_address)
    except Exception:
        static_cfg['network'] = ''
        static_cfg['prefix'] = 24

    # Build interface information for display
    iface_stats = psutil.net_if_stats()
    iface_addrs = psutil.net_if_addrs()
    interfaces_info = []
    for iface in interfaces:
        if os.path.isdir(f'/sys/class/net/{iface}/wireless'):
            iface_type = 'WiFi'
        else:
            iface_type = 'Ethernet'
        is_up = iface_stats.get(iface).isup if iface in iface_stats else False
        status = 'UP' if is_up else 'no_carrier'
        ip_addr = 'none'
        for addr in iface_addrs.get(iface, []):
            if addr.family == socket.AF_INET:
                ip_addr = addr.address
                break
        cfg_file = os.path.join('/etc/network/interfaces.d', f'{iface}.conf')
        mode_val = 'unmanaged'
        if os.path.isfile(cfg_file):
            try:
                text = open(cfg_file).read()
                if re.search(rf'^iface {re.escape(iface)} inet static', text, re.M):
                    mode_val = 'static'
                elif re.search(rf'^iface {re.escape(iface)} inet dhcp', text, re.M):
                    mode_val = 'dynamic'
            except Exception:
                pass
        interfaces_info.append({
            'name': iface,
            'type': iface_type,
            'status': status,
            'ip': ip_addr,
            'mode': mode_val
        })
    # Split existing DNS nameservers for template
    dns_list = static_cfg.get('dns-nameservers', '').split()
    dns1 = dns_list[0] if len(dns_list) > 0 else ''
    dns2 = dns_list[1] if len(dns_list) > 1 else ''
    # Wi-Fi configuration data (only wlanX)
    net_dir = '/sys/class/net'
    wifi_interfaces = []
    hostapd_dir = '/etc/hostapd'
    for iface in sorted(os.listdir(net_dir)):
        # only wlanX interfaces
        if not re.match(r'^wlan\d+$', iface):
            continue
        if os.path.isdir(os.path.join(net_dir, iface, 'wireless')):
            cfg_file = os.path.join(hostapd_dir, f"{iface}.conf")
            ssid = hw_mode = wpa_passphrase = ''
            if os.path.isfile(cfg_file):
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
                except Exception:
                    pass
            try:
                result = subprocess.run(['iwconfig', iface], capture_output=True, text=True, timeout=2)
                stats = result.stdout or result.stderr
            except Exception:
                stats = 'Unable to retrieve interface statistics'
            try:
                en = subprocess.run(['systemctl', 'is-enabled', f'hostapd@{iface}'], capture_output=True, text=True, timeout=2)
                service_enabled = en.stdout.strip() == 'enabled'
            except Exception:
                service_enabled = False
            try:
                act = subprocess.run(['systemctl', 'is-active', f'hostapd@{iface}'], capture_output=True, text=True, timeout=2)
                service_active = act.stdout.strip() == 'active'
            except Exception:
                service_active = False
            wifi_interfaces.append({
                'name': iface,
                'ssid': ssid,
                'hw_mode': hw_mode,
                'wpa_passphrase': wpa_passphrase,
                'stats': stats,
                'service_enabled': service_enabled,
                'service_active': service_active
            })
    HW_MODES = ['g', 'a']
    hw_mode_labels = {
        'g': '2.4 GHz (802.11g: 6/12/24/54 Mbps)',
        'a': '5 GHz (802.11a/n/ac: up to 866 Mbps)'
    }
    return render_template(
        'lan/index.html',
        interfaces=interfaces,
        current_iface=current_iface,
        static_cfg=static_cfg,
        dns1=dns1,
        dns2=dns2,
        interfaces_info=interfaces_info,
        wan_iface=wan_iface,
        wifi_interfaces=wifi_interfaces,
        hw_modes=HW_MODES,
        hw_mode_labels=hw_mode_labels
    )
 
@lan_bp.route('/wifi/<iface>', methods=['POST'])
@login_required
def wifi_save(iface):
    # Validate wireless interface
    net_dir = '/sys/class/net'
    if not os.path.isdir(os.path.join(net_dir, iface, 'wireless')):
        abort(404)
    action = request.form.get('action')
    service = f'hostapd@{iface}'
    if action in ['enable', 'disable', 'restart']:
        try:
            if action == 'enable':
                cmd = ['systemctl', 'enable', '--now', service]
                msg = 'enabled and started'
            elif action == 'disable':
                cmd = ['systemctl', 'disable', '--now', service]
                msg = 'disabled and stopped'
            else:
                cmd = ['systemctl', 'restart', service]
                msg = 'restarted'
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                flash(f'Service {service} {msg} successfully', 'success')
            else:
                flash(f'Failed to {action} service {service}: {result.stderr or result.stdout}', 'danger')
        except Exception as e:
            flash(f'Error managing service {service}: {e}', 'danger')
        return redirect(url_for('lan.index'))
    # Configuration save
    ssid = request.form.get('ssid', '').strip()
    hw_mode = request.form.get('hw_mode', '').strip()
    wpa_passphrase = request.form.get('wpa_passphrase', '').strip()
    if not ssid:
        flash(f'SSID must not be empty for {iface}', 'warning')
        return redirect(url_for('lan.index'))
    if hw_mode not in ['g', 'a']:
        flash(f'Invalid HW mode for {iface}', 'warning')
        return redirect(url_for('lan.index'))
    if len(wpa_passphrase) < 8:
        flash(f'Passphrase must be at least 8 characters for {iface}', 'warning')
        return redirect(url_for('lan.index'))
    cfg_dir = '/etc/hostapd'
    os.makedirs(cfg_dir, exist_ok=True)
    cfg_file = os.path.join(cfg_dir, f"{iface}.conf")
    try:
        # Choose template
        tmpl = 'config/hostapd-2g.conf.j2' if hw_mode == 'g' else 'config/hostapd-5g.conf.j2'
        content = render_template(tmpl, iface=iface, ssid=ssid, hw_mode=hw_mode, wpa_passphrase=wpa_passphrase)
        with open(cfg_file, 'w') as f:
            f.write(content)
        flash(f'Configuration for {iface} saved', 'success')
    except Exception as e:
        flash(f'Failed to save config for {iface}: {e}', 'danger')
    return redirect(url_for('lan.index'))
