import os
import re
import ipaddress
import subprocess
from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, current_app
from flask_login import login_required

lan_bp = Blueprint('lan', __name__, url_prefix='/lan')

# Default static configuration for LAN (used as initial values)
DEFAULT_LAN_STATIC_CFG = {
    'address': '192.168.42.1',
    'netmask': '255.255.255.0',
    'gateway': '192.168.42.1',
    'dns-nameservers': '208.67.220.220 8.8.8.8 1.1.1.1',
    'broadcast': '192.168.42.255'
}

@lan_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    # Discover interfaces, excluding loopback and current WAN interface
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
            if iface == 'lo' or iface == wan_iface:
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
        # Read form fields: network (fixed /24) and optional DNS
        network_str = request.form.get('network', '').strip()
        dns = request.form.get('dns', '').strip()
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
        # Validate optional DNS
        if dns:
            for ip in dns.split():
                try:
                    ipaddress.IPv4Address(ip)
                except Exception:
                    flash('Invalid LAN DNS address', 'warning')
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

    # Retrieve interface statistics
    stats = ''
    if current_iface:
        try:
            result = subprocess.run(
                ['ip', '-s', 'address', 'show', 'dev', current_iface],
                capture_output=True, text=True, timeout=2
            )
            stats = result.stdout or result.stderr
        except Exception:
            stats = 'Unable to retrieve interface statistics'
    return render_template(
        'lan/index.html',
        interfaces=interfaces,
        current_iface=current_iface,
        static_cfg=static_cfg,
        stats=stats
    )
