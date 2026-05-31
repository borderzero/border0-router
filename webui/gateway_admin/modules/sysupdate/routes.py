"""Router self-update: check GitHub for a newer release and apply it in place.

The actual work runs detached in its own systemd transient unit (so it survives
border0-webui restarting itself); see gateway_admin.system_update. These routes
just kick it off and surface the status file the worker writes.
"""

import subprocess
import sys

from flask import Blueprint, jsonify, redirect, render_template, request, url_for, flash
from flask_login import login_required

from ... import system_update

sysupdate_bp = Blueprint('sysupdate', __name__, url_prefix='/system/update')

# WorkingDirectory for the worker so `-m gateway_admin.system_update` resolves.
_WEBUI_DIR = system_update.WEBUI_DIR


@sysupdate_bp.route('/check')
@login_required
def check():
    return jsonify(system_update.check())


@sysupdate_bp.route('/status')
@login_required
def status():
    return jsonify(system_update.read_status())


@sysupdate_bp.route('/start', methods=['POST'])
@login_required
def start():
    info = system_update.check()
    tag = request.form.get('tag') or info.get('latest')
    if not tag:
        flash('No release to update to (could not reach GitHub).', 'warning')
        return redirect(url_for('auth.system'))

    busy = system_update.read_status().get('state')
    if busy in ('downloading', 'extracting', 'backup', 'installing', 'deps',
                'reload', 'restarting', 'verifying', 'rollback'):
        return redirect(url_for('sysupdate.progress'))

    # Run detached in its own transient unit. A plain child would share
    # border0-webui's cgroup and get killed when the worker restarts the service.
    cmd = [
        'systemd-run', '--quiet', '--collect',
        '-p', 'WorkingDirectory=%s' % _WEBUI_DIR,
        sys.executable, '-m', 'gateway_admin.system_update', 'run', tag,
    ]
    try:
        subprocess.Popen(cmd)
    except FileNotFoundError:
        # No systemd-run? fall back to a detached session (best effort).
        subprocess.Popen(cmd[cmd.index(sys.executable):], cwd=_WEBUI_DIR,
                         start_new_session=True)
    system_update._set_status('downloading', 5, 'Starting update to %s…' % tag, tag=tag)
    return redirect(url_for('sysupdate.progress'))


@sysupdate_bp.route('/progress')
@login_required
def progress():
    return render_template('sysupdate/progress.html')
