# CarpMonitor — CARP wedge-watchdog

An external watchdog that forces a **pfSense CARP failover** when the primary
holds CARP MASTER but is too wedged to pass traffic — the one failure mode CARP
cannot self-detect, because it never stops advertising the gateway VIP.

The watchdog runs on a small host that sits on the **flat L2 outside the gateway
path**. From there it can both *distinguish* a real soft-wedge (the gateway VIP
still answers ICMP, but end-to-end traffic through it fails) and *act* on it
locally — even when off-site notification is impossible because the WAN is the
thing that's down.

## Configuration: two files

All site-specific / private values are kept out of the repo.

| File                            | Committed? | Holds                                                              |
| ------------------------------- | ---------- | ----------------------------------------------------------------- |
| `etc/carp-watchdog/config.env`  | yes        | non-sensitive tunables (timings, canaries, demotion delta, paths) |
| `etc/carp-watchdog/.env`        | **no** (gitignored) | IPs, hostnames, ports, users, VMID, ntfy/wiki/UI URLs    |
| `etc/carp-watchdog/.env.example`| yes        | template for `.env` (RFC 5737 documentation addresses)            |

Load precedence (low → high): built-in defaults → `config.env` → `.env` → real
environment. The daemon refuses to start if any required private value
(`GATEWAY_VIP`, `PRIMARY`, `BACKUP`, `HOST`, `NTFY`) is unset.

## Layout (mirrors deploy paths)

| Repo path                                     | Deploys to                                   | Mode | Owner          |
| --------------------------------------------- | -------------------------------------------- | ---- | -------------- |
| `opt/carp-watchdog/watchdog.py`               | `/opt/carp-watchdog/watchdog.py`             | 0755 | root:root      |
| `etc/carp-watchdog/config.env`                | `/etc/carp-watchdog/config.env`              | 0644 | root:root      |
| `etc/carp-watchdog/.env.example`              | (template; copy to `.env`)                   | 0644 | root:root      |
| `etc/systemd/system/carp-watchdog.service`    | `/etc/systemd/system/carp-watchdog.service`  | 0644 | root:root      |
| `usr/local/bin/carp-watchdog-reset`           | `/usr/local/bin/carp-watchdog-reset`         | 0755 | root:root      |

Provisioned out of band (never in the repo): the dedicated SSH key
`/etc/carp-watchdog/id_ed25519`(+`.pub`) and the pinned
`/etc/carp-watchdog/known_hosts`.

## How it works (one paragraph)

A `Type=simple` systemd service polls every 30s. Healthy = at least one external
canary answers ICMP (traffic egresses via the gateway VIP). Two consecutive
failures → `SUSPECT` + an "armed" ntfy and a 600s timer. Any success →
`HEALTHY`. 600s of continuous failure → the **pre-fire guard** asks the *backup*
firewall whether it can reach the internet; if not (ISP/upstream outage) it
`HELD`s and does **not** fail over. If the backup is fine, it fires the lever
ladder: **(1)** `sysctl net.inet.carp.demotion=<DEMOTION>` on the primary
(reversible), then polls egress; **(2)** if that fails, `qm stop <VMID>` on the
hypervisor host. Either success → `FIRED` + latched. Both unavailable → `HELD`
CRITICAL. The latch (persisted in `state.json`) prevents flapping until
`carp-watchdog-reset` runs. While latched the watchdog takes no action, so it
repeats a "still FIRED/HELD" ntfy every `REALERT_SECONDS` (6 h; `0` = never),
and once at start-up if it starts latched.

## Deploy

```bash
# from a checkout, on a machine with sudo on the watchdog host:
sudo install -d -m 755 /opt/carp-watchdog /etc/carp-watchdog
sudo install -m 0755 opt/carp-watchdog/watchdog.py        /opt/carp-watchdog/watchdog.py
sudo install -m 0644 etc/carp-watchdog/config.env         /etc/carp-watchdog/config.env
sudo install -m 0755 usr/local/bin/carp-watchdog-reset    /usr/local/bin/carp-watchdog-reset
sudo install -m 0644 etc/systemd/system/carp-watchdog.service /etc/systemd/system/carp-watchdog.service

# private config: copy the template and fill in real values
sudo install -m 0600 etc/carp-watchdog/.env.example       /etc/carp-watchdog/.env
sudoedit /etc/carp-watchdog/.env

# dedicated service user (no shell, no home)
sudo useradd --system --no-create-home --shell /usr/sbin/nologin carp-watchdog || true

# the service user must read the SSH key, known_hosts, and .env
sudo chown carp-watchdog:carp-watchdog \
    /etc/carp-watchdog/id_ed25519 /etc/carp-watchdog/known_hosts /etc/carp-watchdog/.env
sudo chmod 600 /etc/carp-watchdog/id_ed25519 /etc/carp-watchdog/.env

# Unprivileged ICMP must be permitted for the service user's pings. Ubuntu 22.04
# ships /usr/bin/ping with cap_net_raw=ep, so this normally works.
#   verify:  sudo -u carp-watchdog ping -c1 -W2 1.1.1.1
#   if it fails: echo 'net.ipv4.ping_group_range = 0 2147483647' \
#       | sudo tee /etc/sysctl.d/99-ping.conf && sudo sysctl --system

sudo systemctl daemon-reload
sudo systemctl enable --now carp-watchdog
journalctl -u carp-watchdog -f
```

## Verification plan

1. **Smoke (no risk):** `sudo -u carp-watchdog python3 /opt/carp-watchdog/watchdog.py --check`
   → expect `gateway_passes_traffic: true`, canaries listed, `backup_guard: OK`.
2. **Dry-run:** run with `--dry-run` and blackhole the canaries on the watchdog
   host only, so end-to-end fails while the gateway is fine:
   ```bash
   for ip in 1.1.1.1 8.8.8.8 9.9.9.9; do sudo ip route add blackhole "$ip"; done
   ```
   Expect: `SUSPECT` + "armed" ntfy at ~60s, arm timer, then a `[DRY-RUN] would
   fire via graceful demotion …` ntfy (nothing executed). Remove the blackholes
   (`sudo ip route del blackhole <ip>`) and `carp-watchdog-reset`.
3. **ISP-outage path:** repeat (2) but also block the backup's egress so the
   guard returns `NO_INTERNET` → expect `HELD`, no lever.
4. **Live failover (maintenance window):** real run, let it fire the demotion;
   confirm the backup promotes, egress recovers, routes re-advertise; then
   `carp-watchdog-reset` → choose un-demote to restore the primary.
5. **Resilience:** `systemctl restart carp-watchdog` and a full host reboot
   preserve `state.json` and the latch.

## Manual invocations

```bash
watchdog.py --check      # detection + guard, JSON, no state change
watchdog.py --status     # print persisted state.json
watchdog.py --once       # one poll tick, then exit
watchdog.py --dry-run    # poll loop; logs intended lever, never executes, still latches
```

## Notes / decisions baked in

- **Demotion is additive.** Lever 1 writes `=<DEMOTION>` (adds to the counter).
  `carp-watchdog-reset` reads the *current* value and writes `=-<current>` to
  force it back to 0 — robust even if the primary rebooted (counter already 0)
  between fire and reset.
- **The demotion is never auto-undone** — un-demoting would flap back to a
  still-wedged primary. Recovery + un-demote is a deliberate manual step.
- **pfSense admin SSH must yield a non-interactive shell** so `sysctl`/`qm` run
  cleanly (i.e. shell access, not the console menu).
- **Alerting is local-first:** ntfy publishes to a self-hosted instance on the
  watchdog host, which succeeds even with the WAN down; the phone pulls on
  reconnect. The "armed" alert therefore may not be seen until *after* failover
  restores the path — i.e. for a real wedge the arm window is effectively
  full-auto by design.

## Companion: `pfsense/wan-carp-demote.sh` (runs on the primary firewall)

The watchdog catches a primary that stops passing traffic. A dead **WAN** on the
primary is caught sooner by this script, which runs on the primary itself
(Site A: `/conf/wan-carp-demote/wan-carp-demote.sh` on `.3`, from a pfSense
`cron/item` every minute). After 3 consecutive minutes of pfSense's own gateway
status for `WAN_DHCP` reading `down`, it adds `net.inet.carp.demotion=240` so
the backup takes the VIP; after 5 consecutive minutes of `online` it removes
exactly the demotion it added. `loss`/`delay` never trigger it. State lives in
`/var/run/wan-carp-demote.state` and is discarded after a reboot (which resets
the demotion counter). Alerts go to the same ntfy topic as the watchdog.

Before demoting, it checks that the **backup** can reach the internet: it adds a
temporary host route for `149.112.112.112` via the backup's LAN address
(`192.168.1.2`), pings it, and removes the route, which tests the path LAN
clients would take after the failover. If the backup is dead too it holds (one
ntfy, "held"), and re-checks every minute, so it fails over as soon as the
backup recovers while the WAN is still down.

```sh
wan-carp-demote.sh --status            # gateway status, counters, demotion, backup internet
wan-carp-demote.sh --dry-run           # one tick with its own state; changes nothing
echo down > /tmp/wan-carp-demote.force.dry-run   # simulate an outage for --dry-run
WCD_BACKUP_LAN=192.168.1.250 wan-carp-demote.sh --dry-run   # simulate a dead backup
echo down > /tmp/wan-carp-demote.force           # LIVE test: really fails over in ~3 min
```

Written after the 2026-10-22 FiOS outage, in which the primary's WAN lost DHCP
for 17 minutes, CARP kept the VIP on it, and the watchdog was latched `HELD`.
The backup check was added after that night's live test, when the T-Mobile
backup lost its upstream two minutes into carrying the site and stayed down for
about 18 minutes.

## Companion: `pfsense/wg-s2s/` (site-to-site WireGuard fast path, both masters)

The two sites are joined by a Tailscale subnet route between the pfSense CARP
masters. On pfSense that is user-space `wireguard-go` on Celeron J3160s, and it
carried about 1 MB/s per stream (2 MB/s on four). A kernel WireGuard tunnel
(`pfSense-pkg-WireGuard`, `if_wg`) between the same two masters carries about
17 MB/s on one stream (measured 2026-11-16, Coachman to Saginaw; the other
direction is capped by Saginaw's 21 Mbit/s upload either way). Tailscale stays
as the fallback and for off-site access.

`wg-s2s.sh` runs on each master from a pfSense `cron/item` every minute
(`lockf`, 5 checks 10 s apart), with its settings in `/conf/wg-s2s/wg-s2s.conf`
(see `wg-s2s.conf.example`):

- Tailscale installs the remote LAN as a `/24` on `tailscale0`. While the peer's
  tunnel address answers ping, the script adds the remote LAN as two `/25`s on
  the WireGuard interface; being more specific, they win.
- After 2 failed checks it deletes the `/25`s, and traffic is back on Tailscale
  at once. It then re-learns the peer's public address from
  `tailscale debug netmap` (both WANs are DHCP), sets it with `wg set`, and
  writes it to `config.xml`. WireGuard roaming already handles one side
  changing address; the re-learn covers both changing while the tunnel is down.
- A node that is not CARP master deletes its `/25`s **and takes its tunnel
  down**. Otherwise the peer keeps routing to it while replies leave through the
  new master over Tailscale, and that asymmetric path breaks TCP (states are
  `if-bound`). With the tunnel down the peer falls back too.

Set-up helpers (run once per master, in order): `wg_stage_a.php` creates the
tunnel and prints its public key, `wg_stage_b.php` adds the peer and starts the
service, `wg_rules.php` adds the firewall rules, `wg_cron.php` adds (or
`remove`s) the cron entry. They go through the package's own form handlers, so
everything is visible and editable in the GUI. Plain `php` works: the scripts
clear `$argv` before including the package, whose `wg_service.inc` otherwise
starts its service entry point (and dies unless run by `php_wg`).

Firewall rules (`wg_rules.php`):

- WAN: pass UDP 51820 from any (peer keys authenticate; "any" survives the
  peer's DHCP address changing).
- WireGuard group: pass the remote LAN and the tunnel net.
- WireGuard group, **in and out (floating)**: block UDP 41641 between the
  tunnel addresses. `tailscaled` offers every local address as an endpoint and
  otherwise moves the router-to-router Tailscale path *inside* the WireGuard
  tunnel, so the fallback would die with the tunnel it backs up. The inbound
  block alone is not enough: both sides send, so each side's outbound state
  admits the other's packets.

Rules on the WireGuard group are HA-synced to the backups, which do not have the
package: pfSense comments them out there (`array key "WireGuard" does not
exist`), so they are harmless. Only the masters run WireGuard (the backups' WANs
are double-NAT'd); after a CARP failover, traffic between the sites runs over
Tailscale until the master returns.

```sh
grep 'wg-s2s\[' /var/log/system.log        # state changes
wg show                                      # handshake, endpoint, transfer
netstat -rn -f inet | grep tun_wg            # the /25s while WireGuard carries traffic
php /conf/wg-s2s/wg_cron.php remove          # rollback step 1 (then delete the /25s)
```

Tested 2026-11-16: tunnel down on one side (routes withdrawn on both in about
20 s, restored about 10 s after it came back), a graceful Saginaw CARP failover
and failback (no lost pings; WireGuard back about 6 s after the master
returned), and wrong endpoints on both sides (corrected by roaming before the
script needed to). Re-check after any pfSense upgrade, like the `rc.carp*`
hooks.
