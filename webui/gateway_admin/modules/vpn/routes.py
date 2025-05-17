from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
import os
import subprocess
import re
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

    # Determine status for rendering
    token_exists = os.path.isfile(token_file)
    return render_template(
        'vpn/index.html',
        org=org,
        login_url=login_url,
        token_exists=token_exists
    )
