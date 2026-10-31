package node

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"testing"
	"time"
)

// Every IPv6 address package net sees must be in the netlink dump; which
// flags they carry depends on the machine.
func TestDDNSV6FlagsCoverTheMachinesAddresses(t *testing.T) {
	flags, err := ddnsV6Flags()
	if err != nil {
		t.Fatalf("netlink: %v", err)
	}
	list, err := net.Interfaces()
	if err != nil {
		t.Fatal(err)
	}
	seen := 0
	for _, ifi := range list {
		for _, addr := range ddnsIfaceAddrs(&ifi) {
			if !addr.Is6() {
				continue
			}
			seen++
			f, ok := flags[ddnsAddrKey{ifi.Index, addr}]
			if !ok {
				t.Errorf("%s %s is missing from the netlink dump", ifi.Name, addr)
			}
			t.Logf("%s %s temporary=%v deprecated=%v", ifi.Name, addr, f.temporary, f.deprecated)
		}
	}
	if seen == 0 {
		t.Log("this machine has no IPv6 address besides loopback")
	}
}

func TestDDNSURLBindsToADevice(t *testing.T) {
	s := echoServer(t, "tcp4", "127.0.0.1:0", func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "203.0.113.50") })
	addr, _, err := ddnsURL(context.Background(), []string{s.URL}, false, "lo")
	if errors.Is(err, syscall.EPERM) || err != nil && strings.Contains(err.(*ddnsError).detail, "operation not permitted") {
		t.Skip("SO_BINDTODEVICE needs CAP_NET_RAW here")
	}
	if err != nil || addr.String() != "203.0.113.50" {
		t.Fatalf("bound to lo: %v %v", addr, err)
	}
}

// ddnsNetnsSetup gives a fresh network namespace loopback and a dummy
// interface with a stable, a deprecated and (from mngtmpaddr) a temporary
// IPv6 address.
const ddnsNetnsSetup = `set -e
ip link set lo up
ip link add d0 type dummy
ip link set d0 up
sysctl -qw net.ipv6.conf.d0.use_tempaddr=2
ip addr add 198.51.100.60/24 dev d0
ip addr add 10.9.0.1/24 dev d0
ip addr add 2001:db8:1::1/64 dev d0 nodad mngtmpaddr valid_lft 3600 preferred_lft 1800
ip addr add 2001:db8:2::1/64 dev d0 nodad preferred_lft 0
ip addr add fd00:1::1/64 dev d0 nodad
ip -6 route add default dev d0
exec "$0" -test.run '^(TestDDNSNetnsFlags|TestDDNSURLOverIPv6|TestDDNSSTUNOverIPv6)$' -test.v -test.count=1
`

// TestDDNSInANetworkNamespace runs the IPv6 tests where the addresses and
// their flags are known; it needs root, as the namespace and the dummy
// interface do.
func TestDDNSInANetworkNamespace(t *testing.T) {
	if os.Getenv("OBLAB_DDNS_NETNS") != "" {
		t.Skip("already inside")
	}
	if os.Geteuid() != 0 {
		t.Skip("needs root")
	}
	if _, err := exec.LookPath("unshare"); err != nil {
		t.Skip("needs unshare")
	}
	if _, err := exec.LookPath("ip"); err != nil {
		t.Skip("needs ip")
	}
	cmd := exec.Command("unshare", "--net", "sh", "-c", ddnsNetnsSetup, os.Args[0])
	cmd.Env = append(os.Environ(), "OBLAB_DDNS_NETNS=1")
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("in the namespace: %v\n%s", err, out)
	}
	for _, name := range []string{"TestDDNSNetnsFlags", "TestDDNSURLOverIPv6", "TestDDNSSTUNOverIPv6"} {
		if !strings.Contains(string(out), "--- PASS: "+name) {
			t.Fatalf("%s did not pass in the namespace:\n%s", name, out)
		}
	}
}

func TestDDNSNetnsFlags(t *testing.T) {
	if os.Getenv("OBLAB_DDNS_NETNS") == "" {
		t.Skip("run by TestDDNSInANetworkNamespace")
	}
	ifi, err := net.InterfaceByName("d0")
	if err != nil {
		t.Fatal(err)
	}
	flags, err := ddnsV6Flags()
	if err != nil {
		t.Fatal(err)
	}
	var temporary netip.Addr
	for _, c := range ddnsV6Candidates(ifi, flags) {
		want := ddnsAddrFlags{deprecated: c.addr == netip.MustParseAddr("2001:db8:2::1")}
		if strings.HasPrefix(c.addr.String(), "2001:db8:1:0:") {
			want.temporary, temporary = true, c.addr
		}
		if c.temporary != want.temporary || c.deprecated != want.deprecated {
			t.Errorf("%s: temporary=%v deprecated=%v", c.addr, c.temporary, c.deprecated)
		}
	}
	if !temporary.IsValid() {
		t.Fatal("the kernel made no temporary address")
	}
	nic := func(sel ddnsSelectIn, allowPrivate bool) string {
		s, err := ddnsParseSelect(&sel)
		if err != nil {
			t.Fatal(err)
		}
		addr, iface, err := ddnsNIC(ddnsSpec{family: "v6", v6: true, method: "nic", sel: s, allowPrivate: allowPrivate})
		if err != nil {
			return err.Error()
		}
		if iface != "d0" {
			t.Fatalf("the default interface is %q", iface)
		}
		return addr.String()
	}
	if got := nic(ddnsSelectIn{}, false); got != "2001:db8:1::1" {
		t.Errorf("default rules: %s", got)
	}
	if got := nic(ddnsSelectIn{Mode: "prefix", Value: "fd00::/8"}, false); got != "no_address" {
		t.Errorf("ULA without allowPrivate: %s", got)
	}
	if got := nic(ddnsSelectIn{Mode: "prefix", Value: "fd00::/8"}, true); got != "fd00:1::1" {
		t.Errorf("ULA with allowPrivate: %s", got)
	}
	if got := nic(ddnsSelectIn{Mode: "prefix", Value: "2001:db8:2::/48"}, false); got != "no_address" {
		t.Errorf("a deprecated address was taken: %s", got)
	}
	if got := nic(ddnsSelectIn{Mode: "regex", Value: "^2001:db8:1:0:", AllowTemporary: true}, false); got != temporary.String() {
		t.Errorf("allowTemporary: %s", got)
	}
	if addr, _, err := ddnsNIC(ddnsSpec{family: "v4", method: "nic", iface: "d0"}); err != nil || addr.String() != "198.51.100.60" {
		t.Errorf("IPv4 public before private: %v %v", addr, err)
	}
	var d0 *ddnsIfaceInfo
	for _, i := range ddnsInterfaces() {
		if i.Name == "d0" {
			d0 = &i
		}
	}
	if d0 == nil || !d0.Default || len(d0.V4) != 2 {
		t.Fatalf("interfaces: %+v", d0)
	}
	marks := map[string]string{}
	for _, a := range d0.V6 {
		marks[a.Addr] = fmt.Sprintf("%s/%v/%v", a.Scope, a.Temporary, a.Deprecated)
	}
	for addr, want := range map[string]string{"2001:db8:1::1": "global/false/false", "2001:db8:2::1": "global/false/true",
		"fd00:1::1": "ula/false/false", temporary.String(): "global/true/false"} {
		if marks[addr] != want {
			t.Errorf("%s: %s, want %s", addr, marks[addr], want)
		}
	}
	// A dummy interface goes nowhere, so a socket bound to it cannot reach loopback.
	s := echoServer(t, "tcp4", "127.0.0.1:0", func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "203.0.113.50") })
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, _, err := ddnsURL(ctx, []string{s.URL}, false, "d0"); err == nil {
		t.Error("a request bound to d0 reached loopback")
	}
}
