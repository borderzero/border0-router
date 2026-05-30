from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify, abort
from flask_login import login_required

from ... import netconfig, netutils

wifi_bp = Blueprint('wifi', __name__, url_prefix='/wifi')

# Friendly band labels for the AP mode select.
BAND_LABELS = {
    'g': '2.4 GHz (802.11g/n)',
    'a': '5 GHz (802.11a/n/ac)',
}


def _flash_apply_result(res):
    """Flash whatever apply() coughed up: WARN as warning, the rest as danger."""
    for e in res.get('errors') or []:
        flash(e, 'warning' if e.startswith('WARN') else 'danger')


def _ap_bridge(model, wlan):
    """Name of the bridge whose wifi_ap currently holds wlan, or None."""
    for lan in model.get('lans') or []:
        if wlan in ((lan.get('members') or {}).get('wifi_ap') or []):
            return lan.get('name')
    return None


def _radio_view(iface, model):
    """Shape one radio's stored config into what the template card needs.

    Off radios may have no entry at all — default everything so the AP/client
    blocks still render with sane placeholders.
    """
    cfg = (model.get('wifi') or {}).get(iface) or {}
    client = cfg.get('client') or {}
    mode = cfg.get('mode', 'off')
    band = cfg.get('band') if cfg.get('band') in ('g', 'a') else 'g'
    return {
        'name': iface,
        'mode': mode,
        'ssid': cfg.get('ssid', ''),
        'psk': cfg.get('psk', ''),
        'band': band,
        # preselect a valid channel; falls back to the band default
        'channel': netutils._display_channel(cfg.get('channel'), band),
        'client_ssid': client.get('ssid', ''),
        'client_psk': client.get('psk', ''),
        'attached_bridge': _ap_bridge(model, iface),
        'service': netutils.hostapd_service_state(iface),
    }


@wifi_bp.route('/', methods=['GET'])
@login_required
def index():
    model = netconfig.load()
    radios = [_radio_view(i, model) for i in netutils.list_wifi_radios()]
    return render_template(
        'wifi/index.html',
        radios=radios,
        bridges=[lan.get('name') for lan in model.get('lans') or []],
        band_labels=BAND_LABELS,
        channels_2g=netutils.CHANNELS_2G,
        channels_5g=netutils.CHANNELS_5G,
        # which zone the admin is on, so the template can pre-warn on disruptive edits
        current_zone=netconfig.ingress_zone(request.remote_addr, model),
    )


def _sync_membership(cfg, iface, mode, target_bridge=None):
    """Keep bridge wifi_ap lists and wan.interfaces in step with this radio's mode.

    The WiFi page owns membership for wlanX — nobody else touches it. Rules:
      ap     -> in exactly one bridge's wifi_ap, out of every other and out of wan
      client -> in wan.interfaces (dhcp), out of every bridge's wifi_ap
      off    -> out of everything
    """
    # Strip the radio out of every bridge first; re-add below if it's an AP.
    for lan in cfg.get('lans') or []:
        mem = lan.setdefault('members', {})
        aps = mem.setdefault('wifi_ap', [])
        if iface in aps:
            aps.remove(iface)

    wan = cfg.setdefault('wan', {})
    ifaces = wan.setdefault('interfaces', [])

    if mode == 'ap':
        for lan in cfg.get('lans') or []:
            if lan.get('name') == target_bridge:
                lan.setdefault('members', {}).setdefault('wifi_ap', []).append(iface)
                break
        # an AP is never a wan uplink
        wan['interfaces'] = [w for w in ifaces if w.get('iface') != iface]
    elif mode == 'client':
        if not any(w.get('iface') == iface for w in ifaces):
            ifaces.append({'iface': iface, 'mode': 'dhcp'})
    else:  # off
        wan['interfaces'] = [w for w in ifaces if w.get('iface') != iface]


@wifi_bp.route('/<iface>', methods=['POST'])
@login_required
def save(iface):
    if iface not in netutils.list_wifi_radios():
        abort(404)

    f = request.form
    mode = f.get('mode', 'off').strip()
    if mode not in ('ap', 'client', 'off'):
        flash(f'Invalid mode {mode!r} for {iface}', 'warning')
        return redirect(url_for('wifi.index'))

    cfg = netconfig.load()

    # Build the radio entry from the form. Validation of band/channel is left to
    # netconfig.validate() so we don't reimplement the rules in two places.
    if mode == 'ap':
        ssid = f.get('ssid', '').strip()
        psk = f.get('psk', '').strip()
        band = f.get('band', '').strip()
        channel = f.get('channel', '').strip()
        target_bridge = f.get('target_bridge', '').strip()
        if not ssid:
            flash(f'SSID is required for {iface}', 'warning')
            return redirect(url_for('wifi.index'))
        if len(psk) < 8:
            flash(f'Passphrase must be at least 8 characters for {iface}', 'warning')
            return redirect(url_for('wifi.index'))
        if not target_bridge or target_bridge not in {lan.get('name') for lan in cfg.get('lans') or []}:
            flash(f'Pick an existing LAN bridge to attach {iface} to', 'warning')
            return redirect(url_for('wifi.index'))
        entry = {'mode': 'ap', 'ssid': ssid, 'psk': psk, 'band': band, 'channel': channel}
    elif mode == 'client':
        ssid = f.get('client_ssid', '').strip()
        psk = f.get('client_psk', '').strip()
        target_bridge = None
        if not ssid:
            flash(f'Upstream SSID is required for {iface}', 'warning')
            return redirect(url_for('wifi.index'))
        if len(psk) < 8:
            flash(f'Upstream passphrase must be at least 8 characters for {iface}', 'warning')
            return redirect(url_for('wifi.index'))
        entry = {'mode': 'client', 'client': {'ssid': ssid, 'psk': psk}}
    else:  # off
        target_bridge = None
        entry = {'mode': 'off'}

    # Lockout guard: if this radio is the AP for the bridge the admin came in on,
    # reconfiguring it can drop their connection. Require the confirm box.
    zone = netconfig.ingress_zone(request.remote_addr, cfg)
    if zone and zone == _ap_bridge(cfg, iface) and not f.get('confirm_disruptive'):
        flash(f"{iface} serves the bridge '{zone}' you're connected through — "
              "this change may drop your connection. Resubmit with the confirm box ticked.", 'warning')
        return redirect(url_for('wifi.index'))

    cfg.setdefault('wifi', {})[iface] = entry
    _sync_membership(cfg, iface, mode, target_bridge)

    # Validate the whole model first; fatal errors abort without applying.
    errs = netconfig.validate(cfg)
    fatal = [e for e in errs if not e.startswith('WARN')]
    if fatal:
        for e in fatal:
            flash(e, 'danger')
        return redirect(url_for('wifi.index'))

    res = netconfig.apply(cfg)
    _flash_apply_result(res)
    if not [e for e in res.get('errors') or [] if not e.startswith('WARN')]:
        flash(f'{iface} saved ({mode})', 'success')
    return redirect(url_for('wifi.index'))


@wifi_bp.route('/<iface>/scan', methods=['POST'])
@login_required
def scan(iface):
    """Nearby APs for the client upstream picker. JSON, [] on any failure."""
    if iface not in netutils.list_wifi_radios():
        abort(404)
    return jsonify(netutils.scan_wifi(iface))
