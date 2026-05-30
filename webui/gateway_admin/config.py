import os

import datetime
import secrets

# Ensure SECRET_KEY exists and persist it in /etc/sysconfig/border0-webui on first run
_ENV_FILE = '/etc/sysconfig/border0-webui'
def _ensure_secret_key():
    # If already in environment, skip
    if os.environ.get('SECRET_KEY'):
        return
    key = None
    # Try to load existing key from file
    if os.path.isfile(_ENV_FILE):
        try:
            with open(_ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('export '):
                        line = line[len('export '):]
                    parts = line.split('=', 1)
                    if len(parts) != 2:
                        continue
                    k, v = parts
                    if k.strip() == 'SECRET_KEY':
                        key = v.strip().strip('"').strip("'")
                        break
        except Exception:
            key = None
    # Generate and save if not found
    if not key:
        key = secrets.token_urlsafe(32)
        try:
            os.makedirs(os.path.dirname(_ENV_FILE), exist_ok=True)
            with open(_ENV_FILE, 'w') as f:
                f.write(f'SECRET_KEY="{key}"\n')
        except Exception:
            pass
    # Export for Flask
    os.environ.setdefault('SECRET_KEY', key)

_ensure_secret_key()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'change-me')
    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'password')
    # Path to Border0 CLI binary
    BORDER0_CLI_PATH = os.environ.get('BORDER0_CLI_PATH', 'border0')
    # Optional organization name override via environment
    BORDER0_ORG = os.environ.get('BORDER0_ORG', '')
    # Path where the Border0 client token will be stored for web UI operations
    BORDER0_TOKEN_PATH = os.environ.get(
        'BORDER0_TOKEN_PATH',
        os.path.expanduser('~/.border0/client_token')
    )
    # Path where the organization name will be stored if set via web UI
    BORDER0_ORG_PATH = os.environ.get(
        'BORDER0_ORG_PATH',
        os.path.expanduser('/etc/border0/org')
    )
    # Path where decoded JWT token metadata will be stored for UI display
    BORDER0_TOKEN_METADATA_PATH = os.environ.get(
        'BORDER0_TOKEN_METADATA_PATH',
        os.path.expanduser('/etc/border0/token_metadata.json')
    )
    # Path where the web UI auth-mode toggle is persisted. JSON: {"mode": ...}
    # where mode is one of 'none' | 'local' | 'sso'. Env override below wins.
    AUTH_MODE_PATH = os.environ.get(
        'AUTH_MODE_PATH',
        '/etc/border0/auth_mode.json'
    )
    # Path where the hashed local-mode credential lives.
    LOCAL_AUTH_PATH = os.environ.get(
        'LOCAL_AUTH_PATH',
        '/etc/border0/local_auth.json'
    )
    # Declarative network model (bridges, WAN zone, per-radio wifi). Single source
    # of truth for the apply engine; the UI writes this and nothing else under /etc.
    NETWORK_CONFIG_PATH = os.environ.get(
        'NETWORK_CONFIG_PATH',
        '/etc/border0/network.json'
    )
    # Path to the image build manifest baked in at ISO-build time by
    # build/build_iso.sh. JSON: {"version", "git_commit", "git_branch",
    # "base_image", "built_at"}. Absent on dev/rsync deploys.
    IMAGE_VERSION_PATH = os.environ.get(
        'IMAGE_VERSION_PATH',
        '/etc/border0/image_version.json'
    )
    # Path where historical metrics are logged (JSON lines, timestamp in ms)
    METRICS_LOG_PATH = os.environ.get(
        'METRICS_LOG_PATH',
        '/var/lib/border0/metrics.log'
    )
    # Path where the chosen WAN interface will be stored
    WAN_IFACE_PATH = os.environ.get(
        'WAN_IFACE_PATH',
        '/etc/border0/wan_interface'
    )
    # Path where the chosen LAN interface will be stored
    LAN_IFACE_PATH = os.environ.get(
        'LAN_IFACE_PATH',
        '/etc/border0/lan_interface'
    )
    # Make sessions permanent so they expire after a fixed duration
    SESSION_PERMANENT = True
    # Limit session lifetime to 1 hour
    PERMANENT_SESSION_LIFETIME = datetime.timedelta(hours=1)
