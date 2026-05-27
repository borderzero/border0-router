from flask import (
    Blueprint, Response, current_app, flash, redirect, render_template,
    request, session, url_for,
)
from flask_login import current_user, login_required
import json
import base64
import datetime
import os
import secrets
import shutil
import subprocess
import re
import time
import signal
import stat
import threading
import yaml
from ...config import Config
from ...auth_mode import ANONYMOUS_USER_ID


def _decode_jwt_payload(token_path):
    """Return the decoded JWT payload dict, or None on any failure."""
    try:
        token_str = open(token_path).read().strip()
        parts = token_str.split('.')
        if len(parts) < 2:
            return None
        padding = '=' * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except Exception:
        return None


def _ts_to_iso(ts):
    if not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.datetime.fromtimestamp(ts).isoformat(sep=' ', timespec='seconds')
    except Exception:
        return None


def _summarize_client_token(token_path):
    """Inspect /root/.border0/client_token and classify it.

    Returns a dict the template can render directly, or None if no token
    is present. The 'kind' field is one of 'service_account', 'user', or
    'unknown' so the UI can label the token clearly (E2E vs SSO).
    """
    if not os.path.isfile(token_path):
        return None
    payload = _decode_jwt_payload(token_path)
    try:
        stat = os.stat(token_path)
        mtime_iso = datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(sep=' ', timespec='seconds')
    except Exception:
        mtime_iso = None
    info = {
        'path': token_path,
        'mtime': mtime_iso,
        'raw': payload or {},
    }
    if not payload:
        info['kind'] = 'unknown'
        return info
    if payload.get('service_account_id') or payload.get('service_account'):
        info['kind'] = 'service_account'
        info['identity'] = payload.get('service_account') or payload.get('nickname')
        info['identity_id'] = payload.get('service_account_id') or payload.get('user_id')
    elif payload.get('user_email') or payload.get('sub'):
        info['kind'] = 'user'
        info['identity'] = payload.get('user_email') or payload.get('name') or payload.get('nickname')
        info['identity_id'] = payload.get('sub') or payload.get('user_id')
    else:
        info['kind'] = 'unknown'
        info['identity'] = payload.get('nickname') or payload.get('user_id')
        info['identity_id'] = payload.get('user_id')
    info['org_subdomain'] = payload.get('org_subdomain')
    info['org_id'] = payload.get('org_id')
    info['issued_at'] = _ts_to_iso(payload.get('iat'))
    info['expires_at'] = _ts_to_iso(payload.get('exp'))
    return info


def _load_device_state(state_path):
    """Parse device.state.yaml and return a flat dict for the current org."""
    if not os.path.isfile(state_path):
        return None
    try:
        with open(state_path) as f:
            doc = yaml.safe_load(f) or {}
    except Exception:
        return None
    current = doc.get('current_organization')
    orgs = doc.get('organizations') or {}
    org_block = orgs.get(current) if current else None
    if not isinstance(org_block, dict):
        # Fall back to the first org block if 'current_organization' is missing.
        for v in orgs.values():
            if isinstance(v, dict):
                org_block = v
                break
    if not isinstance(org_block, dict):
        return None
    key_block = org_block.get('key') or {}
    profile = org_block.get('profile') or {}
    return {
        'device_id': org_block.get('device_id'),
        'self_ip_v4': org_block.get('self_ip_v4'),
        'self_ip_v6': org_block.get('self_ip_v6'),
        'network_cidr_v4': org_block.get('network_cidr_v4'),
        'network_cidr_v6': org_block.get('network_cidr_v6'),
        'resources_cidr_v4': org_block.get('resources_cidr_v4'),
        'resources_cidr_v6': org_block.get('resources_cidr_v6'),
        'public_key': key_block.get('public_key'),
        'key_expires_at': key_block.get('expires_at'),
        'managed_interfaces': org_block.get('managed_network_interfaces') or [],
        'exit_node': org_block.get('exit_node'),
        'last_updated_at': org_block.get('last_updated_at'),
        'profile_name': profile.get('name'),
        'profile_email': profile.get('email'),
        'org_subdomain': profile.get('org_subdomain'),
        'org_id': profile.get('org_id'),
    }

vpn_bp = Blueprint('vpn', __name__, url_prefix='/vpn')


def _refuse_anonymous_credential_op(action_label):
    """Block sensitive credential ops when the caller is the synthetic
    anonymous user (mode='none'). Returns a redirect Response if the
    op should be refused, else None.

    Reasoning: even though the operator opted into 'none' mode for the
    UI, the runtime CLI token is a bearer credential whose blast radius
    extends well past the trusted LAN. Anyone on the LAN exfiltrating
    it gets remote access. Require an authenticated identity (sso or
    local) for token download / swap.
    """
    if current_user.get_id() == ANONYMOUS_USER_ID:
        flash(
            f'Cannot {action_label} while web UI authentication is '
            'disabled. Switch to Border0 SSO or local user/password '
            'on the System page first.',
            'danger',
        )
        return redirect(url_for('vpn.index'))
    return None


def _read_token_no_follow(token_path):
    """Read the token file refusing to follow symlinks. Mirrors the
    hardening pattern from auth_mode._open_no_follow_read so a future
    misconfiguration that points BORDER0_TOKEN_PATH at a group-writable
    dir can't be turned into a symlink-swap read primitive.
    """
    import stat as _stat
    fd = os.open(token_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise OSError(f'{token_path} is not a regular file')
        with os.fdopen(fd, 'rb') as f:
            return f.read()
    except Exception:
        os.close(fd)
        raise


@vpn_bp.route('/token/download', methods=['POST'])
@login_required
def token_download():
    """Stream the on-disk Border0 client token as a file download.

    POST (not GET) so link-prefetchers, unfurlers, and accidental clicks
    don't drag credential bytes through caches and logs. Any
    authenticated UI session may export the token — this matches the
    existing upload-token surface; the app has no role/RBAC concept.
    """
    refusal = _refuse_anonymous_credential_op('download the client token')
    if refusal is not None:
        return refusal
    token_path = Config.BORDER0_TOKEN_PATH
    if not token_path or not os.path.isfile(token_path):
        flash('No client token is currently on disk.', 'warning')
        return redirect(url_for('vpn.index'))
    try:
        payload = _read_token_no_follow(token_path)
    except OSError as e:
        current_app.logger.warning(
            'token_download: failed to read %s: %s', token_path, e
        )
        flash(f'Failed to read token file: {e}', 'danger')
        return redirect(url_for('vpn.index'))
    current_app.logger.info(
        'token download: user=%s ip=%s bytes=%d',
        current_user.get_id(), request.remote_addr or 'unknown', len(payload),
    )
    return Response(
        payload,
        mimetype='application/octet-stream',
        headers={
            'Content-Disposition': 'attachment; filename="client_token"',
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
            'Cross-Origin-Resource-Policy': 'same-origin',
        },
    )


def _identity_from_payload(payload):
    return (
        payload.get('service_account')
        or payload.get('user_email')
        or payload.get('nickname')
        or payload.get('sub')
        or payload.get('user_id')
        or 'unknown'
    )


def _kind_from_payload(payload):
    if payload.get('service_account_id') or payload.get('service_account'):
        return 'service_account'
    if payload.get('user_email') or payload.get('sub'):
        return 'user'
    return 'unknown'


def _read_current_org_id():
    """Return the current device's org_id from /etc/border0/org, or None."""
    try:
        with open(Config.BORDER0_ORG_PATH) as f:
            data = json.load(f)
        return data.get('org_id')
    except Exception:
        return None


# Cap on the size of the pasted JWT. Real Border0 tokens are well under
# 1KB; a 8KB ceiling leaves room for unforeseen growth and stops a multi-MB
# paste from being base64-decoded into RAM on the request thread.
MAX_REPLACEMENT_TOKEN_BYTES = 8192

# Serialize file swap + daemon restart so two concurrent operators can't
# interleave writes and racing systemctl restarts.
_token_swap_lock = threading.Lock()


def _validate_replacement_token(jwt_str, current_org_id=None):
    """Decode + validate a candidate client_token.

    Returns (payload_or_None, errors_list, warnings_list). Errors block
    the swap; warnings can be overridden by the operator (e.g. swapping
    to a token for a different org is legal but suspicious).
    """
    errors = []
    warnings = []
    s = (jwt_str or '').strip()
    if not s:
        errors.append('No token provided.')
        return None, errors, warnings
    if len(s) > MAX_REPLACEMENT_TOKEN_BYTES:
        errors.append(
            'Token is unreasonably large ({} bytes; cap is {}).'.format(
                len(s), MAX_REPLACEMENT_TOKEN_BYTES
            )
        )
        return None, errors, warnings
    parts = s.split('.')
    if len(parts) != 3:
        errors.append('Token does not look like a JWT (expected 3 dot-separated parts).')
        return None, errors, warnings
    try:
        padding = '=' * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except Exception as e:
        errors.append(f'Failed to decode JWT payload: {e}')
        return None, errors, warnings
    if not isinstance(payload, dict):
        errors.append('JWT payload is not a JSON object.')
        return None, errors, warnings
    if not payload.get('org_id'):
        errors.append('Token does not appear to be a Border0 token (no org_id).')
        return None, errors, warnings
    exp = payload.get('exp')
    if isinstance(exp, (int, float)) and exp < time.time():
        errors.append('Token has already expired.')
    if current_org_id and payload.get('org_id') != current_org_id:
        warnings.append(
            'Token org_id ({}) does not match current device org_id ({}).'.format(
                payload.get('org_id'), current_org_id
            )
        )
    return payload, errors, warnings


@vpn_bp.route('/token/preview', methods=['POST'])
@login_required
def token_preview():
    """Validate a pasted replacement token and stash the preview."""
    refusal = _refuse_anonymous_credential_op('preview a replacement token')
    if refusal is not None:
        return refusal
    new_token = (request.form.get('new_token') or '').strip()
    current_org_id = _read_current_org_id()
    payload, errors, warnings = _validate_replacement_token(new_token, current_org_id)
    if errors:
        for e in errors:
            flash(e, 'danger')
        return redirect(url_for('vpn.index'))
    session['token_replacement_preview'] = {
        'token': new_token,
        'identity': _identity_from_payload(payload),
        'kind': _kind_from_payload(payload),
        'org_id': payload.get('org_id'),
        'org_subdomain': payload.get('org_subdomain'),
        'expires_at': _ts_to_iso(payload.get('exp')),
        'issued_at': _ts_to_iso(payload.get('iat')),
        'warnings': warnings,
    }
    return redirect(url_for('vpn.index'))


@vpn_bp.route('/token/cancel', methods=['POST'])
@login_required
def token_cancel():
    """Discard the pending replacement-token preview."""
    session.pop('token_replacement_preview', None)
    return redirect(url_for('vpn.index'))


@vpn_bp.route('/token/apply', methods=['POST'])
@login_required
def token_apply():
    """Atomically replace /root/.border0/client_token + restart daemon."""
    refusal = _refuse_anonymous_credential_op('replace the client token')
    if refusal is not None:
        return refusal
    new_token = (request.form.get('new_token') or '').strip()
    override = request.form.get('override_org_mismatch') == 'yes'
    current_org_id = _read_current_org_id()
    payload, errors, warnings = _validate_replacement_token(new_token, current_org_id)
    if errors:
        for e in errors:
            flash(e, 'danger')
        return redirect(url_for('vpn.index'))
    if warnings and not override:
        flash(
            ' '.join(warnings) +
            ' Re-submit with the override checkbox to apply anyway.',
            'warning',
        )
        return redirect(url_for('vpn.index'))

    token_path = Config.BORDER0_TOKEN_PATH
    if not token_path:
        flash('BORDER0_TOKEN_PATH is not configured.', 'danger')
        return redirect(url_for('vpn.index'))
    # Refuse to operate on a symlinked destination — even though
    # os.replace would atomically replace the symlink itself (not write
    # through it), we'd rather surface the unusual state than silently
    # detach whatever was set up there.
    if os.path.islink(token_path):
        flash(
            f'{token_path} is a symlink; refusing to swap. Remove the '
            'symlink (and any stale target) before retrying.',
            'danger',
        )
        return redirect(url_for('vpn.index'))

    # Record the identity we're about to replace, for the audit log.
    old_identity = None
    prior = _decode_jwt_payload(token_path) if os.path.isfile(token_path) else None
    if prior:
        old_identity = _identity_from_payload(prior)

    backup_path = '{}.bak.{}'.format(token_path, int(time.time()))
    tmp_path = '{}.tmp.{}.{}'.format(token_path, os.getpid(), secrets.token_hex(4))
    backed_up = False
    restart_failed_reason = None
    with _token_swap_lock:
        try:
            os.makedirs(os.path.dirname(token_path), exist_ok=True)
            # Snapshot the old token (if any) so we can roll back on
            # failure. Tighten the backup perms before copying contents.
            if os.path.isfile(token_path) and not os.path.islink(token_path):
                shutil.copy2(token_path, backup_path)
                try: os.chmod(backup_path, 0o600)
                except OSError: pass
                backed_up = True
            # Write the new token atomically.
            fd = os.open(
                tmp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write(new_token)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                try: os.unlink(tmp_path)
                except OSError: pass
                raise
            os.replace(tmp_path, token_path)
            # Bounce the daemon so it picks up the new credential.
            try:
                rc = subprocess.run(
                    ['systemctl', 'restart', 'border0-device'],
                    check=False, timeout=15,
                    capture_output=True, text=True,
                )
                if rc.returncode != 0:
                    restart_failed_reason = (rc.stderr or rc.stdout or '').strip() or f'exit code {rc.returncode}'
            except Exception as e:
                restart_failed_reason = str(e)
        except Exception as e:
            # Roll back to the backup if we have one.
            if backed_up:
                try:
                    shutil.move(backup_path, token_path)
                    backed_up = False
                except Exception:
                    pass
            current_app.logger.warning('token_apply failed: %s', e)
            flash(f'Failed to apply replacement token: {e}', 'danger')
            return redirect(url_for('vpn.index'))
        finally:
            # Always try to clean up the backup once we're done — keeps
            # the dir tidy and stops backup files accumulating.
            if backed_up:
                try: os.remove(backup_path)
                except OSError: pass

    session.pop('token_replacement_preview', None)
    current_app.logger.info(
        'token replaced: user=%s ip=%s old_identity=%s new_kind=%s new_identity=%s',
        current_user.get_id(),
        request.remote_addr or 'unknown',
        old_identity or 'none',
        _kind_from_payload(payload),
        _identity_from_payload(payload),
    )
    if restart_failed_reason:
        current_app.logger.warning(
            'token_apply: border0-device restart failed: %s',
            restart_failed_reason,
        )
        flash(
            'Token replaced, but restarting border0-device failed: '
            f'{restart_failed_reason}. Check service logs.',
            'warning',
        )
    else:
        flash(
            'Client token replaced. border0-device has been restarted.',
            'success',
        )
    return redirect(url_for('vpn.index'))


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
        # Reset organization settings and stop VPN service
        if action == 'reset_org':
            errors = []
            # Remove saved organization file
            org_path = Config.BORDER0_ORG_PATH
            try:
                if org_path and os.path.isfile(org_path):
                    os.remove(org_path)
            except Exception as e:
                errors.append(f"org file removal error: {e}")
            # Note: do not remove client token; login will overwrite existing token
            # Remove device state file
            try:
                token_path = Config.BORDER0_TOKEN_PATH
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
                flash('Organization settings reset; VPN service stopped.', 'success')
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
                        [Config.BORDER0_CLI_PATH, 'client', 'login', f'--org={org}'],
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

                    if proc.poll() is not None:
                        try:
                            proc.wait()
                        except Exception:
                            pass
                    if login_url:
                        # schedule CLI login process cleanup after timeout
                        def _cleanup(proc):
                            try:
                                os.killpg(proc.pid, signal.SIGTERM)
                            except Exception:
                                pass
                            try:
                                proc.wait(timeout=5)
                            except Exception:
                                pass
                            current_app.logger.info(f'Border0 CLI login process {proc.pid} cleaned up')
                        timer = threading.Timer(120, _cleanup, args=(proc,))
                        timer.daemon = True
                        timer.start()
                        # monitor token then restart device
                        def _monitor(token_path, proc):
                            for _ in range(60):
                                if os.path.isfile(token_path):
                                    try:
                                        os.killpg(proc.pid, signal.SIGTERM)
                                    except Exception:
                                        pass
                                    try:
                                        proc.wait(timeout=5)
                                    except Exception:
                                        pass
                                    subprocess.run(['systemctl', 'restart', 'border0-device'], check=False)
                                    current_app.logger.info('Border0 device service restarted after token acquisition')
                                    break
                                time.sleep(2)
                        threading.Thread(target=_monitor, args=(token_file, proc), daemon=True).start()
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
                        [Config.BORDER0_CLI_PATH, 'client', 'login', f'--org={org}'],
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

                    if proc.poll() is not None:
                        try:
                            proc.wait()
                        except Exception:
                            pass
                    if login_url:
                        # schedule CLI login process cleanup after timeout
                        def _cleanup_login(proc):
                            try:
                                os.killpg(proc.pid, signal.SIGTERM)
                            except Exception:
                                pass
                            try:
                                proc.wait(timeout=5)
                            except Exception:
                                pass
                            current_app.logger.info(f'Border0 CLI login process {proc.pid} cleaned up (login)')
                        timer = threading.Timer(120, _cleanup_login, args=(proc,))
                        timer.daemon = True
                        timer.start()
                        # schedule restart of border0-device service once token is present
                        def _monitor_and_restart(token_path, proc):
                            for _ in range(60):
                                if os.path.isfile(token_path):
                                    try:
                                        os.killpg(proc.pid, signal.SIGTERM)
                                    except Exception:
                                        pass
                                    try:
                                        proc.wait(timeout=5)
                                    except Exception:
                                        pass
                                    subprocess.run(['systemctl', 'restart', 'border0-device'], check=False)
                                    current_app.logger.info('Border0 device service restarted after login')
                                    break
                                time.sleep(2)
                        monitor_thread = threading.Thread(
                            target=_monitor_and_restart,
                            args=(token_file, proc),
                            daemon=True
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
    # Load user information: prefer cached metadata, else decode token
    user_info = None
    metadata_path = Config.BORDER0_TOKEN_METADATA_PATH
    if os.path.isfile(metadata_path):
        try:
            with open(metadata_path) as mf:
                user_info = json.load(mf)
        except Exception:
            user_info = None
    elif token_exists:
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
        # Populate exit nodes directly from state.
        # Newer border0 CLI returns peers_v2 / services_v2 as dicts keyed by
        # public_key / service_name. Fall back to the legacy list-based
        # peers / services keys for older CLI builds.
        if state:
            peers = state.get('peers_v2') or state.get('peers') or []
            peer_iter = peers.values() if isinstance(peers, dict) else peers
            for peer in peer_iter:
                peer_name = peer.get('name')
                services = peer.get('services_v2') or peer.get('services') or []
                svc_iter = services.values() if isinstance(services, dict) else services
                for service in svc_iter:
                    if service.get('type') == 'exit_node':
                        exit_nodes.append({
                            'name': service.get('name'),
                            'peer_name': peer_name,
                            'dns_name': service.get('dns_name'),
                            'public_ips': service.get('public_ips', [])
                        })
    client_token_info = _summarize_client_token(token_file)
    device_state_path = os.path.join(os.path.dirname(token_file or ''), 'device.state.yaml')
    device_state = _load_device_state(device_state_path)
    replacement_preview = session.get('token_replacement_preview')
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
        client_token_info=client_token_info,
        device_state=device_state,
        replacement_preview=replacement_preview,
        current_org_id=_read_current_org_id(),
    )
