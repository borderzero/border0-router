import os
import re
import ipaddress
from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, current_app
from flask_login import login_required
# Default static configuration defaults
DEFAULT_STATIC_CFG = {
    'address': '192.168.6.1',
    'netmask': '255.255.255.0',
    'gateway': '192.168.6.1',
    'dns-nameservers': '208.67.220.220',
    'broadcast': '192.168.6.255'
}
wan_bp = Blueprint('wan', __name__, url_prefix='/wan')

@wan_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    # Discover interfaces (excluding loopback)
    net_dir = '/sys/class/net'
    interfaces = []
    if os.path.isdir(net_dir):
        for iface in sorted(os.listdir(net_dir)):
            if iface == 'lo':
                continue
            interfaces.append(iface)

    # Load current WAN interface selection
    current_iface = None
    mode = 'dhcp'
    # Prepare static config with sensible defaults
    static_cfg = DEFAULT_STATIC_CFG.copy()
    wan_iface_path = current_app.config.get('WAN_IFACE_PATH')
    try:
        with open(wan_iface_path) as f:
            current_iface = f.read().strip()
    except Exception:
        current_iface = None

    if request.method == 'POST':
        iface = request.form.get('iface')
        if iface not in interfaces:
            flash('Invalid interface selected', 'warning')
            return redirect(url_for('wan.index'))
        m = request.form.get('mode')
        if m not in ['dhcp', 'static']:
            flash('Invalid mode selected', 'warning')
            return redirect(url_for('wan.index'))

        # Build /etc/network/interfaces.d/<iface>.conf
        cfg_dir = '/etc/network/interfaces.d'
        try:
            os.makedirs(cfg_dir, exist_ok=True)
        except Exception:
            pass
        cfg_file = os.path.join(cfg_dir, f'{iface}.conf')
        lines = [
            f'allow-hotplug {iface}',
            f'auto {iface}',
            f'iface {iface} inet {m}'
        ]
        if m == 'static':
            address = request.form.get('address', '').strip()
            netmask = request.form.get('netmask', '').strip()
            gateway = request.form.get('gateway', '').strip()
            dns = request.form.get('dns', '').strip()
            broadcast = request.form.get('broadcast', '').strip()
            # Validate addresses
            try:
                ipaddress.IPv4Address(address)
                ipaddress.IPv4Address(netmask)
            except Exception:
                flash('Invalid static IP address or netmask', 'warning')
                return redirect(url_for('wan.index'))
            if gateway:
                try:
                    ipaddress.IPv4Address(gateway)
                except Exception:
                    flash('Invalid gateway address', 'warning')
                    return redirect(url_for('wan.index'))
            if dns:
                for ip in dns.split():
                    try:
                        ipaddress.IPv4Address(ip)
                    except Exception:
                        flash('Invalid DNS address', 'warning')
                        return redirect(url_for('wan.index'))
            if broadcast:
                try:
                    ipaddress.IPv4Address(broadcast)
                except Exception:
                    flash('Invalid broadcast address', 'warning')
                    return redirect(url_for('wan.index'))
            lines += [
                f'    address {address}',
                f'    netmask {netmask}',
            ]
            if gateway:
                lines.append(f'    gateway {gateway}')
            if dns:
                lines.append(f'    dns-nameservers {dns}')
            if broadcast:
                lines.append(f'    broadcast {broadcast}')
        try:
            with open(cfg_file, 'w') as f:
                f.write('\n'.join(lines) + '\n')
        except Exception as e:
            flash(f'Failed to write config: {e}', 'danger')
            return redirect(url_for('wan.index'))

        # Persist the selected WAN interface
        try:
            os.makedirs(os.path.dirname(wan_iface_path), exist_ok=True)
            with open(wan_iface_path, 'w') as f:
                f.write(iface + '\n')
        except Exception as e:
            flash(f'Failed to save WAN interface selection: {e}', 'danger')
            return redirect(url_for('wan.index'))

        flash(f'WAN interface {iface} configured ({m})', 'success')
        return redirect(url_for('wan.index'))

    # On GET, if static mode, load existing static fields
    if current_iface and current_iface in interfaces:
        cfg_file = os.path.join('/etc/network/interfaces.d', f'{current_iface}.conf')
        if os.path.isfile(cfg_file):
            try:
                text = open(cfg_file).read()
                if re.search(rf'^iface {current_iface} inet static', text, re.M):
                    mode = 'static'
                    for key in ['address', 'netmask', 'gateway', 'dns-nameservers', 'broadcast']:
                        m2 = re.search(rf'^{key} (.+)', text, re.M)
                        if m2:
                            static_cfg[key] = m2.group(1).strip()
            except Exception:
                pass

    return render_template('wan/index.html',
                           interfaces=interfaces,
                           current_iface=current_iface,
                           mode=mode,
                           static_cfg=static_cfg)
