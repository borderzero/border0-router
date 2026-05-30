import ipaddress

from flask import Blueprint, render_template, request, flash, redirect, url_for
from flask_login import login_required

from ... import netconfig, netutils

lan_bp = Blueprint('lan', __name__, url_prefix='/lan')

DEFAULT_SUBNET = '192.168.42.0/24'
DEFAULT_DHCP = {'enabled': True, 'start_host': 10, 'end_host': 250, 'lease': '4h'}


def _eth_ifaces():
    """Physical eth* names. Empty on a dev box without the hardware."""
    return netutils.list_interfaces(('eth',))


def _claimed_eth(model, skip_lan=None):
    """eth ifaces already spoken for — by another bridge or by WAN.

    skip_lan lets the bridge being edited keep its own members in the offer.
    """
    taken = set()
    for lan in model.get('lans') or []:
        if lan.get('name') == skip_lan:
            continue
        for e in (lan.get('members') or {}).get('eth') or []:
            taken.add(e)
    for w in (model.get('wan') or {}).get('interfaces') or []:
        if w.get('iface'):
            taken.add(w['iface'])
    return taken


def _suggest_name(model):
    """Next free lanN. We don't reuse gaps — monotonic is less surprising."""
    used = {lan.get('name') for lan in model.get('lans') or []}
    n = 0
    while f'lan{n}' in used:
        n += 1
    return f'lan{n}'


def _flash_apply_result(res):
    """Flash whatever apply() coughed up: WARN as warning, the rest as danger."""
    for e in res.get('errors') or []:
        flash(e, 'warning' if e.startswith('WARN') else 'danger')


def _bridge_view(lan, model):
    """Shape one stored bridge into what the template card needs."""
    dhcp = lan.get('dhcp') or {}
    name = lan.get('name')
    return {
        'name': name,
        'subnet': lan.get('subnet', ''),
        'dhcp_enabled': bool(dhcp.get('enabled')),
        'dhcp_start': dhcp.get('start', ''),
        'dhcp_end': dhcp.get('end', ''),
        'dhcp_lease': dhcp.get('lease', '4h'),
        'eth_members': list((lan.get('members') or {}).get('eth') or []),
        'wifi_aps': list((lan.get('members') or {}).get('wifi_ap') or []),
        # eth ifaces this card may offer: free ones + its own current members.
        'eth_offer': [e for e in _eth_ifaces() if e not in _claimed_eth(model, skip_lan=name)],
    }


@lan_bp.route('/', methods=['GET'])
@login_required
def index():
    model = netconfig.load()
    bridges = [_bridge_view(lan, model) for lan in model.get('lans') or []]
    return render_template(
        'lan/index.html',
        bridges=bridges,
        suggest_name=_suggest_name(model),
        default_subnet=DEFAULT_SUBNET,
        default_dhcp=DEFAULT_DHCP,
        eth_offer=[e for e in _eth_ifaces() if e not in _claimed_eth(model)],
        iface_rows=netutils.iface_table(netutils.list_interfaces()),
        # which zone the admin is on, so the template can pre-warn on disruptive edits
        current_zone=netconfig.ingress_zone(request.remote_addr, model),
    )


@lan_bp.route('/bridge', methods=['POST'])
@login_required
def bridge_save():
    """Create (orig_name='') or update one bridge, then apply."""
    f = request.form
    orig_name = f.get('orig_name', '').strip()
    name = f.get('name', '').strip()

    if not name:
        flash('Bridge name is required', 'warning')
        return redirect(url_for('lan.index'))

    # Parse subnet: RFC1918, strict /24 (network address ending in .0).
    subnet_str = f.get('subnet', '').strip()
    try:
        net = ipaddress.IPv4Network(subnet_str, strict=True)
    except Exception:
        flash('Invalid subnet; must be a CIDR like 192.168.42.0/24', 'warning')
        return redirect(url_for('lan.index'))
    if net.prefixlen != 24:
        flash('Subnet must be a /24', 'warning')
        return redirect(url_for('lan.index'))
    if not net.is_private:
        flash('Subnet must be RFC1918 (10/8, 172.16/12, 192.168/16)', 'warning')
        return redirect(url_for('lan.index'))

    gateway = str(net.network_address + 1)

    # DHCP fields. Range must sit inside the subnet (and not collide with .0/.1).
    dhcp_enabled = f.get('dhcp_enabled') == 'on'
    dhcp_start = f.get('dhcp_start', '').strip()
    dhcp_end = f.get('dhcp_end', '').strip()
    lease = f.get('dhcp_lease', '').strip() or '4h'
    if dhcp_enabled:
        try:
            start_ip = ipaddress.IPv4Address(dhcp_start)
            end_ip = ipaddress.IPv4Address(dhcp_end)
        except Exception:
            flash('DHCP start/end must be valid IPv4 addresses', 'warning')
            return redirect(url_for('lan.index'))
        if start_ip not in net or end_ip not in net:
            flash('DHCP range must fall inside the subnet', 'warning')
            return redirect(url_for('lan.index'))
        if end_ip < start_ip:
            flash('DHCP end must be >= start', 'warning')
            return redirect(url_for('lan.index'))

    eth_members = f.getlist('eth_members')

    # Build the new bridge on a fresh on-disk model. Keep the existing wifi_ap
    # members — those are owned by the WiFi page, don't clobber them.
    cfg = netconfig.load()
    old_entry = next((l for l in cfg.get('lans') or [] if l.get('name') == orig_name), None) if orig_name else None
    wifi_ap = list(((old_entry or {}).get('members') or {}).get('wifi_ap') or [])

    entry = {
        'name': name,
        'subnet': subnet_str,
        'gateway': gateway,
        'dhcp': {
            'enabled': dhcp_enabled,
            'start': dhcp_start,
            'end': dhcp_end,
            'lease': lease,
        },
        'dns_upstream': list((old_entry or {}).get('dns_upstream') or ['8.8.8.8', '1.1.1.1']),
        'members': {'eth': eth_members, 'wifi_ap': wifi_ap},
        'stp': bool((old_entry or {}).get('stp', False)),
    }

    # Lockout guard: bail if this edit would move the admin off the bridge they
    # came in on, unless they've ticked the confirm box.
    old_model = netconfig.load()
    zone = netconfig.ingress_zone(request.remote_addr, old_model)
    target = orig_name or name
    if zone == target and not f.get('confirm_disruptive'):
        flash(f"This edit changes bridge '{target}', which you're connected through — "
              "it may drop your connection. Resubmit with the confirm box ticked.", 'warning')
        return redirect(url_for('lan.index'))

    lans = cfg.setdefault('lans', [])
    if old_entry is not None:
        lans[lans.index(old_entry)] = entry
    else:
        lans.append(entry)

    # Validate the whole model first; fatal errors abort without applying.
    errs = netconfig.validate(cfg)
    fatal = [e for e in errs if not e.startswith('WARN')]
    if fatal:
        for e in fatal:
            flash(e, 'danger')
        return redirect(url_for('lan.index'))

    res = netconfig.apply(cfg)
    _flash_apply_result(res)
    if not [e for e in res.get('errors') or [] if not e.startswith('WARN')]:
        flash(f"Bridge '{name}' saved", 'success')
    return redirect(url_for('lan.index'))


@lan_bp.route('/bridge/<name>/delete', methods=['POST'])
@login_required
def bridge_delete(name):
    """Drop a bridge from the model and apply."""
    cfg = netconfig.load()
    entry = next((l for l in cfg.get('lans') or [] if l.get('name') == name), None)
    if entry is None:
        flash(f"No such bridge '{name}'", 'warning')
        return redirect(url_for('lan.index'))

    # Same lockout guard as edit — don't let them delete the bridge under them
    # without an explicit ack.
    zone = netconfig.ingress_zone(request.remote_addr, cfg)
    if zone == name and not request.form.get('confirm_disruptive'):
        flash(f"Deleting bridge '{name}' will drop your connection — "
              "resubmit with the confirm box ticked.", 'warning')
        return redirect(url_for('lan.index'))

    cfg['lans'].remove(entry)
    res = netconfig.apply(cfg)
    _flash_apply_result(res)
    if not [e for e in res.get('errors') or [] if not e.startswith('WARN')]:
        flash(f"Bridge '{name}' deleted", 'success')
    return redirect(url_for('lan.index'))
