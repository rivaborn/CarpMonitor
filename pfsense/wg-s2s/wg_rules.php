<?php
/*
 * wg-s2s firewall rules. Usage: php wg_rules.php <remote LAN cidr|-> <local LAN cidr|->
 *   - WAN: pass UDP to this firewall's WAN address port 51820 from any (WireGuard drops anything not
 *     signed by a configured peer key; "any" keeps the rule valid when the peer's DHCP address changes).
 *   - WireGuard interface group (only when LAN args given): pass remote LAN -> local LAN, and the tunnel net.
 *     On a CARP backup without the package the group does not exist and pfSense comments these out.
 * Idempotent: rules are matched by description. Applies the filter once at the end.
 */
require_once('config.inc');
require_once('filter.inc');
require_once("util.inc");
require_once("shaper.inc");

$remote = $argv[1] ?? '-';
$local  = $argv[2] ?? '-';

$want = array();
$want[] = array(
	'type' => 'pass', 'interface' => 'wan', 'ipprotocol' => 'inet', 'protocol' => 'udp',
	'source' => array('any' => ''),
	'destination' => array('network' => 'wanip', 'port' => '51820'),
	'descr' => 'wg-s2s: WireGuard site-to-site listener (peer keys authenticate)',
);
// Keep Tailscale's own transport out of the WireGuard tunnel: tailscaled offers every local address
// as an endpoint and otherwise moves the router-to-router Tailscale path onto 10.255.255.x, so the
// fallback would die with the tunnel it backs up. First rule on the group, ahead of any pass rule.
$want[] = array(
	'type' => 'block', 'interface' => 'WireGuard', 'ipprotocol' => 'inet', 'protocol' => 'udp',
	'source' => array('address' => '10.255.255.0/30'),
	'destination' => array('address' => '10.255.255.0/30', 'port' => '41641'),
	'descr' => 'wg-s2s: keep Tailscale transport off the WireGuard tunnel',
);
// The inbound block alone is not enough: both sides send, so each side's outbound state admits the
// other's packets. Block them leaving as well (floating, outbound, quick).
$want[] = array(
	'type' => 'block', 'interface' => 'WireGuard', 'ipprotocol' => 'inet', 'protocol' => 'udp',
	'floating' => 'yes', 'direction' => 'out', 'quick' => 'yes',
	'source' => array('address' => '10.255.255.0/30'),
	'destination' => array('address' => '10.255.255.0/30', 'port' => '41641'),
	'descr' => 'wg-s2s: keep Tailscale transport off the WireGuard tunnel (outbound)',
);
if ($remote !== '-' && $local !== '-') {
	$want[] = array(
		'type' => 'pass', 'interface' => 'WireGuard', 'ipprotocol' => 'inet', 'protocol' => '',
		'source' => array('address' => $remote),
		'destination' => array('address' => $local),
		'descr' => 'wg-s2s: remote site LAN to this LAN over WireGuard',
	);
	$want[] = array(
		'type' => 'pass', 'interface' => 'WireGuard', 'ipprotocol' => 'inet', 'protocol' => '',
		'source' => array('address' => '10.255.255.0/30'),
		'destination' => array('any' => ''),
		'descr' => 'wg-s2s: tunnel net (health checks, iperf)',
	);
}

$rules = config_get_path('filter/rule', array());
$have = array();
foreach ($rules as $r) { $have[$r['descr'] ?? ''] = true; }

$added = 0;
$tracker = (int)(microtime(true) * 1000) % 2000000000;
foreach (array_reverse($want) as $w) {
	if (isset($have[$w['descr']])) { echo "exists: {$w['descr']}\n"; continue; }
	if ($w['protocol'] === '') { unset($w['protocol']); }
	$w['tracker'] = $tracker++;
	$w['created'] = make_config_revision_entry(null, 'wg-s2s');
	array_unshift($rules, $w);   // top of the list; per-interface order only matters within an interface
	echo "added: {$w['descr']}\n";
	$added++;
}
if ($added) {
	config_set_path('filter/rule', $rules);
	write_config("wg-s2s: {$added} WireGuard site-to-site firewall rule(s)");
	filter_configure_sync();
	echo "filter reloaded\n";
}
