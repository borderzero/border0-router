from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app, jsonify
from flask_login import login_user, logout_user, login_required, current_user, UserMixin
from ...config import Config
import os
import re
import json
import glob
import shutil
import subprocess
import threading
import time
import signal
import base64
import datetime
import urllib.request
from ...extensions import login_manager

auth_bp = Blueprint('auth', __name__)

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home.index'))

    org = None
    token_file = current_app.config.get('BORDER0_TOKEN_PATH')
    login_url = None

    if request.method == 'POST':
        org = request.form.get('org')
        token_exists = os.path.isfile(token_file)
        if not token_exists:
            if not org:
                flash('Please enter an organization name to log into.', 'danger')
            else:
                try:
                    subprocess.run(['pkill', '-f', f"{Config.BORDER0_CLI_PATH} client login"], check=False)
                except Exception:
                    pass

                try:
                    env = os.environ.copy()
                    env.update({
                        'SHELL': '/bin/bash',
                        'LOGNAME': 'root',
                        'HOME': '/root',
                        'USER': 'root',
                    })
                    proc = subprocess.Popen(
                        [Config.BORDER0_CLI_PATH, 'client', 'login', '--org', org],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        preexec_fn=os.setsid,
                        env=env
                    )

                    pattern = re.compile(r'(https?://\S+)')
                    for line in proc.stdout:
                        m = pattern.search(line)
                        if m:
                            login_url = m.group(1)
                            break

                    if login_url:
                        timer = threading.Timer(120, lambda pid=proc.pid: os.killpg(pid, signal.SIGTERM))
                        timer.daemon = True
                        timer.start()

                        def monitor_and_restart(token_path):
                            for _ in range(60):
                                if os.path.isfile(token_path):
                                    subprocess.run(['systemctl', 'restart', 'border0-device'], check=False)
                                    break
                                time.sleep(2)

                        threading.Thread(target=monitor_and_restart, args=(token_file,), daemon=True).start()
                    else:
                        flash('Login URL not found; please try again.', 'danger')
                except Exception as e:
                    flash(f'Error running login command: {e}', 'danger')

        else:
            try:
                token_str = open(token_file).read().strip()
                parts = token_str.split('.')
                if len(parts) >= 2:
                    padding = '=' * (-len(parts[1]) % 4)
                    payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
                    user_id = payload.get('user_email') or payload.get('sub')
                    if user_id:
                        user = User(user_id)
                        login_user(user)
                        return redirect(request.args.get('next') or url_for('home.index'))
            except Exception as e:
                flash(f'Failed to authenticate: {e}', 'danger')

    token_exists = os.path.isfile(token_file)
    user_info = None
    if token_exists:
        try:
            token_str = open(token_file).read().strip()
            parts = token_str.split('.')
            if len(parts) >= 2:
                padding = '=' * (-len(parts[1]) % 4)
                user_info = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        except Exception:
            user_info = None

    # Auto-login and redirect if token exists and user info available
    if token_exists and user_info:
        try:
            user_id = user_info.get('user_email') or user_info.get('sub')
            if user_id:
                user = User(user_id)
                login_user(user)
                return redirect(request.args.get('next') or url_for('home.index'))
        except Exception:
            pass

    return render_template('auth/login.html', org=org, login_url=login_url,
                           token_exists=token_exists, user_info=user_info)

@auth_bp.route('/login/status', methods=['GET'])
def login_status():
    token_file = current_app.config.get('BORDER0_TOKEN_PATH')
    return jsonify({'token_exists': os.path.isfile(token_file)})

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))

@auth_bp.route('/system', methods=['GET', 'POST'])
@login_required
def system():
    """Handle system operations: show system page on GET; perform reboot, update checks, upgrade, or factory reset on POST."""
    if request.method == 'POST':
        action = request.form.get('action')
        # Reboot
        if action == 'reboot':
            try:
                subprocess.Popen(['systemctl', 'reboot'])
                flash('Rebooting system...', 'info')
            except Exception as e:
                flash(f'Failed to reboot system: {e}', 'danger')
            return redirect(url_for('auth.system'))
        # Check for updates
        if action == 'check_update':
            cli = current_app.config.get('BORDER0_CLI_PATH', 'border0')
            cache_dir = '/etc/border0'
            cache_file = os.path.join(cache_dir, 'version_cache.json')
            os.makedirs(cache_dir, exist_ok=True)
            # Get current version
            try:
                out = subprocess.check_output([cli, '--version'], stderr=subprocess.STDOUT, text=True, timeout=30)
                m = re.search(r'version:\s*(v\S+)', out)
                current_version = m.group(1) if m else 'unknown'
            except Exception as e:
                flash(f'Failed to get current Border0 version: {e}', 'warning')
                return redirect(url_for('auth.system'))
            # Get latest version
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
            return redirect(url_for('auth.system'))
        # Factory reset
        if action == 'factory_reset':
            errors = []
            # Paths to clean
            paths = [
                current_app.config.get('WAN_IFACE_PATH'),
                current_app.config.get('LAN_IFACE_PATH'),
                Config.BORDER0_TOKEN_PATH,
                Config.BORDER0_ORG_PATH,
                '/etc/border0/version_cache.json'
            ]
            # Remove files
            for p in paths:
                try:
                    if p and os.path.isfile(p):
                        os.remove(p)
                except Exception as e:
                    errors.append(str(e))
            # Clean interface configs and hostapd
            for pattern in ['/etc/network/interfaces.d/*.conf', '/etc/hostapd/*.conf']:
                for file in glob.glob(pattern):
                    try:
                        os.remove(file)
                    except Exception as e:
                        errors.append(str(e))
            if errors:
                flash(f"Factory reset completed with errors: {'; '.join(errors)}", 'warning')
            else:
                flash('Factory reset completed. Restoring default settings. Rebooting...', 'info')
            # copy template files from /opt/border0/defaults to /etc
            # /opt/border0/defaults/etc/network/interfaces.d/dummy0.conf
            # /opt/border0/defaults/etc/network/interfaces.d/wlan0.conf
            # /opt/border0/defaults/etc/network/interfaces.d/eth0.conf
            # /opt/border0/defaults/etc/hostapd/wlan0.conf
            for file in glob.glob('/opt/border0/defaults/etc/network/interfaces.d/*.conf'):
                shutil.copy(file, f'/etc/network/interfaces.d/{os.path.basename(file)}')
            for file in glob.glob('/opt/border0/defaults/etc/hostapd/*.conf'):
                shutil.copy(file, f'/etc/hostapd/{os.path.basename(file)}')

            # execute "sync"
            subprocess.Popen(['sync'])

            # Reboot after factory reset
            try:
                subprocess.Popen(['systemctl', 'reboot'])
            except Exception as e:
                flash(f'Failed to reboot system: {e}', 'danger')
            return redirect(url_for('auth.system'))
        # Upgrade page
        if action == 'upgrade':
            return redirect(url_for('auth.upgrade'))
    # GET: display system info and version status
    # Uptime
    try:
        with open('/proc/uptime', 'r') as f:
            total_seconds = int(float(f.read().split()[0]))
        uptime_str = str(datetime.timedelta(seconds=total_seconds))
    except Exception:
        uptime_str = 'Unavailable'
    # Load version cache
    cache_file = os.path.join('/etc/border0', 'version_cache.json')
    current_version = 'unknown'
    update_available = False
    new_version = None
    try:
        with open(cache_file) as f:
            data = json.load(f)
            current_version = data.get('current_version', current_version)
            update_available = data.get('update_available', False)
            new_version = data.get('new_version')
    except Exception:
        pass
    return render_template(
        'auth/system.html',
        uptime=uptime_str,
        current_version=current_version,
        update_available=update_available,
        new_version=new_version
    )
