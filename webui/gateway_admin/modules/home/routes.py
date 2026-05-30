import os
import json
import base64
import re
import subprocess
import urllib.request
import psutil
from flask import Blueprint, render_template, current_app, flash, redirect, url_for, request
from flask_login import login_required

from ... import netconfig, netutils

home_bp = Blueprint('home', __name__, url_prefix='')

@home_bp.route('/')
@login_required
def index():
    cache_dir = '/etc/border0'
    cache_file = os.path.join(cache_dir, 'version_cache.json')
    data = {}
    try:
        with open(cache_file) as f:
            data = json.load(f)
    except Exception:
        pass
    current_version = data.get('current_version', 'unknown')
    update_available = data.get('update_available', False)
    new_version = data.get('new_version')
    # --- Dashboard data collection ---
    # Border0: organization (prefer env override, else read org_subdomain from file)
    org = current_app.config.get('BORDER0_ORG')
    if not org:
        org = None
        org_path = current_app.config.get('BORDER0_ORG_PATH')
        try:
            raw = open(org_path).read().strip()
            try:
                data = json.loads(raw)
                org = data.get('org_subdomain', '')
            except ValueError:
                org = raw
        except Exception:
            org = None
    # Border0: exit node
    exit_node = None
    exit_node_error = None
    cli = current_app.config.get('BORDER0_CLI_PATH', 'border0')
    try:
        proc = subprocess.run(
            [cli, 'node', 'state', 'show', '--json'],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            state = json.loads(proc.stdout)
            exit_node = state.get('exit_node') or None
        else:
            exit_node_error = proc.stderr or proc.stdout
    except Exception as e:
        exit_node_error = str(e)
    # Border0: service status
    try:
        svc = subprocess.run(
            ['systemctl', 'status', 'border0-device.service', '-n0'],
            capture_output=True, text=True, timeout=3
        )
        device_status = svc.stdout or svc.stderr
        service_active = 'Active: active' in device_status
    except Exception as e:
        device_status = f'Error obtaining service status: {e}'
        service_active = False
    try:
        ena = subprocess.run(
            ['systemctl', 'is-enabled', 'border0-device.service'],
            capture_output=True, text=True, timeout=3
        )
        service_enabled = ena.stdout.strip() == 'enabled'
    except Exception:
        service_enabled = False

    # System metrics
    def human(n):
        for unit in ['B','KB','MB','GB','TB']:
            if n < 1024:
                return f"{n:.1f}{unit}"
            n /= 1024
        return f"{n:.1f}PB"
    cpu = psutil.cpu_percent(interval=0.1)
    vm = psutil.virtual_memory()
    du = psutil.disk_usage('/')
    nc = psutil.net_io_counters()
    system_info = {
        'cpu_percent': cpu,
        'mem_total': human(vm.total),
        'mem_used': human(vm.used),
        'mem_percent': vm.percent,
        'disk_total': human(du.total),
        'disk_used': human(du.used),
        'disk_percent': du.percent,
        'net_sent': human(nc.bytes_sent),
        'net_recv': human(nc.bytes_recv)
    }

    # --- Network: bridge/zone-aware view off the frozen model ---
    # network.json is the single source of truth now; no more per-iface flag files.
    model = netconfig.load()
    wifi = model.get('wifi') or {}

    # WAN: every uplink + its live link status; flag the active one.
    wan = model.get('wan') or {}
    wan_active = wan.get('active')
    wan_ifaces = []
    active_no_carrier = False
    for w in (wan.get('interfaces') or []):
        iface = w.get('iface')
        if not iface:
            continue
        st = netutils.iface_status(iface)
        is_active = (iface == wan_active)
        wan_ifaces.append({
            'name': iface,
            'type': netutils.iface_type(iface),
            'active': is_active,
            'status': st['status'],
            'ipv4': st['ipv4'],
            'ipv6': st['ipv6'],
        })
        # active uplink with no link = no internet; worth shouting about.
        if is_active and st['status'] != 'up':
            active_no_carrier = True

    # LAN: one entry per bridge, with its eth members, attached AP SSIDs,
    # status, and DHCP clients seen on that bridge.
    refresh = request.args.get('refresh_clients')
    bridges = []
    for lan in (model.get('lans') or []):
        name = lan.get('name')
        members = lan.get('members') or {}
        ap_ssids = []
        for w in (members.get('wifi_ap') or []):
            cfg = wifi.get(w) or {}
            if cfg.get('mode') == 'ap':
                ap_ssids.append({'iface': w, 'ssid': cfg.get('ssid') or w})
        st = netutils.iface_status(name)
        # only rebuild the lease cache for the bridge the user hit Refresh on
        bridges.append({
            'name': name,
            'subnet': lan.get('subnet'),
            'gateway': lan.get('gateway'),
            'status': st['status'],
            'ipv4': st['ipv4'],
            'eth_members': list(members.get('eth') or []),
            'ap_ssids': ap_ssids,
            'clients': netutils.dhcp_clients(name, refresh=(refresh == name)),
        })

    # WiFi radios: one row per physical radio + what the model says it's doing.
    wifi_radios = []
    # reverse map: which bridge each AP wlan lives in
    ap_bridge = {}
    for lan in (model.get('lans') or []):
        for w in ((lan.get('members') or {}).get('wifi_ap') or []):
            ap_bridge[w] = lan.get('name')
    for radio in netutils.list_wifi_radios():
        cfg = wifi.get(radio) or {}
        mode = cfg.get('mode')
        if mode == 'ap':
            role = 'AP'
            detail = '%s → %s' % (cfg.get('ssid') or '?', ap_bridge.get(radio) or '?')
        elif mode == 'client':
            role = 'Client'
            detail = (cfg.get('client') or {}).get('ssid') or '?'
        else:
            role = 'Off'
            detail = ''
        wifi_radios.append({'name': radio, 'role': role, 'detail': detail})

    user_info = None
    token_file = current_app.config.get('BORDER0_TOKEN_PATH')
    meta_file = current_app.config.get('BORDER0_TOKEN_METADATA_PATH')
    # Use cached metadata if available, else decode token and update cache once
    if meta_file and os.path.isfile(meta_file):
        try:
            user_info = json.loads(open(meta_file).read())
        except Exception:
            user_info = None
    elif token_file and os.path.isfile(token_file):
        try:
            token_str = open(token_file).read().strip()
            parts = token_str.split('.')
            if len(parts) >= 2:
                padding = '=' * (-len(parts[1]) % 4)
                payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
                user_info = payload
                try:
                    os.makedirs(os.path.dirname(meta_file), exist_ok=True)
                    with open(meta_file, 'w') as mf:
                        json.dump(payload, mf)
                except Exception:
                    pass
        except Exception:
            user_info = None

    return render_template(
        'home/index.html',
        current_version=current_version,
        update_available=update_available,
        new_version=new_version,
        org=org,
        exit_node=exit_node,
        exit_node_error=exit_node_error,
        service_active=service_active,
        service_enabled=service_enabled,
        device_status=device_status,
        system_info=system_info,
        wan_ifaces=wan_ifaces,
        active_no_carrier=active_no_carrier,
        bridges=bridges,
        wifi_radios=wifi_radios,
        user_info=user_info
    )
def check_update():
    cli = current_app.config.get('BORDER0_CLI_PATH', 'border0')
    cache_dir = '/etc/border0'
    cache_file = os.path.join(cache_dir, 'version_cache.json')
    os.makedirs(cache_dir, exist_ok=True)
    try:
        out = subprocess.check_output([cli, '--version'], stderr=subprocess.STDOUT, text=True, timeout=30)
        m = re.search(r'version:\s*(v\S+)', out)
        current_version = m.group(1) if m else 'unknown'
    except Exception as e:
        flash(f'Failed to get current Border0 version: {e}', 'warning')
        return redirect(url_for('home.index'))
    latest = None
    try:
        resp = urllib.request.urlopen('https://download.border0.com/latest_version.txt', timeout=10)
        latest = resp.read().decode().strip()
    except Exception as e:
        flash(f'Failed to fetch latest version info: {e}', 'warning')
    update_available = bool(latest and latest != current_version)
    cache = {
        'current_version': current_version,
        'update_available': update_available,
        'new_version': latest
    }
    try:
        tmp = cache_file + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cache, f)
        os.replace(tmp, cache_file)
    except Exception as e:
        flash(f'Failed to write version cache: {e}', 'warning')
    if update_available:
        flash(f'New version available: {latest}', 'success')
    else:
        flash('No new version available.', 'info')
    return redirect(url_for('home.index'))
# Remove duplicate json import; keep Response import
from flask import Response

# Page showing progress bar for upgrade
@home_bp.route('/upgrade')
@login_required
def upgrade_page():
    return render_template('home/upgrade.html')

# Server-Sent Events endpoint streaming upgrade progress
@home_bp.route('/upgrade/stream')
@login_required
def upgrade_stream():
    cli = current_app.config.get('BORDER0_CLI_PATH', 'border0')
    def generate():
        # Launch upgrade process
        proc = subprocess.Popen(
            [cli, 'version', 'upgrade'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        # Regex to capture percentage from lines like "[   ...] 12.3%"
        pattern = re.compile(r'\[.*?\]\s*(\d+(?:\.\d+)?)%')
        # Notify start
        yield 'event: start\ndata: {}\n\n'
        # Stream progress events
        for line in proc.stdout:
            m = pattern.search(line)
            if m:
                percent = float(m.group(1))
                yield f"event: progress\ndata: {json.dumps({'percent': percent})}\n\n"
        proc.wait()
        # Final event
        status = 'success' if proc.returncode == 0 else 'error'
        # On successful upgrade, refresh version cache
        if status == 'success':
            cache_dir = '/etc/border0'
            cache_file = os.path.join(cache_dir, 'version_cache.json')
            try:
                os.makedirs(cache_dir, exist_ok=True)
                out = subprocess.check_output([cli, '--version'], stderr=subprocess.STDOUT, text=True, timeout=30)
                m2 = re.search(r'version:\s*(v\S+)', out)
                curr_version = m2.group(1) if m2 else None
                cache = {
                    'current_version': curr_version or '',
                    'update_available': False,
                    'new_version': curr_version or ''
                }
                tmp = cache_file + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(cache, f)
                os.replace(tmp, cache_file)
            except Exception:
                pass
        yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
    return Response(generate(), mimetype='text/event-stream')
