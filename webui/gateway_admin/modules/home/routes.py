import os
import json
import re
import subprocess
import urllib.request
import psutil
import socket
from flask import Blueprint, render_template, current_app, flash, redirect, url_for
from flask_login import login_required

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
    # Border0: organization
    org = current_app.config.get('BORDER0_ORG')
    if not org:
        org_path = current_app.config.get('BORDER0_ORG_PATH')
        try:
            with open(org_path) as f:
                org = f.read().strip()
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

    # WAN info
    wan_iface = None
    wan_path = current_app.config.get('WAN_IFACE_PATH')
    try:
        with open(wan_path) as f:
            wan_iface = f.read().strip()
    except Exception:
        wan_iface = None
    wan_info = None
    wan_traffic = None
    if wan_iface:
        stats = psutil.net_if_stats().get(wan_iface)
        addrs = psutil.net_if_addrs().get(wan_iface, [])
        status = 'UP' if stats and stats.isup else 'no_carrier'
        ipv4 = next((a.address for a in addrs if a.family == socket.AF_INET), None)
        ipv6 = next((a.address for a in addrs if a.family == socket.AF_INET6), None)
        wan_info = {
            'name': wan_iface,
            'status': status,
            'ipv4': ipv4,
            'ipv6': ipv6
        }
        # Traffic
        nic = psutil.net_io_counters(pernic=True).get(wan_iface)
        if nic:
            wan_traffic = {'sent': human(nic.bytes_sent), 'recv': human(nic.bytes_recv)}

    # LAN info
    lan_iface = None
    lan_path = current_app.config.get('LAN_IFACE_PATH')
    try:
        with open(lan_path) as f:
            lan_iface = f.read().strip()
    except Exception:
        lan_iface = None
    lan_info = None
    lan_traffic = None
    lan_clients = []
    if lan_iface:
        stats = psutil.net_if_stats().get(lan_iface)
        addrs = psutil.net_if_addrs().get(lan_iface, [])
        status = 'UP' if stats and stats.isup else 'no_carrier'
        ipv4 = next((a.address for a in addrs if a.family == socket.AF_INET), None)
        lan_info = {'name': lan_iface, 'status': status, 'ipv4': ipv4}
        # Traffic
        nic = psutil.net_io_counters(pernic=True).get(lan_iface)
        if nic:
            lan_traffic = {'sent': human(nic.bytes_sent), 'recv': human(nic.bytes_recv)}
        # ARP clients
        try:
            with open('/proc/net/arp') as arp:
                lines = arp.readlines()[1:]
            for line in lines:
                parts = line.split()
                if parts[5] == lan_iface:
                    lan_clients.append({'ip': parts[0], 'mac': parts[3]})
        except Exception:
            pass

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
        wan_info=wan_info,
        wan_traffic=wan_traffic,
        lan_info=lan_info,
        lan_traffic=lan_traffic,
        lan_clients=lan_clients
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
