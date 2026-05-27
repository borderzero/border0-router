"""Resolve and persist the web-UI authentication mode.

The web UI supports three auth modes:

- ``sso``   : Border0 SSO (default, what the rest of the app was built around)
- ``local`` : Local username/password stored on disk
- ``none``  : Authentication disabled, every route reachable without login

The active mode is resolved as:

    env override (BORDER0_WEBUI_AUTH_MODE)  >  on-disk setting  >  ``sso``

The env override is intended for ops use cases (recovery, ISO-baked
configs) and locks the UI toggle when set.

Module-level cache keeps ``current_mode()`` off the disk on the request
hot path; the cache is keyed on the env value + the auth-mode file's
mtime so any external change is picked up without a webui restart.
"""

import errno
import json
import os
import secrets
import stat
import threading

from werkzeug.security import check_password_hash, generate_password_hash

from .config import Config

VALID_MODES = ('none', 'local', 'sso')
DEFAULT_MODE = 'sso'
ENV_VAR = 'BORDER0_WEBUI_AUTH_MODE'

# The synthetic user id used in mode='none'. Lives here so other
# blueprints (e.g. /vpn token mgmt) can refuse sensitive operations
# from the anonymous identity without circular-importing auth.routes.
ANONYMOUS_USER_ID = 'anonymous@local'

# Pin the hash method so a werkzeug upgrade can't silently weaken it.
# pbkdf2 is available wherever Python's hashlib is; scrypt would also be
# fine but isn't universally compiled in.
PASSWORD_HASH_METHOD = 'pbkdf2:sha256:600000'

# Bump if the on-disk JSON layout for either file changes.
SCHEMA_VERSION = 1

_write_lock = threading.Lock()

# Cache for the resolved mode. Keyed on (env_value, auth_mode_file_mtime_ns).
# Single-process, but the mtime check means external edits to the file
# are reflected on the next request.
_mode_cache_lock = threading.Lock()
_mode_cache = {'key': None, 'value': None}


def _open_no_follow_read(path):
    """open() the path read-only, refusing symlinks.

    Raises FileNotFoundError when missing, OSError when the path is a
    symlink, directory, or otherwise not a regular file.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, f'not a regular file: {path}')
        return os.fdopen(fd, 'r')
    except Exception:
        os.close(fd)
        raise


def _ensure_dir(path, perms=0o755):
    parent = os.path.dirname(path)
    if not parent:
        return
    try:
        os.makedirs(parent, exist_ok=True)
        # makedirs honors umask, not the mode arg, on intermediate dirs
        # that already exist. Set the final perms explicitly.
        os.chmod(parent, perms)
    except OSError:
        # Existing dir we can't chmod is acceptable — don't crash startup.
        pass


def _atomic_write_json(path, payload, perms=0o600):
    """Write ``payload`` as JSON to ``path`` atomically.

    Refuses to follow symlinks at the destination. fsyncs the file and
    the parent directory so the rename survives power-cut on the router.
    """
    _ensure_dir(path)
    tmp = '{}.tmp.{}.{}'.format(path, os.getpid(), secrets.token_hex(4))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp, flags, perms)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Best-effort fsync of the directory so the rename is durable.
    try:
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def _file_mtime_ns(path):
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            return None
        return st.st_mtime_ns
    except OSError:
        return None


def _read_file_mode():
    path = Config.AUTH_MODE_PATH
    try:
        with _open_no_follow_read(path) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None
    mode = (data or {}).get('mode')
    return mode if mode in VALID_MODES else None


def env_mode():
    raw = os.environ.get(ENV_VAR)
    if not raw:
        return None
    mode = raw.strip().lower()
    return mode if mode in VALID_MODES else None


def file_mode():
    """Public accessor for the on-disk mode. Bypasses the request cache.

    Use ``current_mode()`` on the hot path; this is for the System page.
    """
    return _read_file_mode()


def current_mode():
    """Return the resolved auth mode, using an mtime-keyed cache.

    Cheap enough to call from ``before_app_request``: at most one
    ``lstat()`` per request, and one full file read when the file or
    env override changes.
    """
    env = env_mode()
    mtime = _file_mtime_ns(Config.AUTH_MODE_PATH)
    key = (env, mtime)
    with _mode_cache_lock:
        if _mode_cache['key'] == key:
            return _mode_cache['value']
        value = env or _read_file_mode() or DEFAULT_MODE
        _mode_cache['key'] = key
        _mode_cache['value'] = value
        return value


def invalidate_cache():
    """Force the next ``current_mode()`` call to re-read everything."""
    with _mode_cache_lock:
        _mode_cache['key'] = None
        _mode_cache['value'] = None


def is_env_locked():
    return env_mode() is not None


def save_mode(mode):
    if mode not in VALID_MODES:
        raise ValueError(f'invalid auth mode: {mode!r}')
    if is_env_locked():
        raise RuntimeError(
            f'auth mode is locked by {ENV_VAR}; cannot change via UI'
        )
    payload = {'v': SCHEMA_VERSION, 'mode': mode}
    with _write_lock:
        # 0o644: not a secret; readers (border0 CLI tooling, sysadmins) may
        # want to inspect the chosen mode without root.
        _atomic_write_json(Config.AUTH_MODE_PATH, payload, perms=0o644)
    invalidate_cache()


def has_local_credential():
    path = Config.LOCAL_AUTH_PATH
    try:
        st = os.lstat(path)
        return stat.S_ISREG(st.st_mode)
    except OSError:
        return False


def load_local_credential():
    try:
        with _open_no_follow_read(Config.LOCAL_AUTH_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None


def save_local_credential(username, password):
    if not isinstance(username, str) or not username.strip():
        raise ValueError('username is required')
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError('password must be at least 8 characters')
    payload = {
        'v': SCHEMA_VERSION,
        'username': username.strip(),
        'password_hash': generate_password_hash(
            password, method=PASSWORD_HASH_METHOD
        ),
    }
    with _write_lock:
        _atomic_write_json(Config.LOCAL_AUTH_PATH, payload, perms=0o600)


def clear_local_credential():
    path = Config.LOCAL_AUTH_PATH
    try:
        os.remove(path)
    except FileNotFoundError:
        return
    # fsync the parent directory so the unlink is durable across power-cut.
    parent = os.path.dirname(path)
    if not parent:
        return
    try:
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def verify_local_credential(username, password):
    cred = load_local_credential()
    if not cred:
        return False
    stored_user = cred.get('username')
    stored_hash = cred.get('password_hash')
    if not stored_user or not stored_hash:
        return False
    if stored_user != username:
        return False
    try:
        return check_password_hash(stored_hash, password)
    except Exception:
        return False
