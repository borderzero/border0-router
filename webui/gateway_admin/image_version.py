"""Read the baked-in image build manifest.

The manifest is written into the image at ISO-build time by
``build/build_iso.sh`` (see ``Config.IMAGE_VERSION_PATH``). It is absent on
dev boxes provisioned by ``rsync`` rather than a real image build, so every
accessor degrades gracefully to ``None``/``'dev'`` instead of raising.
"""

import json

from .config import Config

_FIELDS = ('version', 'git_commit', 'git_branch', 'base_image', 'built_at')


def read():
    """Return the manifest dict, or ``None`` if it isn't present/parseable."""
    try:
        with open(Config.IMAGE_VERSION_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    # Normalize to the known fields so the template can rely on them existing.
    return {k: data.get(k) for k in _FIELDS}
