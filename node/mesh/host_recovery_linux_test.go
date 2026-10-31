package mesh

import (
	"errors"
	"net/netip"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func TestHostPartialFailureRestoresActualChanges(t *testing.T) {
	for _, fail := range []string{"mtu", "route", "firewall"} {
		t.Run(fail, func(t *testing.T) {
			f := newFakeHost(t)
			armed := false
			x := func(args ...string) (string, error) {
				command := strings.Join(args, " ")
				if armed && (fail == "mtu" && strings.Contains(command, "mtu 1300") || fail == "route" && strings.Contains(command, "route replace 10.2.0.0/24") || fail == "firewall" && strings.Contains(command, "-A OBMESH-IN") && strings.Contains(command, "51999")) {
					armed = false
					return "", errors.New("injected partial failure")
				}
				return f.exec(args...)
			}
			h := newLinuxHost("obmesh0", x, f.has)
			prev := fwConfig
			prev.Routes = []netip.Prefix{prev.Prefix}
			if err := h.apply(prev); err != nil {
				t.Fatal(err)
			}
			next := prev
			next.Address = netip.MustParseAddr("100.96.0.14")
			next.MTU = 1300
			next.UDP = 51999
			next.Routes = append(slices.Clone(prev.Routes), netip.MustParsePrefix("10.1.0.0/24"), netip.MustParsePrefix("10.2.0.0/24"))
			armed = true
			if err := h.apply(next); err == nil {
				t.Fatal("failure not injected")
			}
			f.calls = nil
			if err := h.apply(prev); err != nil {
				t.Fatal(err)
			}
			if !h.cur.equal(prev) || h.dirty || len(h.staleAddresses) != 0 {
				t.Fatal("host state not restored")
			}
			if !f.ran("ip", "-4", "address", "replace", prev.Address.String()+"/32") || !f.ran("ip", "-4", "address", "del", next.Address.String()+"/32") {
				t.Fatal("address rollback skipped")
			}
			if fail != "mtu" && !f.ran("ip", "-4", "route", "del", "10.1.0.0/24") {
				t.Fatal("partially added route not removed")
			}
			if !slices.Equal(f.ipt["filter"][chainIn], []string{"-i obmesh0 -j ACCEPT", "-p udp -m udp --dport 51820 -j ACCEPT", "-p tcp -m tcp --dport 443 -j ACCEPT"}) {
				t.Fatal("partial chain not restored", f.ipt["filter"][chainIn])
			}
		})
	}
}

func TestFirewalldOwnershipSurvivesCrashAndZoneChange(t *testing.T) {
	f := newFakeHost(t)
	f.fdOn = true
	f.ports["public"] = map[string]bool{"443/tcp": true}
	path := filepath.Join(t.TempDir(), "firewalld-owned.json")
	makeHost := func() *linuxHost { h := newLinuxHost("obmesh0", f.exec, f.has); h.fw.fd.stateFile = path; return h }
	h := makeHost()
	if err := h.apply(fwConfig); err != nil {
		t.Fatal(err)
	}
	h = makeHost()
	next := fwConfig
	next.UDP = 51999
	if err := h.apply(next); err != nil {
		t.Fatal(err)
	}
	if f.ports["public"]["51820/udp"] || !f.ports["public"]["51999/udp"] {
		t.Fatal("old ownership not recovered", f.ports)
	}
	f.zones["eth0"] = "work"
	if err := h.apply(next); err != nil {
		t.Fatal(err)
	}
	if f.ports["public"]["51999/udp"] || !f.ports["work"]["51999/udp"] {
		t.Fatal("old zone retained", f.ports)
	}
	h = makeHost()
	h.close()
	if f.zones["obmesh0"] != "" || f.ports["work"]["51999/udp"] || !f.ports["public"]["443/tcp"] {
		t.Fatal("cleanup lost ownership or removed user rule", f.zones, f.ports)
	}
}

func TestNFTGatewayRulesRemainSubnetScopedAndAfterBans(t *testing.T) {
	f := newFakeHost(t)
	c := f.addNFT("inet", "filter", "forward", "forward", "drop", "ip saddr @blocked drop")
	h := newLinuxHost("obmesh0", f.exec, f.has)
	h.forwardFile = filepath.Join(t.TempDir(), "ip_forward")
	write(t, filepath.Dir(h.forwardFile), "ip_forward", "1")
	next := fwConfig
	next.Gateway = &Gateway{Subnets: []netip.Prefix{netip.MustParsePrefix("10.98.0.0/24")}}
	if err := h.apply(next); err != nil {
		t.Fatal(err)
	}
	rules := texts(c)
	if rules[0] != "ip saddr @blocked drop" || !slices.Contains(rules, `iifname "obmesh0" ip daddr 10.98.0.0/24 accept`) || !slices.Contains(rules, `oifname "obmesh0" ip saddr 10.98.0.0/24 accept`) {
		t.Fatal(rules)
	}
	if slices.Contains(rules, `iifname "obmesh0" accept`) {
		t.Fatal("global forwarding accepted")
	}
	next.Gateway = nil
	if err := h.apply(next); err != nil {
		t.Fatal(err)
	}
	for _, rule := range texts(c) {
		if strings.Contains(rule, "10.98.0.0/24") {
			t.Fatal("revoked subnet left")
		}
	}
}
