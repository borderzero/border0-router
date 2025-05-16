import os
from flask import Blueprint, render_template, request, flash, redirect, url_for, abort
from flask_login import login_required

# Allowed hostapd hardware modes
HW_MODES = ['a', 'b', 'g', 'ad']

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
                interfaces.append({
                    'name': iface,
                    'ssid': ssid,
                    'hw_mode': hw_mode,
                    'wpa_passphrase': wpa_passphrase
                })
    return render_template('wifi/index.html', interfaces=interfaces, hw_modes=HW_MODES)

@wifi_bp.route('/<iface>', methods=['POST'])
@login_required
def save(iface):
    # Validate interface
    net_dir = '/sys/class/net'
    if not os.path.isdir(os.path.join(net_dir, iface, 'wireless')):
        abort(404)
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
    lines = [
        f"interface={iface}",
        "driver=nl80211",
        "",
        f"ssid={ssid}",
        f"hw_mode={hw_mode}",
        "channel=6",
        "country_code=US",
        "",
        "wpa=2",
        "wpa_key_mgmt=WPA-PSK",
        f"wpa_passphrase={wpa_passphrase}",
        "rsn_pairwise=CCMP",
        ""
    ]
    try:
        with open(cfg_file, 'w') as f:
            f.write("\n".join(lines))
        flash(f'Configuration for {iface} saved', 'success')
    except Exception as e:
        flash(f'Failed to save config for {iface}: {e}', 'danger')
    return redirect(url_for('wifi.index'))
