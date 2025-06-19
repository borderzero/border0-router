import os
import json
import base64
import re
import subprocess
import time
import urllib.request
import psutil
import socket
from flask import Blueprint, render_template, current_app, flash, redirect, url_for, request
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

        # DHCP lease cache with TTL and manual refresh
        dhcp_cache_file = os.path.join(cache_dir, f'dhcp_clients_{lan_iface}.json')
        refresh = request.args.get('refresh_clients')
        # stale if missing or older than 60 minutes, or on explicit refresh
        stale = False
        try:
            if time.time() - os.path.getmtime(dhcp_cache_file) > 3600:
                stale = True
        except Exception:
            stale = True
        if refresh:
            stale = True

        leases = {}
        if not stale:
            try:
                with open(dhcp_cache_file) as f:
                    leases = {c['mac']: c for c in json.load(f)}
            except Exception:
                leases = {}
        if stale:
            log_file = f'/var/log/dnsmasq_{lan_iface}.log'
            try:
                output = subprocess.check_output(['tail', '-n', '10000', log_file], text=True)
                for line in reversed(output.splitlines()):
                    m = re.search(r'DHCPACK\([^)]*\)\s+(\d+\.\d+\.\d+\.\d+)\s+([0-9A-Fa-f:]+)\s+(\S+)', line)
                    if m:
                        ip, mac, hostname = m.group(1), m.group(2).lower(), m.group(3)
                        if mac not in leases:
                            leases[mac] = {'hostname': hostname, 'ip': ip, 'mac': mac}
                try:
                    import manuf
                    parser = manuf.MacParser()
                    for v in leases.values():
                        v['manufacturer'] = parser.get_manuf(v['mac']) or ''
                except Exception:
                    for v in leases.values():
                        v['manufacturer'] = ''
                tmp = dhcp_cache_file + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(list(leases.values()), f)
                os.replace(tmp, dhcp_cache_file)
            except Exception:
                pass

        try:
            # fetch neighbor table JSON; ignore non-zero exit codes so we still parse entries even if some are FAILED
            proc = subprocess.run(
                ['ip', '-j', 'neigh', 'show', 'dev', lan_iface],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True
            )
            neigh_entries = json.loads(proc.stdout or '[]')
            for entry in neigh_entries:
                # if JSON includes device, skip other interfaces; otherwise assume filtered by 'show dev'
                dev = entry.get('dev')
                if dev and dev != lan_iface:
                    continue
                dst = entry.get('dst')
                # only IPv4
                if not dst or ':' in dst:
                    continue
                mac = entry.get('lladdr', '').lower()
                if not mac:
                    continue
                state = entry.get('state')
                if isinstance(state, list) and state:
                    state = state[0]
                # skip unreachable entries
                if state == 'FAILED':
                    continue
                existing = leases.get(mac)
                if existing:
                    # update only when IP matches or to track IP changes
                    if existing.get('ip') == dst:
                        existing['state'] = state
                    else:
                        existing['state'] = state
                        existing['ip'] = dst
                else:
                    leases[mac] = {
                        'hostname': None,
                        'ip': dst,
                        'mac': mac,
                        'manufacturer': '',
                        'state': state
                    }
        except Exception:
            try:
                with open('/proc/net/arp') as arp_f:
                    lines = arp_f.readlines()[1:]
                for line in lines:
                    parts = line.split()
                    if parts[5] == lan_iface:
                        ip_addr, mac_addr = parts[0], parts[3].lower()
                        if mac_addr not in leases:
                            leases[mac_addr] = {
                                'hostname': None,
                                'ip': ip_addr,
                                'mac': mac_addr,
                                'manufacturer': ''
                            }
            except Exception:
                pass

        lan_clients = list(leases.values())

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
        wan_info=wan_info,
        wan_traffic=wan_traffic,
        lan_info=lan_info,
        lan_traffic=lan_traffic,
        lan_clients=lan_clients,
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
