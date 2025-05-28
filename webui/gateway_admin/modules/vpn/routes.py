from flask import Blueprint, render_template, request, redirect, url_for, flash
import json
import base64
from flask_login import login_required
import os
import subprocess
import re
import time
import signal
import threading
from ...config import Config

vpn_bp = Blueprint('vpn', __name__, url_prefix='/vpn')

@vpn_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    """VPN configuration interface: set org, initiate login, upload client token, and install VPN."""
    # Helpers to get/save org
    def get_org():
        # Env var override preferred
        if Config.BORDER0_ORG:
            return Config.BORDER0_ORG
        try:
            with open(Config.BORDER0_ORG_PATH) as f:
                content = f.read().strip()
                # support JSON with org_subdomain and org_id
                try:
                    data = json.loads(content)
                    return data.get('org_subdomain', '')
                except ValueError:
                    return content
        except IOError:
            return ''

    def save_org(org_name):
        os.makedirs(os.path.dirname(Config.BORDER0_ORG_PATH), exist_ok=True)
        with open(Config.BORDER0_ORG_PATH, 'w') as f:
            f.write(org_name)

    token_file = Config.BORDER0_TOKEN_PATH
    org = get_org()
    login_url = None

    if request.method == 'POST':
        action = request.form.get('action')
        # Reset organization and client token settings, and stop VPN service
        if action == 'reset_org':
            errors = []
            # Remove saved organization file
            org_path = Config.BORDER0_ORG_PATH
            try:
                if org_path and os.path.isfile(org_path):
                    os.remove(org_path)
            except Exception as e:
                errors.append(f"org file removal error: {e}")
            # Remove saved client token
            token_path = Config.BORDER0_TOKEN_PATH
            try:
                if token_path and os.path.isfile(token_path):
                    os.remove(token_path)
            except Exception as e:
                errors.append(f"token removal error: {e}")
            # Remove device state file
            try:
                state_dir = os.path.dirname(token_path)
                state_file = os.path.join(state_dir, 'device.state.yaml')
                if os.path.isfile(state_file):
                    os.remove(state_file)
            except Exception as e:
                errors.append(f"device state removal error: {e}")
            # Stop the border0-device service
            try:
                subprocess.run(['systemctl', 'stop', 'border0-device'], check=False)
            except Exception as e:
                errors.append(f"service stop error: {e}")
            # Report results
            if errors:
                flash(f"Reset completed with errors: {'; '.join(errors)}", 'warning')
            else:
                flash('Organization settings, client token, and device state reset; VPN service stopped.', 'success')
            return redirect(url_for('vpn.index'))
        # Save organization name
        if action == 'save_org':
            org_name = request.form.get('org', '').strip()
            if not org_name:
                flash('Organization name cannot be empty.', 'danger')
            else:
                try:
                    save_org(org_name)
                    org = org_name
                    flash('Organization saved.', 'success')
                    # Auto-trigger Border0 CLI login for this org
                    try:
                        subprocess.run([
                            'pkill', '-f', f"{Config.BORDER0_CLI_PATH} client login"
                        ], check=False)
                    except Exception:
                        pass
                    env = os.environ.copy()
                    env.update({
                        'SHELL': '/bin/bash',
                        'LOGNAME': 'root',
                        'HOME': '/root',
                        'USER': 'root'
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
                    output_lines = []
                    pattern_url = re.compile(r'(https?://\S+)')
                    login_url = None
                    for line in proc.stdout:
                        output_lines.append(line)
                        m = pattern_url.search(line)
                        if m:
                            login_url = m.group(1)
                            break
                    if login_url:
                        # schedule CLI login process kill
                        def _kill(pid):
                            try:
                                os.killpg(pid, signal.SIGTERM)
                            except Exception:
                                pass
                        timer = threading.Timer(120, _kill, args=(proc.pid,))
                        timer.daemon = True
                        timer.start()
                        # monitor token then restart device
                        def monitor(token_path):
                            for _ in range(60):
                                if os.path.isfile(token_path):
                                    subprocess.run(['systemctl', 'restart', 'border0-device'], check=False)
                                    break
                                time.sleep(2)
                        threading.Thread(target=monitor, args=(token_file,), daemon=True).start()
                    else:
                        msg = ''.join(output_lines).strip()
                        flash(f'Login URL not found: {msg}', 'danger')
                except Exception as e:
                    flash(f'Failed to save organization or initiate login: {e}', 'danger')
            # Render page with new login_url
            token_exists = os.path.isfile(token_file)
            # fall through to render at bottom
            # (login_url is set above if successful)
            pass

        # Initiate Border0 client login to obtain URL
        elif action == 'login':
            if not org:
                flash('Please set the organization name first.', 'danger')
            else:
                # avoid duplicate login processes
                try:
                    subprocess.run([
                        'pkill', '-f', f"{Config.BORDER0_CLI_PATH} client login"
                    ], check=False)
                except Exception:
                    pass
                try:
                    # Launch Border0 CLI login with proper environment so token is stored in $HOME/.border0
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
                    # Capture output until the first HTTP(S) URL appears
                    output_lines = []
                    pattern_url = re.compile(r'(https?://\S+)')
                    for line in proc.stdout:
                        output_lines.append(line)
                        m = pattern_url.search(line)
                        if m:
                            login_url = m.group(1)
                            break
                    if login_url:
                        # schedule process kill after timeout to allow user to complete login
                        def _kill_proc(pid):
                            try:
                                os.killpg(pid, signal.SIGTERM)
                            except Exception:
                                pass
                        timer = threading.Timer(
                            120,
                            _kill_proc,
                            args=(proc.pid,)
                        )
                        timer.daemon = True
                        timer.start()
                        # schedule restart of border0-device service once token is present
                        def monitor_and_restart(token_path):
                            for _ in range(60):
                                if os.path.isfile(token_path):
                                    subprocess.run(['systemctl', 'restart', 'border0-device'], check=False)
                                    break
                                time.sleep(2)
                        monitor_thread = threading.Thread(
                            target=monitor_and_restart, args=(token_file,), daemon=True
                        )
                        monitor_thread.start()
                    else:
                        msg = ''.join(output_lines).strip()
                        flash(f'Login URL not found in CLI output. Output: {msg}', 'danger')
                except Exception as e:
                    flash(f'Error running login command: {e}', 'danger')

        # Upload client token
        elif action == 'upload_token':
            token = request.form.get('token', '').strip()
            if token:
                try:
                    os.makedirs(os.path.dirname(token_file), exist_ok=True)
                    with open(token_file, 'w') as f:
                        f.write(token)
                    flash('Client token saved successfully.', 'success')
                except Exception as e:
                    flash(f'Failed to save client token: {e}', 'danger')
            else:
                flash('Token cannot be empty.', 'danger')
            return redirect(url_for('vpn.index'))

        # Restart VPN service
        elif action == 'install_vpn':
            try:
                result = subprocess.run(
                    ['systemctl', 'restart', 'border0-device'],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    flash('VPN service restarted successfully.', 'success')
                else:
                    msg = result.stderr or result.stdout
                    flash(f'Failed to restart VPN service: {msg}', 'danger')
            except Exception as e:
                flash(f'Error restarting VPN service: {e}', 'danger')
            return redirect(url_for('vpn.index'))
        # Set or unset exit node
        elif action == 'set_exitnode':
            selected = request.form.get('exit_node', '').strip()
            try:
                if selected and selected != 'none':
                    result = subprocess.run(
                        [Config.BORDER0_CLI_PATH, 'node', 'exitnode', 'set', selected, '--json'],
                        capture_output=True, text=True
                    )
                else:
                    result = subprocess.run(
                        [Config.BORDER0_CLI_PATH, 'node', 'exitnode', 'unset', '--json'],
                        capture_output=True, text=True
                    )
                if result.returncode == 0:
                    try:
                        msg = json.loads(result.stdout).get('message', '').strip()
                    except Exception:
                        msg = result.stdout.strip()
                    flash(msg, 'success')
                else:
                    msg = result.stderr or result.stdout
                    flash(f'Failed to set exit node: {msg}', 'danger')
            except Exception as e:
                flash(f'Error setting exit node: {e}', 'danger')
            return redirect(url_for('vpn.index'))

    # Determine border0-device service full status output
    try:
        status_proc = subprocess.run(
            ['systemctl', 'status', 'border0-device.service', '-n0'],
            capture_output=True, text=True, timeout=5
        )
        device_status = status_proc.stdout
        service_active = 'Active: active' in device_status
    except Exception as e:
        device_status = f'Error obtaining service status: {e}'
        service_active = False
    try:
        enabled_proc = subprocess.run(
            ['systemctl', 'is-enabled', 'border0-device.service'],
            capture_output=True, text=True, timeout=5
        )
        service_enabled = enabled_proc.stdout.strip() == 'enabled'
    except Exception:
        service_enabled = False

    # Determine token existence (may be used for showing upload form)
    token_exists = os.path.isfile(token_file)
    # Decode client token to extract user information (name, nickname, picture, org_id, etc.)
    user_info = None
    if token_exists:
        try:
            token_str = open(token_file).read().strip()
            parts = token_str.split('.')
            if len(parts) >= 2:
                payload_b64 = parts[1]
                padding = '=' * (-len(payload_b64) % 4)
                payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
                user_info = json.loads(payload_bytes)
        except Exception:
            user_info = None
    # Fetch current exit node and list of available exit nodes
    exit_nodes = []
    current_exit_node = ''
    exitnode_error = None
    state = None
    if service_active:
        # Determine current exit node via state
        try:
            result = subprocess.run(
                [Config.BORDER0_CLI_PATH, 'node', 'state', 'show', '--json'],
                capture_output=True, text=True, timeout=20
            )
            if result.returncode == 0:
                state = json.loads(result.stdout)
                current_exit_node = state.get('exit_node', '') or ''
            else:
                exitnode_error = result.stderr or result.stdout
        except Exception as e:
            exitnode_error = str(e)
        # Populate exit nodes directly from state
        if state:
            for peer in state.get('peers', []):
                peer_name = peer.get('name')
                for service in peer.get('services', []):
                    if service.get('type') == 'exit_node':
                        exit_nodes.append({
                            'name': service.get('name'),
                            'peer_name': peer_name,
                            'dns_name': service.get('dns_name'),
                            'public_ips': service.get('public_ips', [])
                        })
    return render_template(
        'vpn/index.html',
        org=org,
        login_url=login_url,
        token_exists=token_exists,
        token_file=token_file,
        service_active=service_active,
        service_enabled=service_enabled,
        exit_nodes=exit_nodes,
        current_exit_node=current_exit_node,
        exitnode_error=exitnode_error,
        device_status=device_status,
        user_info=user_info,
    )
