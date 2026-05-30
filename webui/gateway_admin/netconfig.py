"""Border0 router network apply-engine.

Single source of truth is /etc/border0/network.json (the frozen model). This
module renders it into ifupdown / hostapd / wpa_supplicant / dnsmasq / iptables
config and (on apply) tears down the old world and brings up the new one.

No Flask. Importable from the UI, and runnable standalone:

    python3 -m gateway_admin.netconfig validate
    python3 -m gateway_admin.netconfig render --write
    python3 -m gateway_admin.netconfig apply [--dry-run]

build_iso.sh uses `render --write` inside the chroot to bake an initial config.
Everything we write carries a `# managed-by: border0-netconfig` marker so
teardown only ever touches our own files — never lo, dummy0, or hand edits.
"""

import os
import sys
import json
import ipaddress
import subprocess

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import netutils

# Default path matches Config.NETWORK_CONFIG_PATH; env override for tests/dev.
NETWORK_CONFIG_PATH = os.environ.get('NETWORK_CONFIG_PATH', '/etc/border0/network.json')

MARKER = '# managed-by: border0-netconfig'

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), 'templates', 'config')

# Where each kind of rendered file lands.
INTERFACES_DIR = '/etc/network/interfaces.d'
HOSTAPD_DIR = '/etc/hostapd'
WPA_DIR = '/etc/wpa_supplicant'
DNSMASQ_DIR = '/etc/dnsmasq.d'
FIREWALL_PATH = '/etc/border0/firewall.sh'

_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=False,
    lstrip_blocks=False,
)


# --------------------------------------------------------------------------- #
# load / save
# --------------------------------------------------------------------------- #

def load(path=NETWORK_CONFIG_PATH):
    """Read the model from disk. Returns {} if absent."""
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save(model, path=NETWORK_CONFIG_PATH):
    """Write the model atomically (tmp + os.replace)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(model, f, indent=2, sort_keys=False)
        f.write('\n')
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _lans(model):
    return model.get('lans') or []


def _wan(model):
    return model.get('wan') or {}


def _wan_ifaces(model):
    return [w.get('iface') for w in _wan(model).get('interfaces', []) if w.get('iface')]


def _wifi(model):
    return model.get('wifi') or {}


def _bridge_members(lan):
    m = lan.get('members') or {}
    return {
        'eth': list(m.get('eth') or []),
        'wifi_ap': list(m.get('wifi_ap') or []),
    }


def _netmask_broadcast(subnet):
    """Return (netmask, broadcast) dotted-quad for a CIDR subnet."""
    net = ipaddress.ip_network(subnet, strict=False)
    return str(net.netmask), str(net.broadcast_address)


def _iface_phy(iface):
    """Map a wlan iface to its phy id via sysfs, or None if unavailable.

    The link resolves to .../ieee80211/phyN; we want phyN. If the link doesn't
    exist (not on real hardware, e.g. dev box) bail out so the caller skips the
    same-radio warning rather than fabricating a bogus shared phy.
    """
    link = '/sys/class/net/%s/phy80211' % iface
    if not os.path.islink(link):
        return None
    target = os.path.realpath(link)
    if not os.path.isdir(target):
        return None
    return os.path.basename(target)


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #

def validate(model):
    """Return a list of human-readable problems. Empty list == good to apply."""
    errs = []
    lans = _lans(model)
    wan = _wan(model)
    wifi = _wifi(model)

    # --- every iface claimed at most once (bridge member XOR wan) ---
    claims = {}  # iface -> location string
    def claim(iface, where):
        if iface in claims:
            errs.append("interface %s claimed by both %s and %s" % (iface, claims[iface], where))
        else:
            claims[iface] = where

    for lan in lans:
        mem = _bridge_members(lan)
        for e in mem['eth']:
            claim(e, "bridge %s" % lan.get('name'))
        for w in mem['wifi_ap']:
            claim(w, "bridge %s" % lan.get('name'))
    for w in wan.get('interfaces', []):
        if w.get('iface'):
            claim(w['iface'], "wan")

    # --- wifi role vs placement ---
    for lan in lans:
        for w in _bridge_members(lan)['wifi_ap']:
            cfg = wifi.get(w) or {}
            if cfg.get('mode') != 'ap':
                errs.append("wlan %s is in bridge %s members.wifi_ap but wifi mode is %r (expected 'ap')"
                            % (w, lan.get('name'), cfg.get('mode')))

    bridge_wlans = {w for lan in lans for w in _bridge_members(lan)['wifi_ap']}
    wan_ifaces = set(_wan_ifaces(model))
    for wlan, cfg in wifi.items():
        if cfg.get('mode') == 'client':
            if wlan in bridge_wlans:
                errs.append("wlan %s is a wifi client but also a bridge member" % wlan)
            if wlan not in wan_ifaces:
                errs.append("wlan %s is a wifi client but not listed in wan.interfaces" % wlan)

    # --- wan.active sanity ---
    active = wan.get('active')
    if wan.get('interfaces'):
        if active is not None and active not in wan_ifaces:
            errs.append("wan.active %r is not one of the wan interfaces" % active)
    elif active not in (None,):
        errs.append("wan.active is set but there are no wan interfaces")

    # --- subnets: RFC1918, non-overlapping ---
    nets = []
    for lan in lans:
        sub = lan.get('subnet')
        try:
            net = ipaddress.ip_network(sub, strict=False)
        except Exception as e:
            errs.append("lan %s has invalid subnet %r: %s" % (lan.get('name'), sub, e))
            continue
        if not net.is_private:
            errs.append("lan %s subnet %s is not RFC1918" % (lan.get('name'), sub))
        for other_name, other in nets:
            if net.overlaps(other):
                errs.append("lan %s subnet %s overlaps lan %s subnet %s"
                            % (lan.get('name'), net, other_name, other))
        nets.append((lan.get('name'), net))

    # --- band / channel ---
    for wlan, cfg in wifi.items():
        if cfg.get('mode') != 'ap':
            continue
        band = cfg.get('band')
        if band not in ('g', 'a'):
            errs.append("wlan %s has invalid band %r (expected 'g' or 'a')" % (wlan, band))
            continue
        chan = str(cfg.get('channel', ''))
        if chan not in netutils._allowed_channels(band):
            errs.append("wlan %s channel %r invalid for band %s (allowed: %s)"
                        % (wlan, chan, band, ','.join(netutils._allowed_channels(band))))

    # --- WARN: AP + client on the same radio (best effort) ---
    phy_roles = {}  # phy -> set of modes
    for wlan, cfg in wifi.items():
        phy = _iface_phy(wlan)
        if phy is None:
            continue
        phy_roles.setdefault(phy, set()).add((wlan, cfg.get('mode')))
    for phy, members in phy_roles.items():
        modes = {m for _, m in members}
        if 'ap' in modes and 'client' in modes:
            names = ', '.join(sorted(w for w, _ in members))
            errs.append("WARN: %s have AP and client on the same radio (%s) — unreliable"
                        % (names, phy))

    return errs


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #

def _render(template, **ctx):
    return _env.get_template(template).render(**ctx)


def render_all(model):
    """Render every managed file. Returns {abs_path: contents}. Writes nothing."""
    out = {}
    lans = _lans(model)
    wan = _wan(model)
    wifi = _wifi(model)
    active = wan.get('active')

    lan_subnets = [lan['subnet'] for lan in lans if lan.get('subnet')]
    wan_ifaces = _wan_ifaces(model)

    # Map an AP wlan to the bridge it belongs to.
    wlan_bridge = {}
    for lan in lans:
        for w in _bridge_members(lan)['wifi_ap']:
            wlan_bridge[w] = lan['name']

    # --- bridges + eth members + per-bridge dnsmasq ---
    for lan in lans:
        name = lan['name']
        mem = _bridge_members(lan)
        netmask, broadcast = _netmask_broadcast(lan['subnet'])
        out['%s/%s.conf' % (INTERFACES_DIR, name)] = _render(
            'interfaces-bridge.conf.j2',
            name=name,
            eth_members=mem['eth'],
            stp=bool(lan.get('stp')),
            gateway=lan['gateway'],
            netmask=netmask,
            broadcast=broadcast,
        )
        for e in mem['eth']:
            out['%s/%s.conf' % (INTERFACES_DIR, e)] = _render(
                'interfaces-bridge-member.conf.j2', iface=e)

        dhcp = lan.get('dhcp') or {}
        if dhcp.get('enabled'):
            out['%s/border0-%s.conf' % (DNSMASQ_DIR, name)] = _render(
                'dnsmasq-lan.conf.j2',
                lan=name,
                start=dhcp['start'],
                end=dhcp['end'],
                lease=dhcp.get('lease', '4h'),
                gateway=lan['gateway'],
                dns_upstream=lan.get('dns_upstream') or [],
            )

    # --- AP hostapd ---
    for wlan, cfg in wifi.items():
        if cfg.get('mode') != 'ap':
            continue
        band = cfg.get('band', 'g')
        chan = str(cfg.get('channel', ''))
        vht_chwidth = 1 if band == 'a' else 0
        out['%s/%s.conf' % (HOSTAPD_DIR, wlan)] = _render(
            'hostapd.conf.j2',
            iface=wlan,
            brname=wlan_bridge.get(wlan),
            ssid=cfg.get('ssid', ''),
            hw_mode=band,
            channel=chan,
            wpa_passphrase=cfg.get('psk', ''),
            vht_chwidth=vht_chwidth,
            vht_seg0=netutils.VHT80_SEG0.get(chan, ''),
        )

    # --- client wpa_supplicant ---
    for wlan, cfg in wifi.items():
        if cfg.get('mode') != 'client':
            continue
        client = cfg.get('client') or {}
        out['%s/wpa_supplicant-%s.conf' % (WPA_DIR, wlan)] = _render(
            'wpa_supplicant-client.conf.j2',
            ssid=client.get('ssid', ''),
            psk=client.get('psk', ''),
        )

    # --- wan interfaces ---
    client_wlans = {w for w, c in wifi.items() if c.get('mode') == 'client'}
    for w in wan.get('interfaces', []):
        iface = w.get('iface')
        if not iface:
            continue
        is_active = (iface == active)
        if w.get('mode') == 'static':
            out['%s/%s.conf' % (INTERFACES_DIR, iface)] = _render(
                'interfaces-wan.conf.j2',
                iface=iface,
                active=is_active,
                mode='static',
                address=w.get('address', ''),
                netmask=w.get('netmask', ''),
                gateway=w.get('gateway', ''),
                dns=w.get('dns', ''),
                is_wlan_client=False,
            )
        else:
            out['%s/%s.conf' % (INTERFACES_DIR, iface)] = _render(
                'interfaces-wan.conf.j2',
                iface=iface,
                active=is_active,
                mode='dhcp',
                address='', netmask='', gateway='', dns='',
                is_wlan_client=(iface in client_wlans),
            )

    # --- firewall ---
    out[FIREWALL_PATH] = _render(
        'firewall.sh.j2',
        active_wan=active,
        lan_subnets=lan_subnets,
        wan_ifaces=wan_ifaces,
    )

    return out


# --------------------------------------------------------------------------- #
# subprocess helper
# --------------------------------------------------------------------------- #

def _run(cmd, check=False, timeout=30):
    """Run a command best-effort. Never raises; returns a result dict."""
    rec = {'cmd': cmd, 'rc': None, 'stdout': '', 'stderr': ''}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rec['rc'] = p.returncode
        rec['stdout'] = (p.stdout or '').strip()
        rec['stderr'] = (p.stderr or '').strip()
    except Exception as e:  # FileNotFoundError, TimeoutExpired, ...
        rec['rc'] = -1
        rec['stderr'] = str(e)
    return rec


def sctl(*args):
    return _run(['systemctl', *args])


# --------------------------------------------------------------------------- #
# file I/O for managed files
# --------------------------------------------------------------------------- #

def _is_managed(path):
    """True if the first line of the file bears our marker."""
    try:
        with open(path) as f:
            first = f.readline()
        return MARKER in first
    except Exception:
        return False


def _write_file(path, contents, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(contents)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _managed_files_on_disk():
    """Every file we manage that currently exists on disk."""
    found = []
    for d in (INTERFACES_DIR, HOSTAPD_DIR, WPA_DIR, DNSMASQ_DIR):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p) and _is_managed(p):
                found.append(p)
    if os.path.isfile(FIREWALL_PATH) and _is_managed(FIREWALL_PATH):
        found.append(FIREWALL_PATH)
    return found


def _live_bridges():
    """Bridge interfaces currently up, via `ip -j link`. Best effort."""
    res = _run(['ip', '-j', 'link', 'show', 'type', 'bridge'])
    if res['rc'] != 0 or not res['stdout']:
        return []
    try:
        return [x['ifname'] for x in json.loads(res['stdout'])]
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# teardown
# --------------------------------------------------------------------------- #

def teardown(prev_model):
    """Stop services + ifdown for everything the previous model brought up.

    Best effort: never raises. Does not delete files (apply() reconciles files
    separately so it can tell removed-from-model apart from rewritten).
    """
    actions = []
    prev = prev_model or {}

    for lan in _lans(prev):
        name = lan['name']
        actions.append(sctl('stop', 'border0-dnsmasq@%s.service' % name))
        actions.append(_run(['ifdown', name]))

    for wlan, cfg in _wifi(prev).items():
        if cfg.get('mode') == 'ap':
            actions.append(sctl('stop', 'hostapd@%s' % wlan))

    for w in _wan(prev).get('interfaces', []):
        if w.get('iface'):
            actions.append(_run(['ifdown', w['iface']]))

    return actions


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #

def apply(model, dry_run=False):
    """Reconcile the live system to `model`. Returns a dict of what happened."""
    result = {'errors': [], 'planned': [], 'actions': [], 'dry_run': dry_run}

    errs = validate(model)
    # Treat WARN lines as advisory, real errors as fatal.
    fatal = [e for e in errs if not e.startswith('WARN')]
    result['errors'] = errs
    if fatal:
        return result

    prev = load()
    rendered = render_all(model)

    new_lans = {lan['name'] for lan in _lans(model)}
    new_ap_wlans = {w for w, c in _wifi(model).items() if c.get('mode') == 'ap'}
    new_wan = set(_wan_ifaces(model))
    keep_files = set(rendered.keys())

    # files we manage that are no longer in the new model
    stale = [p for p in _managed_files_on_disk() if p not in keep_files]

    # bridges that existed before but aren't in the new model
    prev_lans = {lan['name'] for lan in _lans(prev)}
    removed_bridges = [b for b in (prev_lans & set(_live_bridges())) if b not in new_lans]

    if dry_run:
        result['planned'].append({'teardown_prev': prev})
        result['planned'].append({'remove_bridges': removed_bridges})
        result['planned'].append({'remove_files': stale})
        result['planned'].append({'write_files': sorted(keep_files)})
        result['planned'].append({'enable_dnsmasq': sorted(new_lans)})
        result['planned'].append({'enable_hostapd': sorted(new_ap_wlans)})
        result['planned'].append({'ifup_active_wan': _wan(model).get('active')})
        result['planned'].append({'ifup_wan': sorted(new_wan)})
        return result

    # 1) teardown previous world
    result['actions'].extend(teardown(prev))

    # 2) delete removed bridges + stale managed files
    for b in removed_bridges:
        result['actions'].append(_run(['ip', 'link', 'del', b]))
    for p in stale:
        try:
            os.remove(p)
            result['actions'].append({'cmd': ['rm', p], 'rc': 0})
        except Exception as e:
            result['actions'].append({'cmd': ['rm', p], 'rc': -1, 'stderr': str(e)})

    # 3) write all new files atomically (firewall.sh executable)
    for path, contents in rendered.items():
        mode = 0o755 if path == FIREWALL_PATH else 0o644
        _write_file(path, contents, mode=mode)

    # 4) reload units (the dnsmasq template instance may have changed)
    result['actions'].append(sctl('daemon-reload'))

    # 5) bring up bridges + per-bridge dnsmasq
    for lan in _lans(model):
        name = lan['name']
        result['actions'].append(sctl('enable', 'border0-dnsmasq@%s' % name))
        result['actions'].append(_run(['ifup', name]))

    # 6) start AP wlans
    for wlan in sorted(new_ap_wlans):
        result['actions'].append(sctl('enable', '--now', 'hostapd@%s' % wlan))

    # 7) uplinks: active first, then any other auto wan
    active = _wan(model).get('active')
    if active:
        result['actions'].append(_run(['ifup', active]))
    for w in _wan_ifaces(model):
        if w != active:
            result['actions'].append(_run(['ifup', w]))

    # 8) firewall once more, now that everything's up
    result['actions'].append(_run([FIREWALL_PATH, 'apply']))

    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _main(argv):
    if not argv:
        print('usage: python3 -m gateway_admin.netconfig {render [--write]|apply [--dry-run]|validate}',
              file=sys.stderr)
        return 2

    cmd = argv[0]

    if cmd == 'validate':
        errs = validate(load())
        for e in errs:
            print(e)
        return 1 if any(not e.startswith('WARN') for e in errs) else 0

    if cmd == 'render':
        model = load()
        files = render_all(model)
        if '--write' in argv[1:]:
            for path, contents in files.items():
                mode = 0o755 if path == FIREWALL_PATH else 0o644
                _write_file(path, contents, mode=mode)
                print('wrote %s' % path)
        else:
            for path, contents in files.items():
                print('# === %s ===' % path)
                print(contents)
        return 0

    if cmd == 'apply':
        res = apply(load(), dry_run=('--dry-run' in argv[1:]))
        print(json.dumps(res, indent=2))
        return 1 if [e for e in res['errors'] if not e.startswith('WARN')] else 0

    print('unknown command: %s' % cmd, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
