#!/bin/sh
# Runs TestHostFirewallContainer in a throwaway privileged container: its own
# network namespace and firewall, nothing of this machine's. The container is
# removed however the test ends.
#   hostfw_container.sh firewalld [nobus] [nosbin]   rockylinux:9, firewalld --nofork over dbus
#   hostfw_container.sh nftables [nosbin]            debian:bookworm-slim, a pure nftables ruleset
# nobus: firewall-cmd alone, without busctl. nosbin: as a node under systemd on
# a system keeping its programs in /sbin: PATH has none of them, they are
# copied to /oblab-sbin, the one directory the test lets lookCmd search.
# Build the test binary first, static: CGO_ENABLED=0 go test -c -o /tmp/oblab-hostfw.test ./mesh
set -eu
kind=$1
shift
bin=${OBLAB_HOSTFW_BIN:-/tmp/oblab-hostfw.test}
name=oblab-hostfw-$kind-$$
case $kind in
firewalld)
	image=rockylinux:9
	setup='dnf -y -q install firewalld iproute socat iptables-nft procps-ng systemd dbus-daemon >/dev/null &&
		mkdir -p /run/dbus && dbus-daemon --system --fork &&
		(firewalld --nofork --nopid >/tmp/firewalld.log 2>&1 &) &&
		for i in $(seq 50); do firewall-cmd --state >/dev/null 2>&1 && break; sleep 0.2; done && firewall-cmd --state'
	;;
nftables)
	image=debian:bookworm-slim
	setup='apt-get -qq update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get -qq install -y nftables iproute2 socat iptables procps >/dev/null'
	;;
*)
	echo "usage: $0 firewalld|nftables [nobus] [nosbin]" >&2
	exit 2
	;;
esac
nobus= path=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin sbin=
for flag in "$@"; do
	case $flag in
	nobus) nobus=1 ;;
	nosbin) path=/nonexistent sbin=/oblab-sbin ;;
	*) echo "unknown flag $flag" >&2; exit 2 ;;
	esac
done
# OBLAB_SUB: one subtest only, e.g. fail2ban.
run='^TestHostFirewallContainer$'
[ -n "${OBLAB_SUB:-}" ] && run="$run/^$OBLAB_SUB\$"
docker run -d --rm --privileged --name "$name" -v "$bin:/oblab-hostfw.test:ro" "$image" sleep 3600 >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1' EXIT
docker exec "$name" sh -c "$setup"
if [ -n "$sbin" ]; then
	# Copies, links followed: iptables is a multi-call program going by its own name.
	docker exec "$name" sh -c 'mkdir -p /oblab-sbin && for t in ip iptables iptables-restore iptables-legacy nft sysctl socat env firewall-cmd firewalld busctl pkill pgrep; do
		p=$(command -v $t) && cp -L "$p" /oblab-sbin/$t; done; echo "in /oblab-sbin:" $(ls /oblab-sbin)'
fi
docker exec -e PATH="$path" -e OBLAB_HOSTFW="$kind" -e OBLAB_HOSTFW_NOBUS="$nobus" -e OBLAB_HOSTFW_SBIN="$sbin" "$name" \
	/oblab-hostfw.test -test.run "$run" -test.v -test.count=1
