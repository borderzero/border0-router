"""WAN zone admin page.

WAN is a logical zone, not a bridge: a set of candidate uplink interfaces of
which exactly ONE (wan.active) carries the live default route; the rest are
defined but stay down. We manage the eth* candidates here. Client-mode wlans
land in wan.interfaces courtesy of the WiFi page — we display and let them be
chosen as active, but we never create or mutate them.

All persistence goes through netconfig: load() the model, rebuild cfg['wan']
from the form, validate(), then apply() (which applies AND saves). Never save()
before apply() or teardown can't see the delta.
"""

import ipaddress

from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask_login import login_required

from ... import netconfig
from ... import netutils

wan_bp = Blueprint('wan', __name__, url_prefix='/wan')

# Static-field defaults for a fresh row. Nothing clever — just so the inputs
# aren't blank when an admin flips an iface to static for the first time.
DEFAULT_STATIC = {
    'address': '',
    'netmask': '255.255.255.0',
    'gateway': '',
    'dns': '8.8.8.8 1.1.1.1',
}


def _client_wlans(model):
    """wlan ifaces the WiFi page runs in client mode — ours to display, not edit."""
    wifi = model.get('wifi') or {}
    return [w for w, c in wifi.items() if (c or {}).get('mode') == 'client']


def _claimed_lan_eth(model):
    """eth ifaces already swallowed by a LAN bridge — keep them out of WAN."""
    claimed = set()
    for lan in model.get('lans') or []:
        members = (lan.get('members') or {}).get('eth') or []
        claimed.update(members)
    return claimed


def _wan_entry_map(model):
    """iface -> its existing wan.interfaces entry, for prefilling the form."""
    wan = model.get('wan') or {}
    return {e['iface']: e for e in wan.get('interfaces', []) if e.get('iface')}


def _valid_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except Exception:
        return False


@wan_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    model = netconfig.load()

    if request.method == 'POST':
        return _save(model)

    # --- GET: build the candidate rows ---
    wan = model.get('wan') or {}
    active = wan.get('active')
    entries = _wan_entry_map(model)
    claimed = _claimed_lan_eth(model)
    client_wlans = set(_client_wlans(model))

    # eth* candidates that aren't pinned to a LAN bridge, plus any client wlans
    # already parked in the zone (the WiFi page owns those).
    candidates = [i for i in netutils.list_interfaces(('eth',)) if i not in claimed]
    candidates += [w for w in client_wlans if w in entries]

    rows = []
    for iface in candidates:
        entry = entries.get(iface, {})
        is_client = iface in client_wlans
        mode = entry.get('mode', 'dhcp')
        rows.append({
            'iface': iface,
            'in_zone': iface in entries,
            'is_client': is_client,
            'mode': mode,
            'address': entry.get('address', DEFAULT_STATIC['address']),
            'netmask': entry.get('netmask', DEFAULT_STATIC['netmask']),
            'gateway': entry.get('gateway', DEFAULT_STATIC['gateway']),
            'dns': entry.get('dns', DEFAULT_STATIC['dns']),
            'status': netutils.iface_status(iface),
        })

    return render_template('wan/index.html', rows=rows, active=active)


def _save(old_model):
    """Rebuild cfg['wan'] from the form on a fresh load, validate, apply (PRG)."""
    cfg = netconfig.load()
    cfg.setdefault('wan', {})

    client_wlans = set(_client_wlans(cfg))
    claimed = _claimed_lan_eth(cfg)
    old_entries = _wan_entry_map(cfg)

    # Preserve client-wlan entries verbatim — the WiFi page owns those rows; we
    # must not drop them just because this form doesn't carry their static guts.
    interfaces = [old_entries[w] for w in client_wlans if w in old_entries]

    # Collect the checked eth ifaces with their mode/static fields.
    for iface in request.form.getlist('in_zone'):
        if iface in client_wlans or iface in claimed:
            continue  # not ours to write here
        mode = request.form.get(f'mode_{iface}', 'dhcp')
        if mode not in ('dhcp', 'static'):
            flash(f'{iface}: invalid mode {mode!r}', 'danger')
            return redirect(url_for('wan.index'))
        entry = {'iface': iface, 'mode': mode}
        if mode == 'static':
            entry['address'] = request.form.get(f'address_{iface}', '').strip()
            entry['netmask'] = request.form.get(f'netmask_{iface}', '').strip()
            entry['gateway'] = request.form.get(f'gateway_{iface}', '').strip()
            entry['dns'] = request.form.get(f'dns_{iface}', '').strip()
            # Cheap field validation; netconfig.validate handles cross-checks.
            for label, val in (('address', entry['address']),
                               ('gateway', entry['gateway'])):
                if not _valid_ip(val):
                    flash(f'{iface}: invalid {label} {val!r}', 'danger')
                    return redirect(url_for('wan.index'))
            if not _valid_ip(entry['netmask']):
                flash(f'{iface}: invalid netmask {entry["netmask"]!r}', 'danger')
                return redirect(url_for('wan.index'))
        interfaces.append(entry)

    active = request.form.get('active') or None
    cfg['wan']['interfaces'] = interfaces
    cfg['wan']['active'] = active

    zone_ifaces = [e['iface'] for e in interfaces]
    if active is not None and active not in zone_ifaces:
        flash(f'Active uplink {active!r} is not one of the WAN interfaces', 'danger')
        return redirect(url_for('wan.index'))

    # Lockout guard: if the admin reached us over the WAN zone and this change
    # moves the active uplink, they're sawing off the branch they sit on. Make
    # them tick the box first.
    if netconfig.ingress_zone(request.remote_addr, old_model) == 'wan':
        old_active = (old_model.get('wan') or {}).get('active')
        if active != old_active and not request.form.get('confirm_disruptive'):
            flash('Changing the active uplink may drop your connection (you are '
                  'connected via WAN). Re-submit with "I understand" checked to '
                  'proceed.', 'warning')
            return redirect(url_for('wan.index'))

    errs = netconfig.validate(cfg)
    fatal = [e for e in errs if not e.startswith('WARN')]
    if fatal:
        for e in fatal:
            flash(e, 'danger')
        return redirect(url_for('wan.index'))

    result = netconfig.apply(cfg)
    apply_fatal = [e for e in result.get('errors', []) if not e.startswith('WARN')]
    if apply_fatal:
        for e in apply_fatal:
            flash(e, 'danger')
    else:
        for w in (e for e in result.get('errors', []) if e.startswith('WARN')):
            flash(w, 'warning')
        flash('WAN configuration applied', 'success')
    return redirect(url_for('wan.index'))
