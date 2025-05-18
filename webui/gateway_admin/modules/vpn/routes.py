from flask import Blueprint, render_template, request, redirect, url_for, flash
import json
from flask_login import login_required
import os
import subprocess
import re
import time
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
                return f.read().strip()
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
                except Exception as e:
                    flash(f'Failed to save organization: {e}', 'danger')
            return redirect(url_for('vpn.index'))

        # Initiate Border0 client login to obtain URL
        elif action == 'login':
            if not org:
                flash('Please set the organization name first.', 'danger')
            else:
                try:
                    # Launch Border0 CLI login to obtain the URL and stay running
                    proc = subprocess.Popen(
                        [Config.BORDER0_CLI_PATH, 'client', 'login', '--org', org],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        preexec_fn=os.setsid
                    )
                    # Parse first HTTP(S) URL from output
                    pattern_url = re.compile(r'(https?://\S+)')
                    for line in proc.stdout:
                        m = pattern_url.search(line)
                        if m:
                            login_url = m.group(1)
                            break
                    if not login_url:
                        flash('Login URL not found in CLI output.', 'danger')
                    # Process stays alive to wait for user to complete login
                except Exception as e:
                    flash(f'Error running login command: {e}', 'danger')
            # fall through to render with login_url if set

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

    # Determine status for rendering
    token_exists = os.path.isfile(token_file)
    # Fetch exit node options and current selection
    exit_nodes = []
    current_exit_node = ''
    exitnode_error = None
    if token_exists:
        state = None
        state_file = os.path.join(os.path.dirname(os.path.expanduser(Config.BORDER0_TOKEN_PATH)), 'device.state.yaml')
        try:
            mtime = os.path.getmtime(state_file)
            if time.time() - mtime < 3600:
                import yaml
                with open(state_file) as f:
                    state = yaml.safe_load(f)
        except Exception:
            state = None
        if state is None:
            try:
                result = subprocess.run(
                    [Config.BORDER0_CLI_PATH, 'node', 'state', 'show', '--json'],
                    capture_output=True, text=True, timeout=20
                )
                if result.returncode == 0:
                    state = json.loads(result.stdout)
                else:
                    exitnode_error = result.stderr or result.stdout
            except Exception as e:
                exitnode_error = str(e)
        if state:
            current_exit_node = state.get('exit_node', '') or ''
            for peer in state.get('peers', []):
                for service in peer.get('services', []):
                    if service.get('type') == 'exit_node':
                        exit_nodes.append({
                            'name': service.get('name'),
                            'peer_name': peer.get('name'),
                            'dns_name': service.get('dns_name'),
                            'public_ips': service.get('public_ips', [])
                        })
    return render_template(
        'vpn/index.html',
        org=org,
        login_url=login_url,
        token_exists=token_exists,
        exit_nodes=exit_nodes,
        current_exit_node=current_exit_node,
        exitnode_error=exitnode_error
    )
