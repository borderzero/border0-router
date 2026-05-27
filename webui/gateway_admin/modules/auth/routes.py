from urllib.parse import urlparse, urljoin
from flask import Blueprint, render_template, redirect, url_for, request, flash, current_app, jsonify, session, g
from flask_login import login_user, logout_user, login_required, current_user, UserMixin
from ...config import Config
import os
import re
import json
import uuid
import glob
import shutil
import subprocess
import threading
import time
import signal
import base64
import hashlib
import datetime
import tempfile
import urllib.request
from ...extensions import login_manager
from ... import auth_mode

ANONYMOUS_USER_ID = 'anonymous@local'
# Endpoints that must remain reachable even when the session is being
# torn down by a mode change, so the user can finish the redirect chain
# without bouncing in a loop.
_AUTH_FLOW_ENDPOINTS = frozenset({
    'auth.login',
    'auth.login_status',
    'auth.login_callback',
    'auth.logout',
    'static',
})

# map login_id to Border0 CLI subprocess for ongoing login flows
pending_logins = {}
pending_lock = threading.Lock()
LOGIN_FLOW_TTL = 300  # seconds to keep pending login flows before cleanup
# Web-UI SSO flows run the CLI with HOME pointed at a per-flow tempdir so the
# CLI writes its client_token there (HOME/.border0/client_token) instead of
# stomping on /root/.border0/client_token. That keeps the runtime CLI token
# (e.g. a manually-installed E2E service-account token) isolated from the
# transient SSO token the web UI uses only to authenticate the browser
# session.
SSO_FLOW_ROOT = '/var/lib/border0-webui/ssoflows'


def _flow_token_path(flow_home):
    return os.path.join(flow_home, '.border0', 'client_token')


def _cleanup_flow_dir(flow_home):
    if not flow_home:
        return
    try:
        shutil.rmtree(flow_home, ignore_errors=True)
    except Exception:
        pass


# In-memory rate limit for local-auth POST /login. Per-IP sliding window.
# After _LOCAL_LOGIN_MAX_ATTEMPTS failures inside _LOCAL_LOGIN_WINDOW
# seconds, the IP is locked out for _LOCAL_LOGIN_LOCKOUT seconds.
#
# NOTE: keyed on the effective client IP per _client_ip(). If the webui
# is ever fronted by a reverse proxy, wrap the WSGI app with
# werkzeug.middleware.proxy_fix.ProxyFix so request.remote_addr reflects
# the real client, otherwise the whole internet shares one bucket and
# the lockout either DOSes legit users or does nothing.
_local_login_failures = {}
_local_login_lock = threading.Lock()
_LOCAL_LOGIN_MAX_ATTEMPTS = 5
_LOCAL_LOGIN_WINDOW = 60
_LOCAL_LOGIN_LOCKOUT = 60
_LOCAL_LOGIN_MAX_TRACKED_IPS = 4096  # FIFO cap on the failure dict


def _client_ip():
    """Best-effort identification of the requesting client.

    Uses ProxyFix-rewritten request.remote_addr when available. A noisy
    proxy without ProxyFix will degrade the lockout to per-proxy, which
    is the documented expected behavior.
    """
    return request.remote_addr or 'unknown'


def _local_login_evict_old(now):
    """Drop tracked IPs whose entries are all outside the rate window.

    Bounds the dict so a scanner hitting from many IPs can't balloon it.
    Caller must hold _local_login_lock.
    """
    cutoff = now - max(_LOCAL_LOGIN_WINDOW, _LOCAL_LOGIN_LOCKOUT)
    for ip in list(_local_login_failures.keys()):
        attempts = _local_login_failures[ip]
        if not attempts or attempts[-1] < cutoff:
            del _local_login_failures[ip]
    # Hard cap regardless: drop the oldest entries (by last attempt) until
    # we're back under the limit.
    if len(_local_login_failures) > _LOCAL_LOGIN_MAX_TRACKED_IPS:
        ordered = sorted(
            _local_login_failures.items(),
            key=lambda kv: kv[1][-1] if kv[1] else 0,
        )
        excess = len(_local_login_failures) - _LOCAL_LOGIN_MAX_TRACKED_IPS
        for ip, _ in ordered[:excess]:
            _local_login_failures.pop(ip, None)


def _local_login_check_lockout(ip):
    """Return None if the IP may attempt login, else seconds until unlock."""
    now = time.time()
    with _local_login_lock:
        _local_login_evict_old(now)
        attempts = [t for t in _local_login_failures.get(ip, []) if t > now - _LOCAL_LOGIN_WINDOW]
        if len(attempts) >= _LOCAL_LOGIN_MAX_ATTEMPTS:
            wait = (attempts[0] + _LOCAL_LOGIN_LOCKOUT) - now
            if wait > 0:
                _local_login_failures[ip] = attempts
                return int(wait) + 1
            _local_login_failures.pop(ip, None)
            return None
        _local_login_failures[ip] = attempts
        return None


def _local_login_record_failure(ip):
    now = time.time()
    with _local_login_lock:
        _local_login_evict_old(now)
        attempts = [t for t in _local_login_failures.get(ip, []) if t > now - _LOCAL_LOGIN_WINDOW]
        attempts.append(now)
        _local_login_failures[ip] = attempts


def _local_login_record_success(ip):
    with _local_login_lock:
        _local_login_failures.pop(ip, None)


def _is_safe_redirect(target):
    """Reject off-site / scheme-changing values for the post-login ``next``.

    Only allow same-host targets (or relative paths) over the same scheme.
    """
    if not target:
        return False
    ref = urlparse(request.host_url)
    test = urlparse(urljoin(request.host_url, target))
    return (
        test.scheme in ('http', 'https')
        and ref.netloc == test.netloc
    )


auth_bp = Blueprint('auth', __name__)


class User(UserMixin):
    def __init__(self, id):
        self.id = id


@login_manager.user_loader
def load_user(user_id):
    return User(user_id)


@auth_bp.before_app_request
def apply_auth_mode():
    """Honor the resolved auth mode before any other auth check runs.

    - mode=none : auto-log-in a synthetic anonymous user so every
      @login_required route passes. No session/JWT enforcement.
    - mode=sso/local : if the session was established under a different
      mode (e.g. operator just flipped the toggle), drop the session
      and bounce to /login so the user re-authenticates under the
      new regime.

    Stashes the resolved mode on flask.g so downstream hooks
    (enforce_single_session) get a consistent value within one request
    even if the underlying file's mtime changes mid-flight.
    """
    # request.endpoint is None for un-routed paths (404 candidates,
    # OPTIONS preflights, errors during routing). Don't mutate sessions
    # for those, and don't touch static-asset requests — both would
    # cause spurious Set-Cookie churn.
    endpoint = request.endpoint
    if endpoint is None or endpoint == 'static':
        return None

    mode = auth_mode.current_mode()
    g.auth_mode = mode

    if mode == 'none':
        # Already the synthetic anonymous user? Just make sure the
        # marker is correct and move on — no session mutation.
        if current_user.is_authenticated and current_user.get_id() == ANONYMOUS_USER_ID:
            if session.get('auth_mode') != 'none':
                session['auth_mode'] = 'none'
            return None
        # Drop any prior real-user session (token_iat, device_id, etc.)
        # before installing the synthetic one — we don't want stale
        # SSO state riding along under the anonymous identity.
        session.clear()
        login_user(User(ANONYMOUS_USER_ID))
        session.permanent = True
        session['auth_mode'] = 'none'
        return None

    # sso or local. Skip mismatch handling on the auth-flow endpoints
    # themselves so we don't redirect-loop while the user is mid-login.
    if endpoint in _AUTH_FLOW_ENDPOINTS:
        return None

    if current_user.is_authenticated:
        session_mode = session.get('auth_mode')
        if session_mode is None:
            # Back-fill once; pre-feature sessions are all SSO.
            session_mode = 'sso'
            session['auth_mode'] = 'sso'
        if session_mode != mode:
            # Mode changed under this session. Tear down + bounce.
            # NOTE: this triggers on plain GET requests, so a
            # cross-origin link click after a mode flip can drop the
            # victim's session. Acceptable — it's a logout, not an
            # auth grant — but worth knowing.
            logout_user()
            session.clear()
            flash('Authentication mode changed; please log in again.', 'warning')
            return redirect(url_for('auth.login'))
    return None


@auth_bp.before_app_request
def enforce_single_session():
    # Only meaningful in SSO mode — local/none don't have a JWT iat or
    # per-device cookie binding to enforce. Use the mode snapshot from
    # apply_auth_mode so both hooks see the same value within one
    # request even if the file mtime changes between them.
    mode = getattr(g, 'auth_mode', None) or auth_mode.current_mode()
    if mode != 'sso':
        return None
    # Invalidate sessions when a new login (token refresh) occurs elsewhere
    if current_user.is_authenticated:
        meta_file = current_app.config.get('BORDER0_TOKEN_METADATA_PATH')
        if meta_file and os.path.isfile(meta_file):
            try:
                data = json.loads(open(meta_file).read())
                # Expire if token was refreshed elsewhere
                server_iat = data.get('iat')
                if server_iat and session.get('token_iat') != server_iat:
                    logout_user()
                    flash('Your session has expired due to login on another device.', 'warning')
                    return redirect(url_for('auth.login'))
                # Enforce per-device binding via cookie
                device_cookie = request.cookies.get('device_id')
                if data.get('device_id') != device_cookie:
                    logout_user()
                    flash('Please log in on this device.', 'warning')
                    return redirect(url_for('auth.login'))
            except Exception:
                pass
    return None

def _handle_local_login():
    """Username/password sign-in for auth mode 'local'."""
    if current_user.is_authenticated:
        return redirect(url_for('home.index'))
    raw_next = request.args.get('next') or request.form.get('next') or ''
    next_url = raw_next if _is_safe_redirect(raw_next) else url_for('home.index')
    if request.method == 'POST':
        ip = _client_ip()
        lockout = _local_login_check_lockout(ip)
        if lockout is not None:
            flash(
                f'Too many failed attempts. Try again in {lockout} seconds.',
                'danger',
            )
        elif not auth_mode.has_local_credential():
            flash(
                'No local credential is configured. Set one on the System '
                'page before signing in.',
                'danger',
            )
        else:
            username = (request.form.get('username') or '').strip()
            password = request.form.get('password') or ''
            if username and password and auth_mode.verify_local_credential(username, password):
                _local_login_record_success(ip)
                # Wipe any prior session state (anonymous-mode marker,
                # stale SSO bits, attacker-prestamped cookies) before
                # binding the new identity. Defense-in-depth — Flask's
                # default itsdangerous-signed cookies aren't classically
                # session-fixable, but cookie-content carryover would
                # leak old mode markers into the new session anyway.
                session.clear()
                login_user(User(username))
                session.permanent = True
                session['auth_mode'] = 'local'
                current_app.logger.info(
                    'local-auth login OK: user=%s ip=%s', username, ip
                )
                return redirect(next_url)
            _local_login_record_failure(ip)
            current_app.logger.warning(
                'local-auth login FAILED: user=%r ip=%s', username, ip
            )
            flash('Invalid username or password.', 'danger')
    return render_template(
        'auth/login.html',
        auth_mode='local',
        local_credential_set=auth_mode.has_local_credential(),
        next_url=next_url,
        org=None,
        login_url=None,
        token_exists=False,
        user_info=None,
        locked=False,
        login_id=None,
    )


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    mode = auth_mode.current_mode()
    # In 'none' mode auth is disabled; the before_app_request hook has
    # already logged in an anonymous user. Visiting /login explicitly
    # should bounce home rather than render the (now-pointless) form.
    if mode == 'none':
        return redirect(url_for('home.index'))
    if mode == 'local':
        return _handle_local_login()
    if current_user.is_authenticated:
        return redirect(url_for('home.index'))
    # assign or preserve a unique login_id for this login flow
    login_id = request.values.get('login_id') or uuid.uuid4().hex

    # Determine if organization is locked via existing org file
    org_file = current_app.config.get('BORDER0_ORG_PATH')
    org_saved = None
    locked = False
    if os.path.isfile(org_file):
        try:
            content = open(org_file).read().strip()
            data = json.loads(content)
            org_saved = data.get('org_subdomain') or None
        except Exception:
            org_saved = content
        if org_saved:
            locked = True
    # Initial org value: use saved if locked
    org = org_saved if locked else None
    token_file = current_app.config.get('BORDER0_TOKEN_PATH')
    login_url = None

    if request.method == 'POST':
        skip_token = session.pop('skip_token', False)
        # For locked org, always use saved subdomain; else read from form
        if locked:
            org = org_saved
        else:
            org = request.form.get('org')
        # only allow "continue as" auto-login if user just completed SSO
        can_continue = session.pop('authenticated_via_SSO', False)
        token_present = os.path.isfile(token_file) and not skip_token
        if not token_present or not can_continue:
            if not org:
                flash('Please enter an organization name to log into.', 'danger')
            else:
                try:
                    subprocess.run(['pkill', '-f', f"{Config.BORDER0_CLI_PATH} client login"], check=False)
                except Exception:
                    pass

                try:
                    os.makedirs(SSO_FLOW_ROOT, exist_ok=True)
                    flow_home = tempfile.mkdtemp(prefix=f'{login_id}-', dir=SSO_FLOW_ROOT)
                    os.makedirs(os.path.join(flow_home, '.border0'), exist_ok=True)
                    flow_token_path = _flow_token_path(flow_home)
                    env = os.environ.copy()
                    env.update({
                        'SHELL': '/bin/bash',
                        'LOGNAME': 'root',
                        'HOME': flow_home,
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
                    with pending_lock:
                        pending_logins[login_id] = {
                            'proc': proc,
                            'start': time.time(),
                            'flow_home': flow_home,
                            'token_path': flow_token_path,
                        }
                    # collect initial CLI output lines
                    log_lines = []

                    pattern = re.compile(r'(https?://\S+)')
                    for line in proc.stdout:
                        log_lines.append(line)
                        m = pattern.search(line)
                        if m:
                            login_url = m.group(1)
                            break

                    # if process exited on its own, reap to avoid zombies
                    if proc.poll() is not None:
                        try:
                            proc.wait()
                        except Exception:
                            pass

                    if login_url:
                        current_app.logger.info(
                            "Border0 CLI login URL found; initial output:\n%s",
                            ''.join(log_lines)
                        )
                        # schedule CLI login process kill and cleanup after timeout
                        def _cleanup_proc(p):
                            try:
                                os.killpg(p.pid, signal.SIGTERM)
                            except Exception:
                                pass
                            try:
                                p.wait(timeout=5)
                            except Exception:
                                pass
                        timer = threading.Timer(120, _cleanup_proc, args=(proc,))
                        timer.daemon = True
                        timer.start()
                        # Provisioning of /root/.border0/client_token + bounce
                        # of border0-device happens inside login_callback so
                        # it runs synchronously before we rmtree the flow
                        # tempdir. No watcher thread needed here.
                    else:
                        # no URL found; kill CLI and reap, then log output
                        try:
                            os.killpg(proc.pid, signal.SIGTERM)
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            pass
                        with pending_lock:
                            pending_logins.pop(login_id, None)
                        _cleanup_flow_dir(flow_home)
                        current_app.logger.error(
                            "Border0 CLI login failed; output:\n%s",
                            ''.join(log_lines)
                        )
                        flash(
                            'Login URL not found; please try again. '
                            'See server logs for details.',
                            'danger'
                        )
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
                        # mark session as permanent and record the token iat & SSO auth flag
                        session.permanent = True
                        if isinstance(payload.get('iat'), (int, str)):
                            session['token_iat'] = payload.get('iat')
                        session['authenticated_via_SSO'] = True
                        session['auth_mode'] = 'sso'
                        # persist org_id/subdomain and token metadata for UI display
                        org_path = current_app.config.get('BORDER0_ORG_PATH')
                        try:
                            os.makedirs(os.path.dirname(org_path), exist_ok=True)
                            if payload.get('org_id') and payload.get('org_subdomain') and not os.path.isfile(org_path):
                                with open(org_path, 'w') as f:
                                    json.dump({'org_subdomain': payload.get('org_subdomain'), 'org_id': payload.get('org_id')}, f)
                        except Exception:
                            pass
                        meta_path = current_app.config.get('BORDER0_TOKEN_METADATA_PATH')
                        try:
                            os.makedirs(os.path.dirname(meta_path), exist_ok=True)
                            with open(meta_path, 'w') as mf:
                                json.dump(payload, mf)
                        except Exception:
                            pass
                        return redirect(request.args.get('next') or url_for('home.index'))
            except Exception as e:
                flash(f'Failed to authenticate: {e}', 'danger')

    # Always require a fresh SSO flow on this device/browser
    return render_template(
        'auth/login.html',
        auth_mode='sso',
        org=org,
        login_url=login_url,
        token_exists=False,
        user_info=None,
        locked=locked,
        login_id=login_id
    )

@auth_bp.route('/login/status', methods=['GET'])
def login_status():
    login_id = request.args.get('login_id')
    error = None
    now = time.time()
    expired_homes = []
    with pending_lock:
        for lid, entry in list(pending_logins.items()):
            if now - entry['start'] > LOGIN_FLOW_TTL:
                try:
                    os.killpg(entry['proc'].pid, signal.SIGTERM)
                except Exception:
                    pass
                expired_homes.append(entry.get('flow_home'))
                pending_logins.pop(lid, None)
        entry = pending_logins.get(login_id)
    for home in expired_homes:
        _cleanup_flow_dir(home)
    # Only the SSO subprocess for *this* login_id is allowed to satisfy the
    # status check; a stale token file at a different path (CLI E2E token,
    # previous flow) must never short-circuit the poll.
    authenticated = False
    if entry is not None:
        flow_token = entry.get('token_path')
        if flow_token and os.path.isfile(flow_token):
            authenticated = True
    proc = entry['proc'] if entry else None
    if not authenticated and proc is not None:
        code = proc.poll()
        if code is not None and code != 0:
            error = f'Authentication process exited with code {code}'
            with pending_lock:
                pending_logins.pop(login_id, None)
            _cleanup_flow_dir(entry.get('flow_home'))
    return jsonify({'authenticated': authenticated, 'error': error})

@auth_bp.route('/login/callback')
def login_callback():
    login_id = request.args.get('login_id')
    with pending_lock:
        entry = pending_logins.pop(login_id, None)
    proc = entry['proc'] if entry else None
    flow_home = entry.get('flow_home') if entry else None
    flow_token = entry.get('token_path') if entry else None
    if not proc:
        flash('Invalid or expired login flow. Please log in again.', 'danger')
        return redirect(url_for('auth.login'))
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass

    if not flow_token or not os.path.isfile(flow_token):
        _cleanup_flow_dir(flow_home)
        flash('Login did not complete; please try again.', 'danger')
        return redirect(url_for('auth.login'))

    real_token = current_app.config.get('BORDER0_TOKEN_PATH')
    try:
        token_str = open(flow_token).read().strip()
        parts = token_str.split('.')
        if len(parts) >= 2:
            padding = '=' * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
            user_id = payload.get('user_email') or payload.get('sub')
            if user_id:
                user = User(user_id)
                login_user(user)
                session.permanent = True
                if isinstance(payload.get('iat'), (int, str)):
                    session['token_iat'] = payload.get('iat')
                session['authenticated_via_SSO'] = True
                session['auth_mode'] = 'sso'
                # Generate and bind a per-device ID
                device_id = uuid.uuid4().hex
                response = redirect(request.args.get('next') or url_for('home.index'))
                response.set_cookie('device_id', device_id, httponly=True, samesite='Lax')

                # First-install provisioning: seed /root/.border0/client_token
                # from the SSO token only if the operator hasn't put their
                # own credential there (E2E paste, prior install, etc.).
                # Restart border0-device so the daemon picks up the new
                # credential. Done synchronously here so we don't race
                # against the flow tempdir cleanup below.
                provisioned = False
                if real_token and not os.path.isfile(real_token):
                    try:
                        os.makedirs(os.path.dirname(real_token), exist_ok=True)
                        shutil.copy2(flow_token, real_token)
                        provisioned = True
                    except Exception as e:
                        current_app.logger.warning(
                            f"Failed to seed {real_token}: {e}"
                        )
                if provisioned:
                    try:
                        subprocess.run(
                            ['systemctl', 'restart', 'border0-device'],
                            check=False,
                        )
                    except Exception:
                        pass

                org_path = current_app.config.get('BORDER0_ORG_PATH')
                try:
                    os.makedirs(os.path.dirname(org_path), exist_ok=True)
                    if payload.get('org_id') and payload.get('org_subdomain') and not os.path.isfile(org_path):
                        with open(org_path, 'w') as f:
                            json.dump({'org_subdomain': payload.get('org_subdomain'), 'org_id': payload.get('org_id')}, f)
                except Exception:
                    pass
                meta_path = current_app.config.get('BORDER0_TOKEN_METADATA_PATH')
                try:
                    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
                    meta = payload.copy()
                    meta['device_id'] = device_id
                    with open(meta_path, 'w') as mf:
                        json.dump(meta, mf)
                    breadcrumb_path = '/etc/border0/first_login_done'
                    try:
                        if not os.path.exists(breadcrumb_path):
                            os.makedirs(os.path.dirname(breadcrumb_path), exist_ok=True)
                            with open(breadcrumb_path, 'w') as bf:
                                bf.write('')
                            current_app.logger.info(
                                f"Created first login breadcrumb file: {breadcrumb_path}"
                            )
                    except Exception as e:
                        current_app.logger.info(
                            f"Failed to create breadcrumb file {breadcrumb_path}: {e}"
                        )
                except Exception:
                    pass
                _cleanup_flow_dir(flow_home)
                return response
    except Exception as e:
        flash(f'Failed to authenticate: {e}', 'danger')
    _cleanup_flow_dir(flow_home)
    return redirect(url_for('auth.login'))
@auth_bp.route('/switch_user', methods=['POST'])
@login_required
def switch_user():
    # Clear UI session and skip using existing token for this session
    logout_user()
    session['skip_token'] = True
    flash('Please log in as a different user.', 'info')
    return redirect(url_for('auth.login'))
    
@auth_bp.route('/logout', methods=['GET', 'POST'])
@login_required
def logout():
    mode = auth_mode.current_mode()
    # In 'none' mode there is nothing to log out of — the next request
    # will just be auto-logged-in again by apply_auth_mode.
    if mode == 'none':
        flash('Authentication is disabled; logout has no effect.', 'info')
        return redirect(url_for('home.index'))
    # In 'local' mode there's no SSO token info to display; just clear
    # the session and bounce back to /login.
    if mode == 'local':
        logout_user()
        session.clear()
        flash('You have been logged out.', 'info')
        return redirect(url_for('auth.login'))
    token_file = current_app.config.get('BORDER0_TOKEN_PATH')
    meta_file = current_app.config.get('BORDER0_TOKEN_METADATA_PATH')
    token_exists = os.path.isfile(meta_file)
    token_info = None
    if token_exists:
        try:
            with open(meta_file) as mf:
                payload = json.load(mf)
            exp_ts = payload.get('exp')
            exp = None
            if isinstance(exp_ts, (int, float)):
                exp = datetime.datetime.fromtimestamp(exp_ts)
            token_info = type('TokenInfo', (), {
                'user_email': payload.get('user_email'),
                'org_subdomain': payload.get('org_subdomain'),
                'org_id': payload.get('org_id'),
                'exp': exp
            })
        except Exception:
            token_info = None
    if request.method == 'GET':
        return render_template('auth/logout.html', token_exists=token_exists, token_info=token_info)
    action = request.form.get('action')
    if action == 'cancel':
        return redirect(url_for('home.index'))
    logout_user()
    session['skip_token'] = True
    # Drop the web-UI session metadata only. The runtime CLI token at
    # token_file is owned by the operator (e.g. an E2E service-account token)
    # and must survive a UI logout so border0 CLI keeps working.
    try:
        os.remove(meta_file)
    except Exception:
        pass
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))

@auth_bp.route('/system', methods=['GET', 'POST'])
@login_required
def system():
    """Handle system operations: show system page on GET; perform reboot, update checks, upgrade, or factory reset on POST."""
    if request.method == 'POST':
        action = request.form.get('action')
        # Auth-mode toggle (radios + optional credential fields)
        if action == 'set_auth_mode':
            # In 'none' mode any LAN visitor is the synthetic anonymous
            # user. Letting that identity mutate the auth mode (or set
            # the local credential) would let an attacker on the trusted
            # network lock the real operator out. To switch *out of*
            # 'none', the operator has to either set BORDER0_WEBUI_AUTH_MODE
            # in the systemd unit or remove /etc/border0/auth_mode.json
            # by hand. Document this in the response.
            if current_user.get_id() == ANONYMOUS_USER_ID:
                flash(
                    'Cannot change auth mode while authentication is '
                    'disabled. Set BORDER0_WEBUI_AUTH_MODE in the '
                    'border0-webui systemd unit, or remove '
                    '/etc/border0/auth_mode.json, then restart the '
                    'border0-webui service.',
                    'danger',
                )
                return redirect(url_for('auth.system'))
            if auth_mode.is_env_locked():
                flash(
                    f'Auth mode is locked by {auth_mode.ENV_VAR} env var.',
                    'warning',
                )
                return redirect(url_for('auth.system'))
            new_mode = (request.form.get('mode') or '').strip().lower()
            if new_mode not in auth_mode.VALID_MODES:
                flash('Invalid auth mode.', 'danger')
                return redirect(url_for('auth.system'))
            if new_mode == 'local':
                username = (request.form.get('username') or '').strip()
                password = request.form.get('password') or ''
                confirm = request.form.get('password_confirm') or ''
                if password or confirm or not auth_mode.has_local_credential():
                    if password != confirm:
                        flash('Passwords do not match.', 'danger')
                        return redirect(url_for('auth.system'))
                    if not username or len(password) < 8:
                        flash(
                            'Local mode requires a username and an 8+ '
                            'character password.',
                            'danger',
                        )
                        return redirect(url_for('auth.system'))
                    try:
                        auth_mode.save_local_credential(username, password)
                    except ValueError as e:
                        flash(f'Failed to save credential: {e}', 'danger')
                        return redirect(url_for('auth.system'))
            try:
                auth_mode.save_mode(new_mode)
            except (RuntimeError, ValueError) as e:
                flash(str(e), 'danger')
                return redirect(url_for('auth.system'))
            current_app.logger.info(
                'auth mode changed to %s by %s from %s',
                new_mode, current_user.get_id(), _client_ip(),
            )
            # Force re-login under the new regime. The before-request hook
            # would do this on the next request anyway, but doing it here
            # gives a clean flash and saves a round-trip.
            logout_user()
            session.clear()
            flash(
                f'Authentication mode set to {new_mode}. Please sign in again.',
                'success',
            )
            return redirect(url_for('auth.login'))
        # Reset / change the local credential while staying in local mode.
        if action == 'set_local_password':
            if current_user.get_id() == ANONYMOUS_USER_ID:
                flash(
                    'Cannot set a local credential while authentication '
                    'is disabled. Switch to a different mode first.',
                    'danger',
                )
                return redirect(url_for('auth.system'))
            if auth_mode.current_mode() != 'local':
                flash('Local credential is only used in local auth mode.', 'warning')
                return redirect(url_for('auth.system'))
            username = (request.form.get('username') or '').strip()
            password = request.form.get('password') or ''
            confirm = request.form.get('password_confirm') or ''
            if password != confirm:
                flash('Passwords do not match.', 'danger')
                return redirect(url_for('auth.system'))
            try:
                auth_mode.save_local_credential(username, password)
            except ValueError as e:
                flash(f'Failed to save credential: {e}', 'danger')
                return redirect(url_for('auth.system'))
            current_app.logger.info(
                'local credential updated for user=%s by %s from %s',
                username, current_user.get_id(), _client_ip(),
            )
            flash('Local credential updated.', 'success')
            return redirect(url_for('auth.system'))
        # Reboot
        if action == 'reboot':
            try:
                subprocess.run(['systemctl', 'reboot'], check=False)
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
            # Paths to clean: WAN/LAN interface, token, org, version cache
            paths = [
                current_app.config.get('WAN_IFACE_PATH'),
                current_app.config.get('LAN_IFACE_PATH'),
                current_app.config.get('BORDER0_TOKEN_PATH'),
                current_app.config.get('BORDER0_ORG_PATH'),
                '/etc/border0/version_cache.json'
            ]
            # Remove files
            for p in paths:
                try:
                    if p and os.path.isfile(p):
                        os.remove(p)
                except Exception as e:
                    errors.append(str(e))
            # Remove device state file
            try:
                token_path = current_app.config.get('BORDER0_TOKEN_PATH')
                state_file = os.path.join(os.path.dirname(token_path or ''), 'device.state.yaml')
                if state_file and os.path.isfile(state_file):
                    os.remove(state_file)
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
            # Restore default network templates
            for file in glob.glob('/opt/border0/defaults/etc/network/interfaces.d/*.conf'):
                shutil.copy(file, f'/etc/network/interfaces.d/{os.path.basename(file)}')
            for file in glob.glob('/opt/border0/defaults/etc/hostapd/*.conf'):
                shutil.copy(file, f'/etc/hostapd/{os.path.basename(file)}')
            # Sync and reboot
            try:
                subprocess.run(['sync'], check=False)
            except Exception:
                pass
            try:
                subprocess.run(['systemctl', 'reboot'], check=False)
            except Exception as e:
                flash(f'Failed to reboot system: {e}', 'danger')

        if action == 'add_ssh_key':
            key_line = request.form.get('ssh_key', '').strip()
            parts = key_line.split(None, 2)
            if len(parts) < 2 or parts[0] not in ('ssh-ed25519', 'ssh-ed25519-sk', 'ssh-rsa'):
                flash('Invalid SSH public key format. Supported types: ed25519, ed25519-sk, rsa.', 'danger')
                return redirect(url_for('auth.system'))
            key_type, key_data = parts[0], parts[1]
            comment = parts[2] if len(parts) > 2 else ''
            if not re.match(r'^[A-Za-z0-9+/=]+$', key_data):
                flash('Invalid SSH public key data.', 'danger')
                return redirect(url_for('auth.system'))
            ssh_dir = os.path.expanduser('~/.ssh')
            auth_file = os.path.join(ssh_dir, 'authorized_keys')
            os.makedirs(ssh_dir, exist_ok=True)
            os.chmod(ssh_dir, 0o700)
            existing = []
            if os.path.exists(auth_file):
                with open(auth_file) as f:
                    existing = [l.strip() for l in f if l.strip()]
            if key_line in existing:
                flash('SSH key already provisioned.', 'info')
                return redirect(url_for('auth.system'))
            try:
                with open(auth_file, 'a') as f:
                    f.write(key_line + '\n')
                os.chmod(auth_file, 0o600)
                flash('SSH key added successfully.', 'success')
            except Exception as e:
                flash(f'Failed to add SSH key: {e}', 'danger')
            return redirect(url_for('auth.system'))

        # Upgrade page - redirect to upgrade workflow in home blueprint
        if action == 'upgrade':
            return redirect(url_for('home.upgrade_page'))
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

    ssh_keys = []
    ssh_dir = os.path.expanduser('~/.ssh')
    auth_file = os.path.join(ssh_dir, 'authorized_keys')
    try:
        with open(auth_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split(None, 2)
                if len(parts) < 2 or parts[0] not in ('ssh-ed25519', 'ssh-ed25519-sk', 'ssh-rsa'):
                    continue
                key_type, key_data = parts[0], parts[1]
                comment = parts[2] if len(parts) > 2 else ''
                try:
                    key_bin = base64.b64decode(key_data)
                    fp_digest = hashlib.sha256(key_bin).digest()
                    fp_b64 = base64.b64encode(fp_digest).decode('ascii').rstrip('=')
                    fingerprint = f'SHA256:{fp_b64}'
                except Exception:
                    fingerprint = ''
                ssh_keys.append({'type': key_type, 'comment': comment, 'fingerprint': fingerprint})
    except Exception:
        ssh_keys = []

    local_cred = auth_mode.load_local_credential() or {}
    return render_template(
        'auth/system.html',
        uptime=uptime_str,
        current_version=current_version,
        update_available=update_available,
        new_version=new_version,
        ssh_keys=ssh_keys,
        auth_mode_current=auth_mode.current_mode(),
        auth_mode_file_value=auth_mode.file_mode(),
        auth_mode_env_value=auth_mode.env_mode(),
        auth_mode_env_locked=auth_mode.is_env_locked(),
        auth_mode_env_var=auth_mode.ENV_VAR,
        auth_mode_anonymous=(current_user.get_id() == ANONYMOUS_USER_ID),
        local_credential_set=auth_mode.has_local_credential(),
        local_credential_username=local_cred.get('username', ''),
    )
