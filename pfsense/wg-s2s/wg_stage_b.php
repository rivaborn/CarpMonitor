<?php
/*
 * wg-s2s stage B: add the remote master as a peer of the site-to-site tunnel, enable the
 * WireGuard service and start it. Idempotent (an existing peer with the same public key is kept).
 * Usage: php wg_stage_b.php <local tun> <peer pubkey> <peer endpoint ip> <peer tunnel ip> <remote LAN> <remote LAN mask> <descr>
 * The package adds no routes for AllowedIPs: routing is done by wg-s2s.sh.
 */
[$self, $tun, $pub, $endpoint, $peer_tun_ip, $lan, $lanmask, $descr] = $argv;
$argv = array($self);   // wg_service.inc runs its entry point when $argv[1] is set
$argc = 1;

require_once('functions.inc');
require_once('wireguard/includes/wg.inc');
global $wgg;
wg_globals();

$have = false;
foreach (config_get_path('installedpackages/wireguard/peers/item', []) as $p) {
	if (($p['publickey'] ?? '') === $pub && ($p['tun'] ?? '') === $tun) { $have = true; }
}
if (!$have) {
	$res = wg_do_peer_post(array(
		'enabled'             => 'yes',
		'tun'                 => $tun,
		'descr'               => $descr,
		'endpoint'            => $endpoint,
		'port'                => '51820',
		'persistentkeepalive' => '25',
		'publickey'           => $pub,
		'presharedkey'        => '',
		'address0'            => $peer_tun_ip,  'address_subnet0' => '32', 'address_descr0' => 'peer tunnel address',
		'address1'            => $lan,          'address_subnet1' => $lanmask, 'address_descr1' => 'remote LAN',
	));
	if (!empty($res['input_errors'])) { echo "PEER ERRORS:\n"; print_r($res['input_errors']); exit(1); }
	echo "peer added on {$tun}\n";
} else {
	echo "peer already present on {$tun}\n";
}

$s = config_get_path('installedpackages/wireguard/config/0', []);
if (($s['enable'] ?? 'off') !== 'on') {
	$s['enable'] = 'yes';   // the settings post maps 'yes' to 'on'
	$res = wg_do_settings_post($s);
	if (!empty($res['input_errors'])) { echo "SETTINGS ERRORS:\n"; print_r($res['input_errors']); exit(1); }
	echo "service enabled in settings\n";
}

$r = wg_service_cli_restart(false);
echo "service restart: ", is_array($r) ? json_encode($r) : var_export($r, true), "\n";
