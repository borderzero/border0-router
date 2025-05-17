import os
import re
import ipaddress
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
        iface = request.form.get('iface')
        if iface not in interfaces:
            flash('Invalid interface selected for LAN', 'warning')
            return redirect(url_for('lan.index'))
        # Read static form fields
        address = request.form.get('address', '').strip()
        netmask = request.form.get('netmask', '').strip()
        gateway = request.form.get('gateway', '').strip()
        dns = request.form.get('dns', '').strip()
        broadcast = request.form.get('broadcast', '').strip()
        # Validate required fields
        if not address or not netmask:
            flash('Address and netmask are required for LAN', 'warning')
            return redirect(url_for('lan.index'))
        try:
            ipaddress.IPv4Address(address)
            ipaddress.IPv4Address(netmask)
        except Exception:
            flash('Invalid LAN IP address or netmask', 'warning')
            return redirect(url_for('lan.index'))
        # Optional fields validation
        if gateway:
            try:
                ipaddress.IPv4Address(gateway)
            except Exception:
                flash('Invalid LAN gateway address', 'warning')
                return redirect(url_for('lan.index'))
        if dns:
            for ip in dns.split():
                try:
                    ipaddress.IPv4Address(ip)
                except Exception:
                    flash('Invalid LAN DNS address', 'warning')
                    return redirect(url_for('lan.index'))
        if broadcast:
            try:
                ipaddress.IPv4Address(broadcast)
            except Exception:
                flash('Invalid LAN broadcast address', 'warning')
                return redirect(url_for('lan.index'))
        # Build config file
        # Render static LAN config via Jinja template
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
            'broadcast': broadcast
        }
        try:
            content = render_template(template_name, **context)
            with open(cfg_file, 'w') as f:
                f.write(content)
            # Save selected LAN interface
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
                # Parse static only
                for key in ['address', 'netmask', 'gateway', 'dns-nameservers', 'broadcast']:
                    m = re.search(rf'^{key} (.+)', text, re.M)
                    if m:
                        static_cfg[key] = m.group(1).strip()
            except Exception:
                pass

    return render_template('lan/index.html',
                           interfaces=interfaces,
                           current_iface=current_iface,
                           static_cfg=static_cfg)
