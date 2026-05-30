# Networking architecture

border0-router uses a **declarative, zone-based** network model (v2.0.1+). You
describe the network you want in a single file, `/etc/border0/network.json`, and
an apply engine renders and applies all the underlying config — bridges, DHCP,
NAT, hostapd, wpa_supplicant — idempotently, without a reboot.

The web UI never edits `/etc/network/...`, `/etc/hostapd/...`, etc. directly. It
only writes `network.json` and calls the engine. One source of truth.

## The model

- **LAN = one or more Linux bridges** (`lan0`, `lan1`, …). Each bridge owns a
  subnet, its `.1` gateway, its own DHCP server, and a set of members (ethernet
  ports and/or AP-mode WiFi radios). You can run several LANs at once; **all LANs
  route to each other** and NAT out to the internet.
- **WAN = a logical zone, not a bridge.** Several interfaces can be assigned to
  WAN, but exactly **one is the active uplink** (the live default route) at a
  time; the rest stay configured-but-down. Traffic is never forwarded between WAN
  ports — it's the internet.
- **WiFi is independent of zone.** Each radio is either an **AP** (joins a LAN
  bridge and broadcasts an SSID) or a **client/station** (connects to an upstream
  SSID and becomes a WAN uplink) — or off.

```
                 ┌──────────── lan0 (bridge, 192.168.42.1/24) ───────────┐
   eth1 ─────────┤  dnsmasq DHCP  ·  gateway.border0                      │
   wlan0 (AP) ───┤  members: eth1, wlan0                                  │──┐
                 └───────────────────────────────────────────────────────┘  │
                 ┌──────────── lan1 (bridge, 192.168.50.1/24) ───────────┐  │  NAT
   eth2 ─────────┤  dnsmasq DHCP                                          │──┤ (MASQUERADE)
                 └───────────────────────────────────────────────────────┘  │
                                                                             ▼
                                            WAN zone ── active: eth0 ──► internet
                                            (standby: wlan1 client, eth3, …)
```

## network.json

```jsonc
{
  "version": 1,
  "lans": [
    {
      "name": "lan0",                         // also the bridge interface name
      "subnet": "192.168.42.0/24",
      "gateway": "192.168.42.1",              // the .1 host
      "dhcp": {"enabled": true, "start": "192.168.42.10",
               "end": "192.168.42.250", "lease": "4h"},
      "dns_upstream": ["8.8.8.8", "1.1.1.1"],
      "members": {"eth": ["eth1"], "wifi_ap": ["wlan0"]},
      "stp": false
    }
  ],
  "wan": {
    "interfaces": [
      {"iface": "eth0", "mode": "dhcp"},
      {"iface": "wlan1", "mode": "dhcp"}      // a wifi client, added by the WiFi page
      // static: {"iface":"eth3","mode":"static","address":"...","netmask":"...","gateway":"...","dns":"..."}
    ],
    "active": "eth0"                          // the one live uplink
  },
  "wifi": {
    "wlan0": {"mode": "ap", "ssid": "border0", "psk": "", "band": "a", "channel": "36"},
    "wlan1": {"mode": "client", "client": {"ssid": "Upstream", "psk": "hunter2"}},
    "wlan2": {"mode": "off"}
  }
}
```

Rules the engine enforces (`netconfig.validate`):
- An interface belongs to at most one place (a bridge member **or** a WAN iface).
- A WiFi radio in a bridge's `wifi_ap` must have `mode: "ap"`; a `client` radio
  must be in `wan.interfaces` and not a bridge member.
- `wan.active` must be one of `wan.interfaces`.
- LAN subnets are RFC1918 and must not overlap.
- AP band/channel must be valid (5 GHz is non-DFS only; see hardware notes).
- A radio can't be AP and client at the same time — and on the Pi's built-in
  radio, AP+client even across SSIDs is unreliable, so use a USB adapter for a
  WiFi WAN uplink (warning only).

An empty AP `psk` means an **open** SSID (the shipped default). Set a passphrase
from the WiFi page to switch it to WPA2.

## What the engine renders

| File | Role |
|---|---|
| `/etc/network/interfaces.d/<lanN>.conf` | bridge stanza (eth members in `bridge_ports`; AP wlans are attached by hostapd) |
| `/etc/network/interfaces.d/<eth>.conf` | bridge member (`inet manual`) |
| `/etc/network/interfaces.d/<wan>.conf` | uplink; only the **active** one is `auto`/up |
| `/etc/hostapd/<wlan>.conf` | AP, with `bridge=<lanN>` |
| `/etc/wpa_supplicant/wpa_supplicant-<wlan>.conf` | client credentials |
| `/etc/dnsmasq.d/border0-<lan>.conf` | per-bridge DHCP/DNS, started by `border0-dnsmasq@<lan>` |
| `/etc/border0/firewall.sh` | NAT + forwarding (idempotent `BORDER0_NAT`/`BORDER0_FWD` chains) |

The `10.10.10.10` gateway anchor (`dummy0`) and the `utun+` VPN masquerade (for
Border0 exit nodes) are preserved.

## Active-uplink selection

The DHCP client on this image is **dhcpcd**, which ignores ifupdown route
metrics. So "single active uplink" is enforced simply: only the active WAN
interface is brought up; the others are defined but stay down. Switching the
active uplink downs the old one and ups the new one.

## Applying changes

`python3 -m gateway_admin.netconfig apply` reconciles the live system to
`network.json` (the web UI does this for you). It's idempotent: re-applying an
unchanged model is a no-op, and changing one zone only touches that zone. The
default `network.json` is rendered into the image at build time by the same
engine, so a fresh boot and a UI-driven change go through identical code.

> **Lockout warning:** reassigning the interface you're currently connected
> through (e.g. turning your own AP into a client) will drop your session. The UI
> asks you to confirm; over SSH, mind which interface is your way in.
