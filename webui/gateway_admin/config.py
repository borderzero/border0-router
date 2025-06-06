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
