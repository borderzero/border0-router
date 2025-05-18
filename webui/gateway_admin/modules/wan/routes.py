import os
import re
import ipaddress
import subprocess
from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, current_app
from flask_login import login_required
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
        'wan/index.html',
        interfaces=interfaces,
        current_iface=current_iface,
        mode=mode,
        static_cfg=static_cfg,
        stats=stats
    )
