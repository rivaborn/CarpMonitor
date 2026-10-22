#!/bin/sh
# wan-carp-demote — fail the site over to the CARP backup while this node's WAN is down.
#
# CARP only fails over when the master stops advertising on the LAN, so a dead WAN on the
# master never moves the VIP (2026-10-22 FiOS outage). Run from cron every minute: after
# DOWN_TICKS consecutive "down" readings of $GW it adds a CARP demotion of $DEMOTION, and after
# UP_TICKS consecutive "online" readings it removes exactly what it added. Demotion is a
# delta shared with the carp-watchdog, so it never writes an absolute value.
#
# Before demoting it checks that the backup can reach the internet, by pinging $PROBE through
# the backup's LAN address on a temporary host route (the path LAN clients would take after a
# failover). If the backup is dead too it holds instead, and re-checks every minute: failing
# over to a dead backup only delays the fail-back once the WAN returns (2026-10-22 live test).
#
#   wan-carp-demote.sh            # one tick (cron)
#   wan-carp-demote.sh --dry-run  # one tick; logs the sysctl it would run, changes nothing
#   wan-carp-demote.sh --status   # print state and the current gateway status
#
# Test hook: a file $FORCE containing a status word (e.g. "down") overrides the real reading
# (a dry run reads $FORCE.dry-run instead). A dry run also honours WCD_BACKUP_LAN, so pointing
# it at an unused address exercises the "backup has no internet" path.

GW=WAN_DHCP
DOWN_TICKS=3
UP_TICKS=5
DEMOTION=240
BACKUP_LAN=192.168.1.2
PROBE=149.112.112.112  # Quad9 secondary: used by nothing else here, so the temporary route is harmless
STATE=/var/run/wan-carp-demote.state
FORCE=/tmp/wan-carp-demote.force
NTFY=http://192.168.1.30:2586/carp-watchdog-coachman
TAG=wan-carp-demote

DRY=0
# A dry run keeps its own state and force file, so a whole simulated outage can be stepped
# through without the cron run seeing it.
[ "$1" = "--dry-run" ] && { DRY=1; STATE=$STATE.dry-run; FORCE=$FORCE.dry-run; BACKUP_LAN=${WCD_BACKUP_LAN:-$BACKUP_LAN}; }

log() { logger -t "$TAG" "$*"; }

notify() {  # title, message, tags
	/usr/local/bin/curl -s -m 5 -o /dev/null -H "Title: $1" -H "Tags: $3" -H "Priority: 4" \
		-d "$2" "$NTFY" || log "ntfy POST failed: $1"
}

gw_status() {
	if [ -s "$FORCE" ]; then
		head -1 "$FORCE"
		return
	fi
	# Columns: Name Monitor Source Delay StdDev Loss Status Substatus (empty columns collapse).
	/usr/local/sbin/pfSsh.php playback gatewaystatus 2>/dev/null |
		awk -v g="$GW" '$1 == g { print $(NF-1); found = 1 } END { if (!found) print "missing" }'
}

backup_has_internet() {
	route -q delete -host "$PROBE" >/dev/null 2>&1
	route -q add -host "$PROBE" "$BACKUP_LAN" >/dev/null 2>&1 || return 1
	ping -c 3 -t 8 "$PROBE" >/dev/null 2>&1
	rc=$?
	route -q delete -host "$PROBE" >/dev/null 2>&1
	return $rc
}

boot=$(sysctl -n kern.boottime | sed -E 's/^\{ sec = ([0-9]+),.*/\1/')  # "{ sec = N, usec = M } ..."
applied=0; down=0; up=0; held=0
if [ -r "$STATE" ]; then
	. "$STATE"
	# A reboot resets net.inet.carp.demotion to 0, so a demotion recorded before it is gone.
	[ "$state_boot" = "$boot" ] || { applied=0; down=0; up=0; held=0; }
fi

status=$(gw_status)

if [ "$1" = "--status" ]; then
	echo "gateway $GW: $status"
	echo "applied=$applied down=$down up=$up held=$held demotion=$(sysctl -n net.inet.carp.demotion)"
	backup_has_internet && echo "backup $BACKUP_LAN: internet OK" || echo "backup $BACKUP_LAN: NO internet"
	exit 0
fi

case "$status" in
	down) down=$((down + 1)); up=0 ;;
	online) up=$((up + 1)); down=0; held=0 ;;
	*) down=0; up=0 ;;  # loss/delay/unknown: degraded, not dead; never act on it
esac

if [ "$applied" -eq 0 ] && [ "$down" -ge "$DOWN_TICKS" ] && ! backup_has_internet; then
	# Failing over would not help; hold, and try again next minute.
	if [ "$held" -eq 0 ]; then
		msg="$GW down for $down min, but the backup $BACKUP_LAN cannot reach the internet either: NOT failing over"
		log "$msg"
		[ "$DRY" -eq 1 ] || notify "WAN failover: held on $(hostname -s)" "$msg. Will fail over once the backup has internet." "no_entry,warning"
	fi
	held=1
elif [ "$applied" -eq 0 ] && [ "$down" -ge "$DOWN_TICKS" ]; then
	msg="$GW down for $down min: demoting CARP by $DEMOTION so the backup takes the gateway"
	held=0
	if [ "$DRY" -eq 1 ]; then
		applied=$DEMOTION
		log "[dry-run] would run: sysctl net.inet.carp.demotion=$DEMOTION ($msg)"
	elif sysctl net.inet.carp.demotion="$DEMOTION" >/dev/null; then
		applied=$DEMOTION
		log "$msg"
		notify "WAN failover: demoted $(hostname -s)" "$msg." "rotating_light"
	else
		log "ERROR: sysctl demotion failed"
	fi
elif [ "$applied" -gt 0 ] && [ "$up" -ge "$UP_TICKS" ]; then
	cur=$(sysctl -n net.inet.carp.demotion)
	undo=$applied
	[ "$DRY" -eq 0 ] && [ "$cur" -lt "$undo" ] && undo=$cur  # never drive the counter below 0
	msg="$GW online for $up min: removing the CARP demotion of $undo, this node takes the gateway back"
	if [ "$DRY" -eq 1 ]; then
		applied=0
		log "[dry-run] would run: sysctl net.inet.carp.demotion=-$undo ($msg)"
	elif [ "$undo" -le 0 ] || sysctl net.inet.carp.demotion="-$undo" >/dev/null; then
		applied=0
		log "$msg"
		notify "WAN failover: restored $(hostname -s)" "$msg." "white_check_mark"
	else
		log "ERROR: sysctl un-demotion failed"
	fi
fi

printf 'state_boot=%s\napplied=%s\ndown=%s\nup=%s\nheld=%s\n' "$boot" "$applied" "$down" "$up" "$held" > "$STATE.tmp" &&
	mv "$STATE.tmp" "$STATE"
[ "$DRY" -eq 1 ] && echo "status=$status down=$down up=$up applied=$applied held=$held (dry-run)"
exit 0
