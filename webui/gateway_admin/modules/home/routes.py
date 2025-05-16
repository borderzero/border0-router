import subprocess
import re
from flask import Blueprint, render_template, current_app, flash, redirect, url_for
from flask_login import login_required

home_bp = Blueprint('home', __name__, url_prefix='')

@home_bp.route('/')
@login_required
def index():
    cli = current_app.config.get('BORDER0_CLI_PATH', 'border0')
    # Get current version
    try:
        out = subprocess.check_output([cli, '--version'], stderr=subprocess.STDOUT, text=True)
        m = re.search(r'version:\s*(v\S+)', out)
        current_version = m.group(1) if m else 'unknown'
    except Exception as e:
        current_version = 'unknown'
        flash(f'Failed to get current Border0 version: {e}', 'warning')
    # Check for new version
    update_available = False
    new_version = None
    try:
        check_out = subprocess.check_output([cli, 'version', 'check'], stderr=subprocess.STDOUT, text=True)
        m2 = re.search(r'There is a newer version available.*\((v[^)]+)\)', check_out)
        if m2:
            update_available = True
            new_version = m2.group(1)
    except Exception:
        pass
    return render_template('home/index.html',
                           current_version=current_version,
                           update_available=update_available,
                           new_version=new_version)

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
