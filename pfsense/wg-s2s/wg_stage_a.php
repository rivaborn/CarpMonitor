<?php
/*
 * wg-s2s stage A: create the site-to-site tunnel on this pfSense master (no peer yet).
 * Usage: php wg_stage_a.php <tunnel address> <description>
 * Idempotent: an existing tunnel with the same description is reported, not recreated.
 * Prints the tunnel's PUBLIC key only; the private key never leaves the router.
 *
 * Before the first run, check for tunnels left over from an earlier WireGuard install (the package
 * keeps its config when removed, "keep_conf"). Any still marked enabled come back up as soon as the
 * service is enabled in stage B; disable them first (on the Coachman master, 2026-11-16, an old
 * road-warrior tunnel on tun_wg0 was set to disabled, so this tunnel became tun_wg1).
 */
// wg_service.inc runs its service entry point when the including script has arguments
// (isset($argv[1])), so take ours first and clear them before loading the package.
$addr = $argv[1];
$descr = $argv[2];
$argv = array($argv[0]);
$argc = 1;

require_once('functions.inc');
require_once('wireguard/includes/wg.inc');
global $wgg;
wg_globals();

foreach (config_get_path('installedpackages/wireguard/tunnels/item', []) as $i => $t) {
	if (($t['descr'] ?? '') !== $descr && ($t['enabled'] ?? '') === 'yes') {
		echo "WARNING: another tunnel {$t['name']} ({$t['descr']}) is enabled and will start with the service\n";
	}
	if (($t['descr'] ?? '') === $descr) {
		echo "exists: {$t['name']} pub {$t['publickey']}\n";
		exit(0);
	}
}

$kp = wg_gen_keypair();
$post = array(
	'enabled'        => 'yes',
	'descr'          => $descr,
	'listenport'     => '51820',
	'privatekey'     => $kp['privkey'],
	'mtu'            => '1420',
	'address0'       => $addr,
	'address_subnet0'=> '30',
	'address_descr0' => 'wg-s2s tunnel net (10.255.255.0/30)',
);
$res = wg_do_tunnel_post($post);
if (!empty($res['input_errors'])) {
	echo "ERRORS:\n";
	print_r($res['input_errors']);
	exit(1);
}
echo "created: {$res['pconfig']['name']} port {$res['pconfig']['listenport']} pub {$res['pconfig']['publickey']}\n";
