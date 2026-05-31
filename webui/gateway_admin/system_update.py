"""In-place upgrade of the border0-router software layer.

Pulls a tagged release straight from the public GitHub repo (no token), swaps the
files under /opt/border0, reinstalls deps, and restarts border0-webui. Because the
web UI restarts *itself*, the actual work runs detached (the route spawns
``python3 -m gateway_admin.system_update run <tag>`` in its own session) and reports
progress through a status file that a polling page reads — an SSE stream wouldn't
survive its own server going down.

Flask-free on purpose: importable by the route AND runnable as a module.
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

from . import image_version

REPO = 'borderzero/border0-router'
API_LATEST = 'https://api.github.com/repos/%s/releases/latest' % REPO
TARBALL = 'https://github.com/%s/archive/refs/tags/%s.tar.gz'

INSTALL_ROOT = '/opt/border0'           # webui/, templates/ live here
WEBUI_DIR = INSTALL_ROOT + '/webui'
TEMPLATES_DIR = INSTALL_ROOT + '/templates'
SYSTEMD_DIR = '/etc/systemd/system'
WORK_DIR = '/var/tmp/border0-update'    # NOT /tmp — that's tmpfs/RAM on the Pi
STATUS_PATH = '/run/border0-sysupdate.json'
HEALTH_URL = 'http://127.0.0.1/'        # any HTTP response = webui serving again

_UA = {'User-Agent': 'border0-router-updater'}


# --------------------------------------------------------------------------- #
# version / check
# --------------------------------------------------------------------------- #

def current_version():
    mf = image_version.read() or {}
    return mf.get('version') or 'dev'


def _semver(tag):
    """('v2.0.1' | 'v2.0.0-6-gabc') -> (2,0,1). None if not vX.Y.Z-ish."""
    if not tag:
        return None
    t = tag.lstrip('v').split('-')[0]
    parts = t.split('.')
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def latest_release(timeout=8):
    """Newest published release tag, or None (best-effort, anonymous)."""
    try:
        req = urllib.request.Request(API_LATEST, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get('tag_name')
    except Exception:
        return None


def check():
    cur, latest = current_version(), latest_release()
    cur_v, latest_v = _semver(cur), _semver(latest)
    if not latest_v:
        available = False               # couldn't reach GitHub / no release
    elif not cur_v:
        available = True                # dev/unknown build -> offer the real release
    else:
        available = latest_v > cur_v
    return {'current': cur, 'latest': latest, 'update_available': available}


# --------------------------------------------------------------------------- #
# status file
# --------------------------------------------------------------------------- #

def read_status():
    try:
        with open(STATUS_PATH) as f:
            return json.load(f)
    except Exception:
        return {'state': 'idle'}


def _set_status(state, pct, msg, **extra):
    data = {'state': state, 'pct': pct, 'msg': msg, 'ts': time.time()}
    data.update(extra)
    try:
        tmp = STATUS_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, STATUS_PATH)
    except Exception:
        pass
    return data


# --------------------------------------------------------------------------- #
# worker
# --------------------------------------------------------------------------- #

def _run(cmd, timeout=600):
    """Best-effort command; returns (rc, output). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or '') + (p.stderr or '')
    except Exception as e:
        return -1, str(e)


def _download(tag, dest):
    url = TARBALL % (REPO, tag)
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)


def _extract_src(tarball, into):
    """Untar and return the path of the single top-level border0-router-* dir."""
    with tarfile.open(tarball) as tf:
        tf.extractall(into)
    for name in os.listdir(into):
        p = os.path.join(into, name)
        if os.path.isdir(p) and name.startswith('border0-router-'):
            return p
    raise RuntimeError('unexpected tarball layout')


def _unit_files(build_templates):
    out = []
    if os.path.isdir(build_templates):
        for n in os.listdir(build_templates):
            if n.startswith('border0-') and n.endswith('.service'):
                out.append(n)
    return out


def _backup(path):
    """tar up the current webui + our unit files for rollback. Returns the path."""
    os.makedirs(WORK_DIR, exist_ok=True)
    with tarfile.open(path, 'w:gz') as tf:
        if os.path.isdir(WEBUI_DIR):
            # exclude the venv — big, and setup.sh rebuilds it
            tf.add(WEBUI_DIR, arcname='webui',
                   filter=lambda ti: None if '/venv/' in ti.name + '/' or ti.name.endswith('/venv') else ti)
        for n in _unit_files(SYSTEMD_DIR):
            tf.add(os.path.join(SYSTEMD_DIR, n), arcname='units/' + n)
    return path


def _install(src_root):
    """Overlay the new webui + units. Keeps the existing venv (not in the tarball)."""
    src_webui = os.path.join(src_root, 'webui')
    shutil.copytree(src_webui, WEBUI_DIR, dirs_exist_ok=True)
    src_build_templates = os.path.join(src_root, 'build', 'templates')
    for n in _unit_files(src_build_templates):
        shutil.copy(os.path.join(src_build_templates, n), os.path.join(SYSTEMD_DIR, n))
    if os.path.isdir(src_build_templates):
        shutil.copytree(src_build_templates, TEMPLATES_DIR, dirs_exist_ok=True)


def _restore(backup):
    """Roll back: extract the backup over webui + units."""
    with tarfile.open(backup) as tf:
        members = tf.getmembers()
        for m in members:
            if m.name.startswith('webui/'):
                m.name = m.name[len('webui/'):]
                if m.name:
                    tf.extract(m, WEBUI_DIR)
        # units
    with tarfile.open(backup) as tf:
        for m in tf.getmembers():
            if m.name.startswith('units/'):
                m.name = m.name[len('units/'):]
                if m.name:
                    tf.extract(m, SYSTEMD_DIR)


def _webui_healthy(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=3) as r:
                if r.status:
                    return True
        except urllib.error.HTTPError:
            return True             # any HTTP status = it's serving (e.g. 302/login)
        except Exception:
            time.sleep(2)           # not up yet (restarting) — keep waiting
    return False


def run(tag):
    """The detached worker. Writes progress to STATUS_PATH; rolls back on failure."""
    cur = read_status()
    if cur.get('state') in ('downloading', 'installing', 'restarting', 'verifying'):
        return  # an upgrade is already running

    shutil.rmtree(WORK_DIR, ignore_errors=True)
    os.makedirs(WORK_DIR, exist_ok=True)
    tarball = os.path.join(WORK_DIR, 'src.tgz')
    backup = os.path.join(WORK_DIR, 'backup.tgz')

    try:
        _set_status('downloading', 10, 'Downloading %s…' % tag, tag=tag)
        _download(tag, tarball)

        _set_status('extracting', 25, 'Extracting…', tag=tag)
        src_root = _extract_src(tarball, WORK_DIR)

        _set_status('backup', 35, 'Backing up current install…', tag=tag)
        _backup(backup)

        _set_status('installing', 50, 'Installing new files…', tag=tag)
        _install(src_root)

        _set_status('deps', 65, 'Installing dependencies (this can take a minute)…', tag=tag)
        rc, out = _run(['bash', './setup.sh'], timeout=600)
        if rc != 0:
            raise RuntimeError('setup.sh failed: %s' % out[-500:])

        _set_status('reload', 80, 'Reloading services…', tag=tag)
        _run(['systemctl', 'daemon-reload'])

        _set_status('restarting', 88, 'Restarting web UI…', tag=tag)
        # Detached from the webui: this kills the old server but not us.
        _run(['systemctl', 'restart', 'border0-webui'], timeout=60)

        _set_status('verifying', 94, 'Waiting for web UI to come back…', tag=tag)
        if not _webui_healthy(90):
            raise RuntimeError('web UI did not come back after restart')

        _set_status('done', 100, 'Updated to %s.' % tag, tag=tag)
    except Exception as e:
        try:
            if os.path.isfile(backup):
                _set_status('rollback', 0, 'Update failed, rolling back…', tag=tag, error=str(e))
                _restore(backup)
                _run(['systemctl', 'daemon-reload'])
                _run(['systemctl', 'restart', 'border0-webui'], timeout=60)
                _webui_healthy(90)
                _set_status('failed', 0, 'Update failed; rolled back to previous version.',
                            tag=tag, error=str(e))
            else:
                _set_status('failed', 0, 'Update failed before any change was made.',
                            tag=tag, error=str(e))
        except Exception as e2:
            _set_status('failed', 0, 'Update failed AND rollback failed — check logs.',
                        tag=tag, error='%s | rollback: %s' % (e, e2))
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == 'check':
        print(json.dumps(check()))
    elif len(sys.argv) >= 3 and sys.argv[1] == 'run':
        run(sys.argv[2])
    else:
        print('usage: python3 -m gateway_admin.system_update {check | run <tag>}',
              file=sys.stderr)
        sys.exit(2)
