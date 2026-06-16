#!/usr/bin/env python3
"""CARP wedge-watchdog.

External watchdog that forces a pfSense CARP failover when the primary holds
CARP MASTER but is too wedged to pass traffic — the one failure mode CARP
cannot self-detect (it never stops advertising the gateway VIP).

The watchdog host sits on the flat L2 *outside* the gateway path, so it can
both distinguish a real soft-wedge (the gateway VIP still answers ICMP but
end-to-end canaries through it fail) and act on it locally even while off-site
notification is impossible.

Site-specific config (IPs, hosts, ports, users, URLs) lives in a gitignored
`.env` — copy `.env.example` and fill it in. Design notes: README.md.

stdlib only. Normally a systemd service; also runnable by hand:
    watchdog.py            # poll loop (real levers)
    watchdog.py --dry-run  # never executes a lever; logs intended action, still latches
    watchdog.py --once     # run a single poll tick and exit
    watchdog.py --check    # run detection + guard once, print JSON, change nothing
    watchdog.py --status   # print persisted state and exit
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request

log = logging.getLogger("carp-watchdog")

# --------------------------------------------------------------------------
# Configuration
#   precedence (low -> high): DEFAULTS < config.env < .env < real environment
#   config.env  — non-sensitive tunables (committed)
#   .env        — site-specific / private values (gitignored; see .env.example)
# --------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("CARP_WATCHDOG_CONFIG", "/etc/carp-watchdog/config.env")
ENV_PATH = os.environ.get("CARP_WATCHDOG_ENV", "/etc/carp-watchdog/.env")

DEFAULTS = {
    # --- non-sensitive tunables (also in config.env) ---
    "POLL_INTERVAL":  "30",     # seconds between polls
    "CONFIRM_FAILS":  "2",      # consecutive fails -> SUSPECT (+ "armed" alert)
    "ARM_SECONDS":    "600",    # continuous-fail seconds in SUSPECT -> fire
    "POST_FIRE_WAIT": "60",     # after a lever, poll egress this long for recovery
    "CANARIES":       "1.1.1.1,8.8.8.8,9.9.9.9",  # public ICMP targets; healthy if ANY answers
    "DEMOTION":       "240",    # CARP demotion delta applied to the primary
    "NTFY_TOPIC":     "carp-watchdog",
    "SSH_KEY":        "/etc/carp-watchdog/id_ed25519",
    "SSH_KNOWN_HOSTS": "/etc/carp-watchdog/known_hosts",
    "STATE_FILE":     "/var/lib/carp-watchdog/state.json",
    "SSH_CONNECT_TIMEOUT": "10",
    "PING_COUNT":     "2",
    "PING_TIMEOUT":   "2",      # per-canary ICMP wait (seconds, Linux `ping -W`)
    # --- site-specific / private: MUST come from .env (placeholders here) ---
    "GATEWAY_VIP":    "",
    "PRIMARY":        "", "PRIMARY_PORT": "22", "PRIMARY_USER": "root",
    "BACKUP":         "", "BACKUP_PORT":  "22", "BACKUP_USER":  "root",
    "HOST":           "", "HOST_PORT":    "22", "HOST_USER":    "root",
    "VMID":           "0",
    "NTFY":           "",
    "WIKI_URL":       "",
    "BACKUP_UI":      "",
}

# Private values with no safe default — the watchdog refuses to run without them.
REQUIRED = ("GATEWAY_VIP", "PRIMARY", "BACKUP", "HOST", "NTFY")


def _parse_env_file(path: str, into: dict) -> None:
    """Overlay KEY=VALUE lines from `path` onto `into` (later files win)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.split(" #", 1)[0].strip().strip('"').strip("'")  # drop inline comment + quotes
            into[k.strip()] = v


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    _parse_env_file(CONFIG_PATH, cfg)   # non-sensitive tunables
    _parse_env_file(ENV_PATH, cfg)      # private overrides
    for k in list(cfg):                 # real environment wins (systemd/shell)
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg


def missing_required(raw: dict) -> list[str]:
    return [k for k in REQUIRED if not raw.get(k)]


class Cfg:
    """Typed view over the raw config dict."""

    def __init__(self, d: dict) -> None:
        self.poll_interval = int(d["POLL_INTERVAL"])
        self.confirm_fails = int(d["CONFIRM_FAILS"])
        self.arm_seconds = int(d["ARM_SECONDS"])
        self.post_fire_wait = int(d["POST_FIRE_WAIT"])
        self.canaries = [c.strip() for c in d["CANARIES"].split(",") if c.strip()]
        self.gateway_vip = d["GATEWAY_VIP"]
        self.primary = (d["PRIMARY"], int(d["PRIMARY_PORT"]), d["PRIMARY_USER"])
        self.backup = (d["BACKUP"], int(d["BACKUP_PORT"]), d["BACKUP_USER"])
        self.host = (d["HOST"], int(d["HOST_PORT"]), d["HOST_USER"])
        self.vmid = int(d["VMID"])
        self.demotion = int(d["DEMOTION"])
        self.ntfy = d["NTFY"].rstrip("/")
        self.ntfy_topic = d["NTFY_TOPIC"]
        self.ssh_key = d["SSH_KEY"]
        self.known_hosts = d["SSH_KNOWN_HOSTS"]
        self.state_file = d["STATE_FILE"]
        self.ping_count = int(d["PING_COUNT"])
        self.ping_timeout = int(d["PING_TIMEOUT"])
        self.ssh_connect_timeout = int(d["SSH_CONNECT_TIMEOUT"])
        self.wiki_url = d["WIKI_URL"]
        self.backup_ui = d["BACKUP_UI"]


# --------------------------------------------------------------------------
# State (persisted; the latch must survive restart/reboot)
# --------------------------------------------------------------------------
HEALTHY = "HEALTHY"
SUSPECT = "SUSPECT"
FIRED = "FIRED"
HELD = "HELD"
LATCHED = (FIRED, HELD)

_STATE_DEFAULTS = {
    "state": HEALTHY,
    "consecutive_fails": 0,
    "suspect_since": None,
    "applied_demotion": 0,
    "lever_used": None,
    "reason": "",
    "dry_run": False,
    "updated": None,
}


def load_state(cfg: Cfg) -> dict:
    try:
        with open(cfg.state_file, encoding="utf-8") as fh:
            s = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    for k, v in _STATE_DEFAULTS.items():
        s.setdefault(k, v)
    return s


def save_state(cfg: Cfg, s: dict) -> None:
    s["updated"] = time.time()
    os.makedirs(os.path.dirname(cfg.state_file), exist_ok=True)
    tmp = cfg.state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(s, fh, indent=2, sort_keys=True)
    os.replace(tmp, cfg.state_file)  # atomic


# --------------------------------------------------------------------------
# Primitives: ICMP + SSH (shell out; stdlib only)
# --------------------------------------------------------------------------
def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


def ping(cfg: Cfg, ip: str) -> bool:
    """Local ICMP from the watchdog host (egresses via the gateway VIP). Raw IP, no DNS."""
    budget = cfg.ping_count * (cfg.ping_timeout + 1) + 3
    rc, _, _ = _run(["ping", "-c", str(cfg.ping_count), "-W", str(cfg.ping_timeout), ip],
                    timeout=budget)
    return rc == 0


def ssh_cmd(cfg: Cfg, target: tuple, command: str, timeout: int = 20) -> tuple[int, str, str]:
    host, port, user = target
    cmd = [
        "ssh", "-i", cfg.ssh_key,
        "-o", "UserKnownHostsFile=" + cfg.known_hosts,
        "-o", "StrictHostKeyChecking=yes",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=" + str(cfg.ssh_connect_timeout),
        "-p", str(port), f"{user}@{host}", command,
    ]
    return _run(cmd, timeout=timeout)


def ssh_reachable(cfg: Cfg, target: tuple) -> bool:
    rc, _, _ = ssh_cmd(cfg, target, "true", timeout=cfg.ssh_connect_timeout + 5)
    return rc == 0


# --------------------------------------------------------------------------
# Detection + pre-fire guard
# --------------------------------------------------------------------------
def gateway_passes_traffic(cfg: Cfg) -> tuple[bool, bool, list[str]]:
    """End-to-end check: ICMP each canary (which egresses via the gateway VIP).
    Healthy if ANY canary answers. Also records whether the VIP itself still
    pings — purely for alert context (a soft-wedge keeps answering the VIP)."""
    reachable = [ip for ip in cfg.canaries if ping(cfg, ip)]
    gw_pings = ping(cfg, cfg.gateway_vip)
    return (len(reachable) > 0), gw_pings, reachable


GUARD_OK, GUARD_NO_INET, GUARD_UNREACHABLE = "OK", "NO_INTERNET", "UNREACHABLE"


def backup_has_internet(cfg: Cfg) -> tuple[str, str | None]:
    """Pre-fire guard. From the BACKUP firewall, can we actually reach the
    internet? Distinguishes a primary-specific wedge (-> fail over) from an
    ISP/upstream outage (-> do NOT fail over). FreeBSD `ping -t <sec>` is the
    whole-run timeout."""
    for canary in cfg.canaries:
        rc, _, _ = ssh_cmd(cfg, cfg.backup, f"ping -c 2 -t 5 {canary}", timeout=20)
        if rc == 255:                 # ssh transport failure -> backup unreachable
            return GUARD_UNREACHABLE, canary
        if rc == 0:
            return GUARD_OK, canary
    return GUARD_NO_INET, None        # connected fine, but no canary answered


def wait_for_egress(cfg: Cfg) -> bool:
    """After a lever fires, poll end-to-end egress up to POST_FIRE_WAIT seconds.
    Returns True as soon as a canary answers (failover restored the path)."""
    deadline = time.monotonic() + cfg.post_fire_wait
    while time.monotonic() < deadline:
        ok, _, _ = gateway_passes_traffic(cfg)
        if ok:
            return True
        time.sleep(5)
    return False


# --------------------------------------------------------------------------
# Notifications (local ntfy; publish succeeds even with WAN down — phone pulls
# on reconnect). Never let alerting crash the watchdog.
# --------------------------------------------------------------------------
def notify(cfg: Cfg, title: str, message: str, tags: str = "warning",
           priority: str = "5", click: str | None = None) -> None:
    url = f"{cfg.ntfy}/{cfg.ntfy_topic}"
    headers = {"Title": title, "Tags": tags, "Priority": str(priority)}
    if click:
        headers["Click"] = click
    try:
        req = urllib.request.Request(url, data=message.encode("utf-8"),
                                     headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10)
        log.info("ntfy: %s — %s", title, message)
    except Exception as e:  # noqa: BLE001 - alerting is best-effort
        log.warning("ntfy POST failed (%s): %s", e, title)


# --------------------------------------------------------------------------
# Failover: pre-fire guard, then the escalating lever ladder. Latches.
# --------------------------------------------------------------------------
def _latch_fired(cfg: Cfg, state: dict, dry_run: bool, lever: str,
                 recovered: bool | None) -> None:
    state["state"] = FIRED
    state["dry_run"] = dry_run
    if dry_run:
        rec_txt = "egress recovery not measured (dry-run)"
    elif recovered:
        rec_txt = "egress recovered after failover"
    else:
        rec_txt = "egress has NOT recovered yet — watch the backup"
    state["reason"] = ("[dry-run] would fire via " if dry_run else "fired via ") + lever + "; " + rec_txt
    notify(
        cfg,
        "CARP watchdog: [DRY-RUN] would fire" if dry_run else "CARP watchdog: FAILOVER FIRED",
        f"{'(dry-run) ' if dry_run else ''}Forced CARP failover via {lever}. "
        f"{rec_txt}. Latched — run carp-watchdog-reset after restoring the primary.",
        tags="test_tube" if dry_run else "rotating_light",
        priority="5",
        click=cfg.backup_ui or None,
    )
    log.warning("LATCHED FIRED (dry_run=%s): %s [%s]", dry_run, lever, rec_txt)


def _hold(cfg: Cfg, state: dict, reason: str, title: str, message: str,
          tags: str, priority: str = "4") -> None:
    state["state"] = HELD
    state["reason"] = reason
    notify(cfg, title, message, tags=tags, priority=priority, click=cfg.wiki_url or None)
    log.error("LATCHED HELD: %s", reason)


def attempt_failover(cfg: Cfg, state: dict, dry_run: bool) -> None:
    """Pre-fire guard -> escalating lever ladder. Latches FIRED or HELD.
    Never auto-undoes a demotion (would flap back to a still-wedged primary)."""
    # --- pre-fire guard: is the backup a viable failover target? ----------
    guard, _ = backup_has_internet(cfg)
    if guard == GUARD_NO_INET:
        _hold(cfg, state,
              "ISP/upstream outage suspected — backup also has no internet; NOT failing over",
              "CARP watchdog: HELD (ISP outage?)",
              "Gateway end-to-end is down AND the backup firewall cannot reach the "
              "internet either — looks like an upstream/ISP outage, not a primary "
              "wedge. NOT failing over. Investigate WAN.",
              tags="no_entry,warning")
        return
    if guard == GUARD_UNREACHABLE:
        _hold(cfg, state,
              "backup firewall unreachable — cannot confirm a safe failover target",
              "CARP watchdog: HELD (backup unreachable)",
              "Gateway end-to-end is down and the backup firewall is unreachable "
              "over SSH, so the pre-fire guard cannot confirm a working failover "
              "target. NOT firing. On-site check needed.",
              tags="no_entry,warning")
        return

    # --- guard OK: a primary-specific wedge. Fire. ------------------------
    if dry_run:
        # Read-only reachability probes so the dry-run reports the real lever.
        if ssh_reachable(cfg, cfg.primary):
            log.info("[DRY-RUN] would apply: sysctl net.inet.carp.demotion=%d on %s",
                     cfg.demotion, cfg.primary[0])
            _latch_fired(cfg, state, True,
                         f"graceful demotion (+{cfg.demotion}) on primary {cfg.primary[0]}", None)
        elif ssh_reachable(cfg, cfg.host):
            log.info("[DRY-RUN] would run: qm stop %d on host %s", cfg.vmid, cfg.host[0])
            _latch_fired(cfg, state, True,
                         f"hard stop of VM {cfg.vmid} on host {cfg.host[0]}", None)
        else:
            _hold(cfg, state,
                  "primary AND host unreachable — on-site recovery needed (no IPMI/PDU)",
                  "CARP watchdog: [DRY-RUN] CRITICAL — would HOLD",
                  "(dry-run) Backup has internet but BOTH the primary firewall and its "
                  "host are unreachable over SSH. Real run would be unable to fail over.",
                  tags="test_tube,rotating_light", priority="5")
        return

    # --- Lever 1: graceful CARP demotion on the primary (reversible) ------
    rc, out, err = ssh_cmd(cfg, cfg.primary,
                           f"sysctl net.inet.carp.demotion={cfg.demotion}", timeout=20)
    if rc == 0:
        state["applied_demotion"] = cfg.demotion
        state["lever_used"] = "demotion"
        recovered = wait_for_egress(cfg)
        _latch_fired(cfg, state, False,
                     f"graceful demotion (+{cfg.demotion}) on primary {cfg.primary[0]}", recovered)
        return
    log.error("Lever 1 (demotion) rc=%s: %s — escalating to VM stop", rc, (err or out).strip())

    # --- Lever 2: hard stop of the primary pfSense VM on its host ---------
    rc2, out2, err2 = ssh_cmd(cfg, cfg.host, f"qm stop {cfg.vmid}", timeout=45)
    if rc2 == 0:
        state["lever_used"] = "qm_stop"
        recovered = wait_for_egress(cfg)
        _latch_fired(cfg, state, False,
                     f"hard stop of VM {cfg.vmid} on host {cfg.host[0]}", recovered)
        return
    log.error("Lever 2 (qm stop) rc=%s: %s", rc2, (err2 or out2).strip())

    # --- Both levers unavailable: critical, on-site recovery -------------
    _hold(cfg, state,
          "primary AND host unreachable — on-site recovery needed (no IPMI/PDU)",
          "CARP watchdog: CRITICAL — HELD",
          f"Gateway is wedged and the backup has internet, but BOTH the primary "
          f"firewall ({cfg.primary[0]}) and its host ({cfg.host[0]}) are unreachable "
          f"over SSH. The watchdog cannot fail over — on-site recovery needed (no "
          f"IPMI/PDU). applied_demotion={state['applied_demotion']}.",
          tags="rotating_light,sos", priority="5")


# --------------------------------------------------------------------------
# One poll cycle
# --------------------------------------------------------------------------
def tick(cfg: Cfg, dry_run: bool) -> dict:
    state = load_state(cfg)

    # Latched: observe only. carp-watchdog-reset clears the latch on disk and we resume.
    if state["state"] in LATCHED:
        log.debug("latched in %s (%s) — no action until carp-watchdog-reset",
                  state["state"], state["reason"])
        return state

    ok, gw_pings, reachable = gateway_passes_traffic(cfg)

    if ok:
        if state["state"] == SUSPECT:
            log.info("recovered: %d/%d canaries reachable again",
                     len(reachable), len(cfg.canaries))
            notify(cfg, "CARP watchdog: recovered",
                   "Gateway end-to-end recovered before the arm timer expired. Stand down.",
                   tags="white_check_mark", priority="3")
        if state["state"] != HEALTHY or state["consecutive_fails"]:
            log.info("HEALTHY (%d/%d canaries)", len(reachable), len(cfg.canaries))
        state.update(state=HEALTHY, consecutive_fails=0, suspect_since=None)
        save_state(cfg, state)
        return state

    # A failed end-to-end poll.
    state["consecutive_fails"] += 1
    log.warning("gateway end-to-end FAIL (%d consecutive); VIP %s pings=%s",
                state["consecutive_fails"], cfg.gateway_vip, gw_pings)

    if state["state"] == HEALTHY:
        if state["consecutive_fails"] >= cfg.confirm_fails:
            state["state"] = SUSPECT
            state["suspect_since"] = time.time()
            notify(cfg, "CARP watchdog: ARMED",
                   f"Gateway failed {state['consecutive_fails']} consecutive "
                   f"end-to-end checks (VIP {cfg.gateway_vip} pings={gw_pings}). "
                   f"Auto-failover in ~{cfg.arm_seconds // 60} min unless it recovers. "
                   f"(You may not see this until the path is restored.)",
                   tags="warning,hourglass", priority="5", click=cfg.wiki_url or None)
            log.warning("-> SUSPECT (armed); arm timer %ds", cfg.arm_seconds)
        save_state(cfg, state)
        return state

    if state["state"] == SUSPECT:
        elapsed = time.time() - (state["suspect_since"] or time.time())
        log.warning("SUSPECT for %ds / %ds", int(elapsed), cfg.arm_seconds)
        if elapsed >= cfg.arm_seconds:
            log.warning("arm timer expired -> attempting failover (dry_run=%s)", dry_run)
            attempt_failover(cfg, state, dry_run)
        save_state(cfg, state)
        return state

    save_state(cfg, state)
    return state


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="CARP wedge-watchdog")
    ap.add_argument("--dry-run", action="store_true",
                    help="never execute a lever; log the intended action, still latch")
    ap.add_argument("--once", action="store_true", help="run a single poll tick and exit")
    ap.add_argument("--check", action="store_true",
                    help="run detection + guard once, print JSON, change nothing")
    ap.add_argument("--status", action="store_true", help="print persisted state and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    raw = load_config()

    if args.status:                       # status only reads state; no private config needed
        print(json.dumps(load_state(Cfg(raw)), indent=2, sort_keys=True))
        return

    missing = missing_required(raw)
    if missing:
        log.error("missing required config: %s — set them in %s (see .env.example)",
                  ", ".join(missing), ENV_PATH)
        sys.exit(1)
    cfg = Cfg(raw)

    if args.check:
        ok, gw, reach = gateway_passes_traffic(cfg)
        guard, gcan = backup_has_internet(cfg)
        print(json.dumps({
            "gateway_passes_traffic": ok,
            "canaries_reachable": reach,
            "gateway_vip_pings": gw,
            "backup_guard": guard,
            "backup_guard_canary": gcan,
        }, indent=2))
        return

    log.info("carp-watchdog starting (dry_run=%s, poll=%ds, confirm=%d, arm=%ds)",
             args.dry_run, cfg.poll_interval, cfg.confirm_fails, cfg.arm_seconds)
    s = load_state(cfg)
    if s["state"] in LATCHED:
        log.warning("starting LATCHED in %s — run carp-watchdog-reset to resume: %s",
                    s["state"], s["reason"])

    if args.once:
        tick(cfg, args.dry_run)
        return

    while True:
        try:
            tick(cfg, args.dry_run)
        except Exception:  # noqa: BLE001 - a poll must never kill the loop
            log.exception("tick failed (continuing)")
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    main()
