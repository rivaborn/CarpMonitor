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

```sh
wan-carp-demote.sh --status            # gateway status, counters, current demotion
wan-carp-demote.sh --dry-run           # one tick with its own state; changes nothing
echo down > /tmp/wan-carp-demote.force.dry-run   # simulate an outage for --dry-run
echo down > /tmp/wan-carp-demote.force           # LIVE test: really fails over in ~3 min
```

Written after the 2026-10-22 FiOS outage, in which the primary's WAN lost DHCP
for 17 minutes, CARP kept the VIP on it, and the watchdog was latched `HELD`.
