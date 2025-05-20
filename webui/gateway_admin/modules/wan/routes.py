import os
import re
import ipaddress
import subprocess
from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, current_app
from flask_login import login_required
import socket
import psutil
# Default static configuration defaults
DEFAULT_STATIC_CFG = {
    'address': '192.168.123.1',
    'netmask': '255.255.255.0',
    'gateway': '192.168.123.1',
    'dns-nameservers': '208.67.220.220 8.8.8.8 1.1.1.1',
    'broadcast': '192.168.123.255'
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
        action = request.form.get('action')
        iface = request.form.get('iface')
        if action == 'restart':
            if iface not in interfaces:
                flash('Invalid interface selected', 'warning')
                return redirect(url_for('wan.index'))
            try:
                subprocess.run(['ifdown', iface], capture_output=True, text=True, timeout=10)
                subprocess.run(['ifup', iface], capture_output=True, text=True, timeout=10)
                flash(f'WAN interface {iface} restarted', 'success')
            except Exception as e:
                flash(f'Failed to restart WAN interface: {e}', 'danger')
            return redirect(url_for('wan.index'))
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
        # Render network config via Jinja template
        template_name = 'config/interfaces-dhcp.conf.j2' if m == 'dhcp' else 'config/interfaces-static.conf.j2'
        # Prepare context
        context = {'iface': iface}
        if m == 'static':
            address = request.form.get('address', '').strip()
            netmask = request.form.get('netmask', '').strip()
            gateway = request.form.get('gateway', '').strip()
            dns = request.form.get('dns', '').strip()
            broadcast = request.form.get('broadcast', '').strip()
            context.update({
                'address': address,
                'netmask': netmask,
                'gateway': gateway,
                'dns': dns,
                'broadcast': broadcast
            })
        try:
            content = render_template(template_name, **context)
            with open(cfg_file, 'w') as f:
                f.write(content)
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

    # Load current LAN interface selection for cross-page indicator
    lan_iface = None
    lan_iface_path = current_app.config.get('LAN_IFACE_PATH')
    try:
        with open(lan_iface_path) as f:
            lan_iface = f.read().strip()
    except Exception:
        lan_iface = None
    # Build interface information for display
    iface_stats = psutil.net_if_stats()
    iface_addrs = psutil.net_if_addrs()
    interfaces_info = []
    for iface in interfaces:
        # Determine type: WiFi if wireless, else Ethernet
        if os.path.isdir(f'/sys/class/net/{iface}/wireless'):
            iface_type = 'WiFi'
        else:
            iface_type = 'Ethernet'
        # Determine status: UP if interface is up, else no_carrier
        is_up = iface_stats.get(iface).isup if iface in iface_stats else False
        status = 'UP' if is_up else 'no_carrier'
        # Get IPv4 address if available
        ip_addr = 'none'
        for addr in iface_addrs.get(iface, []):
            if addr.family == socket.AF_INET:
                ip_addr = addr.address
                break
        # Determine mode: static, dynamic, or unmanaged based on config file
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
    return render_template(
        'wan/index.html',
        interfaces=interfaces,
        current_iface=current_iface,
        mode=mode,
        static_cfg=static_cfg,
        interfaces_info=interfaces_info,
        lan_iface=lan_iface
    )
