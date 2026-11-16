#!/bin/sh
# wg-s2s.sh - steer site-to-site LAN traffic onto the kernel WireGuard tunnel between the two
# pfSense CARP masters, and fall back to the Tailscale subnet route when the tunnel is not usable.
#
# How: Tailscale installs the remote LAN as a /24 on tailscale0. While the WireGuard peer answers,
# this script adds the remote LAN as two /25 routes on the WireGuard interface; being more specific,
# they win. When the peer stops answering (2 checks in a row) or this node is not the CARP master,
# the /25s are deleted and traffic is back on Tailscale at once.
# A dead tunnel also triggers an endpoint re-learn: the peer router's current public IP is read from
# `tailscale status --json` (both masters are tailnet peers), because both WANs get DHCP addresses.
#
# Runs from pfSense cron every minute (via lockf); checks every 10 s inside the minute.
# Config: /conf/wg-s2s/wg-s2s.conf (per site). Logs: `clog`/system log, tag wg-s2s.
# Installed 2026-11-16. Lives in /conf so it survives upgrades, but re-check after any pfSense
# upgrade like the rc.carp* hooks (cron entry is in config.xml <cron>, not HA-synced).

CONF=/conf/wg-s2s/wg-s2s.conf
. "$CONF" || exit 1
# expects: WG_IF LOCAL_TUN PEER_TUN PEER_PUB PEER_TS_IP REMOTE_NETS CARP_IF CARP_VHID
STATE=/var/run/wg-s2s.fails
TS=/usr/local/bin/tailscale

log() { logger -t wg-s2s "$*"; }

is_master() { ifconfig "$CARP_IF" 2>/dev/null | grep -q "carp: MASTER vhid $CARP_VHID "; }

route_on_wg() {   # $1 = cidr; true if a route for exactly that cidr exists on the WireGuard interface
	netstat -rn -f inet | awk -v n="$1" -v i="$WG_IF" '$1 == n && $NF == i { f = 1 } END { exit !f }'
}

routes_up() {
	changed=""
	for n in $REMOTE_NETS; do
		route_on_wg "$n" && continue
		route -q delete -net "$n" >/dev/null 2>&1   # a stale entry elsewhere, if any
		route -q add -net "$n" -interface "$WG_IF" >/dev/null 2>&1 && changed=1
	done
	[ -n "$changed" ] && log "WireGuard up: routes $REMOTE_NETS via $WG_IF"
}

routes_down() {
	changed=""
	for n in $REMOTE_NETS; do
		route_on_wg "$n" && route -q delete -net "$n" >/dev/null 2>&1 && changed=1
	done
	[ -n "$changed" ] && log "WireGuard not usable ($1): routes $REMOTE_NETS removed, traffic on Tailscale"
}

peer_alive() { ping -c 2 -t 4 -S "$LOCAL_TUN" "$PEER_TUN" >/dev/null 2>&1; }

relearn_endpoint() {
	# The netmap lists the endpoints the peer registered with the control plane (its public WAN
	# address among them). `tailscale status` is no use here: its CurAddr is whatever path is in
	# use, and Addrs is empty on 1.80.
	ip=$("$TS" debug netmap 2>/dev/null | php -r '
		$j = json_decode(stream_get_contents(STDIN), true); $want = $argv[1] . "/32";
		foreach (($j["Peers"] ?? []) as $p) {
			if (!in_array($want, $p["Addresses"] ?? [], true)) continue;
			foreach (($p["Endpoints"] ?? []) as $a) {
				$h = preg_replace("/:[0-9]+$/", "", (string)$a);
				if (filter_var($h, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4 | FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE)) { echo $h; exit; }
			}
		}' "$PEER_TS_IP")
	[ -z "$ip" ] && { log "endpoint re-learn: no public address for $PEER_TS_IP in the tailscale netmap"; return; }
	cur=$(wg show "$WG_IF" endpoints | awk -v k="$PEER_PUB" '$1 == k { sub(/:[0-9]+$/, "", $2); print $2 }')
	[ "$ip" = "$cur" ] && return
	wg set "$WG_IF" peer "$PEER_PUB" endpoint "$ip:51820" && log "endpoint re-learned: $cur -> $ip"
	# keep config.xml in step so a reboot or package resync uses the new address
	php -r '
		require_once("config.inc");
		foreach (config_get_path("installedpackages/wireguard/peers/item", []) as $i => $p) {
			if (($p["publickey"] ?? "") === $argv[1] && ($p["endpoint"] ?? "") !== $argv[2]) {
				config_set_path("installedpackages/wireguard/peers/item/$i/endpoint", $argv[2]);
				write_config("wg-s2s: peer endpoint re-learned from tailscale status: " . $argv[2]);
			}
		}' "$PEER_PUB" "$ip"
}

wg_is_up() { ifconfig "$WG_IF" 2>/dev/null | head -1 | grep -q '<UP,'; }

check() {
	if ! is_master; then
		routes_down "not CARP master"
		# Take the tunnel down too. Otherwise the peer keeps routing this site's LAN to us while
		# the LAN's replies leave through the new master over Tailscale: an asymmetric path that
		# breaks TCP (states are if-bound). With the tunnel down the peer falls back as well.
		if wg_is_up; then ifconfig "$WG_IF" down && log "not CARP master: $WG_IF down"; fi
		echo 0 > "$STATE"
		return
	fi
	if ! wg_is_up; then ifconfig "$WG_IF" up && log "CARP master: $WG_IF up"; fi
	if peer_alive; then
		echo 0 > "$STATE"
		routes_up
	else
		f=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
		echo "$f" > "$STATE"
		if [ "$f" -ge 2 ]; then
			routes_down "peer $PEER_TUN not answering"
			[ "$f" -eq 2 ] || [ $(( f % 6 )) -eq 0 ] && relearn_endpoint
		fi
	fi
}

for i in 1 2 3 4 5; do
	check
	[ "$i" -lt 5 ] && sleep 10
done
