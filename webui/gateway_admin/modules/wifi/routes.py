import os
from flask import Blueprint, render_template, request, flash, redirect, url_for, abort
import subprocess
from flask_login import login_required

# Allowed hostapd hardware modes on Raspberry Pi (2.4 GHz g, 5 GHz a)
HW_MODES = ['g', 'a']

wifi_bp = Blueprint('wifi', __name__, url_prefix='/wifi')

@wifi_bp.route('/')
@login_required
def index():
    # Discover wireless interfaces via sysfs
    net_dir = '/sys/class/net'
    interfaces = []
    if os.path.isdir(net_dir):
        for iface in sorted(os.listdir(net_dir)):
            path = os.path.join(net_dir, iface)
            # wifi-capable if 'wireless' subdir exists
            if os.path.isdir(os.path.join(path, 'wireless')):
                # Load existing hostapd config if present
                cfg_dir = '/etc/hostapd'
                cfg_file = os.path.join(cfg_dir, f"{iface}.conf")
                ssid = ''
                hw_mode = ''
                wpa_passphrase = ''
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
                # Gather interface statistics and hostapd service status
                stats = ''
                service_enabled = False
                service_active = False
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
                interfaces.append({
                    'name': iface,
                    'ssid': ssid,
                    'hw_mode': hw_mode,
                    'wpa_passphrase': wpa_passphrase,
                    'stats': stats,
                    'service_enabled': service_enabled,
                    'service_active': service_active
                })
    # Friendly labels for hardware modes with typical speeds
    hw_mode_labels = {
        'g': '2.4 GHz (802.11g: 6/12/24/54 Mbps)',
        'a': '5 GHz   (802.11a: 6/12/24/54 Mbps)'
    }
    return render_template(
        'wifi/index.html',
        interfaces=interfaces,
        hw_modes=HW_MODES,
        hw_mode_labels=hw_mode_labels
    )

@wifi_bp.route('/<iface>', methods=['POST'])
@login_required
def save(iface):
    # Validate interface
    net_dir = '/sys/class/net'
    if not os.path.isdir(os.path.join(net_dir, iface, 'wireless')):
        abort(404)
    # Determine requested action: service management or configuration save
    action = request.form.get('action')
    if action in ['enable', 'disable', 'restart']:
        service = f'hostapd@{iface}'
        try:
            if action == 'enable':
                cmd = ['systemctl', 'enable', '--now', service]
                action_str = 'enabled and started'
            elif action == 'disable':
                cmd = ['systemctl', 'disable', '--now', service]
                action_str = 'disabled and stopped'
            else:  # restart
                cmd = ['systemctl', 'restart', service]
                action_str = 'restarted'
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                flash(f'Service {service} {action_str} successfully', 'success')
            else:
                flash(f'Failed to {action} service {service}: {result.stderr or result.stdout}', 'danger')
        except Exception as e:
            flash(f'Error managing service {service}: {e}', 'danger')
        return redirect(url_for('wifi.index'))
    # Get form values
    ssid = request.form.get('ssid', '').strip()
    hw_mode = request.form.get('hw_mode', '').strip()
    wpa_passphrase = request.form.get('wpa_passphrase', '').strip()
    # Validate SSID
    if not ssid:
        flash(f'SSID must not be empty for {iface}', 'warning')
        return redirect(url_for('wifi.index'))
    # Validate hardware mode
    if hw_mode not in HW_MODES:
        flash(f'Invalid HW mode for {iface}', 'warning')
        return redirect(url_for('wifi.index'))
    # Validate passphrase length
    if len(wpa_passphrase) < 8:
        flash(f'Passphrase must be at least 8 characters for {iface}', 'warning')
        return redirect(url_for('wifi.index'))
    # Build hostapd config
    cfg_dir = '/etc/hostapd'
    try:
        os.makedirs(cfg_dir, exist_ok=True)
    except Exception:
        pass
    cfg_file = os.path.join(cfg_dir, f"{iface}.conf")
    # Render hostapd config via Jinja template
    try:
        content = render_template(
            'config/hostapd.conf.j2',
            iface=iface,
            ssid=ssid,
            hw_mode=hw_mode,
            wpa_passphrase=wpa_passphrase
        )
        with open(cfg_file, 'w') as f:
            f.write(content)
        flash(f'Configuration for {iface} saved', 'success')
    except Exception as e:
        flash(f'Failed to save config for {iface}: {e}', 'danger')
    return redirect(url_for('wifi.index'))
