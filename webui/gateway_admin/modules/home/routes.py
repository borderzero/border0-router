import os
import json
import re
import subprocess
import urllib.request
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
    return render_template(
        'home/index.html',
        current_version=current_version,
        update_available=update_available,
        new_version=new_version
    )
@home_bp.route('/check_update', methods=['POST'])
@login_required
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

import json
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
        yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
    return Response(generate(), mimetype='text/event-stream')
