import os

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
        os.path.expanduser('~/.border0/org')
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
