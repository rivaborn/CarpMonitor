<?php
/*
 * Add the wg-s2s cron entry (every minute, lockf so runs never overlap). Idempotent.
 * The cron section is not part of HA sync, so the entry stays on this master.
 * Usage: php wg_cron.php [remove]
 */
require_once('config.inc');
require_once('services.inc');

$cmd = '/usr/bin/lockf -s -t 0 /var/run/wg-s2s.lock /bin/sh /conf/wg-s2s/wg-s2s.sh';
$items = config_get_path('cron/item', array());
$idx = null;
foreach ($items as $i => $it) { if (($it['command'] ?? '') === $cmd) { $idx = $i; } }

if (($argv[1] ?? '') === 'remove') {
	if ($idx === null) { echo "no wg-s2s cron entry\n"; exit(0); }
	unset($items[$idx]);
	config_set_path('cron/item', array_values($items));
	write_config('wg-s2s: cron entry removed');
	configure_cron();
	echo "wg-s2s cron entry removed\n";
	exit(0);
}
if ($idx !== null) { echo "wg-s2s cron entry exists\n"; exit(0); }
$items[] = array('minute' => '*', 'hour' => '*', 'mday' => '*', 'month' => '*', 'wday' => '*', 'who' => 'root', 'command' => $cmd);
config_set_path('cron/item', $items);
write_config('wg-s2s: run /conf/wg-s2s/wg-s2s.sh every minute');
configure_cron();
echo "wg-s2s cron entry added\n";
