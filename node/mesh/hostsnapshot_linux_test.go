package mesh

import (
	"errors"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func snapshotHost(t *testing.T, gateway bool) (*fakeHost, *linuxHost) {
	t.Helper()
	f := newFakeHost(t)
	f.ipt["filter"][dockerUser] = nil
	h := newLinuxHost("obmesh0", f.exec, func(name string) bool {
		return name != "firewall-cmd" && name != "busctl" && f.has(name)
	})
	h.forwardFile = filepath.Join(t.TempDir(), "ip_forward")
	if err := os.WriteFile(h.forwardFile, []byte("0\n"), 0600); err != nil {
		t.Fatal(err)
	}
	c := fwConfig
	c.TCP = 0 // the measured member: UDP and Docker, two rules each
	if gateway {
		c.Gateway = &Gateway{Subnets: []netip.Prefix{netip.MustParsePrefix("192.0.2.0/24")}, Masquerade: true, SNAT: true}
	}
	if err := h.apply(c); err != nil {
		t.Fatal(err)
	}
	f.calls = nil
	return f, h
}

func firewallWrites(calls [][]string) [][]string {
	var out [][]string
	for _, c := range calls {
		if c[0] == "iptables" && (slices.Contains(c, "-N") || slices.Contains(c, "-F") || slices.Contains(c, "-I") || slices.Contains(c, "-D") || slices.Contains(c, "-A")) || c[0] == "nft" && (c[1] == "insert" || c[1] == "add" || c[1] == "delete") {
			out = append(out, c)
		}
	}
	return out
}

func TestFirewallSnapshotHealthyQueries(t *testing.T) {
	for _, gateway := range []bool{false, true} {
		t.Run(map[bool]string{false: "member", true: "gateway"}[gateway], func(t *testing.T) {
			f, h := snapshotHost(t, gateway)
			// IPv4/inet user chains grow without growing the query count.
			f.addNFT("inet", "custom", "input", "input", "drop")
			f.addNFT("ip", "custom", "forward", "forward", "drop")
			// The existing policy excludes ip6-only chains, not inet chains.
			v6 := f.addNFT("ip6", "custom", "input", "input", "drop", "tcp dport 22 accept")
			h.heal()
			f.calls = nil
			before := 1 + 1 + 2 // report, nft chains, two nft per-chain reads
			for _, c := range h.chains {
				before += 2 + len(c.rules) // -S own, each -C, -S parent
			}
			want := 2
			if gateway {
				want = 4 // filter, nat, mangle and nft ruleset
			}
			for range 2 {
				f.calls = nil
				if fixed := h.heal(); len(fixed) != 0 || len(f.calls) != want {
					t.Fatalf("fixed=%v calls=%v", fixed, f.calls)
				}
				if writes := firewallWrites(f.calls); len(writes) > 0 {
					t.Fatalf("healthy firewall written: %v", writes)
				}
			}
			if !slices.Equal(texts(v6), []string{"tcp dport 22 accept"}) {
				t.Fatal("changed IPv6-only policy")
			}
			t.Logf("simulated healthy queries: old formula %d -> %d (includes two user nft chains)", before, want)
		})
	}
}

func TestFirewallSnapshotMeasuredMemberQueries(t *testing.T) {
	f, h := snapshotHost(t, false)
	if fixed := h.heal(); len(fixed) != 0 || len(f.calls) != 2 {
		t.Fatalf("%v: %v", fixed, f.calls)
	}
	t.Log("measured topology replay: 10 queries before -> 2 queries now (fake runner; not live CPU measurement)")
}

func TestFirewallSnapshotRepairsRulesAndJumps(t *testing.T) {
	for _, damage := range []string{"missing", "order", "duplicate-rule", "jump-order", "two-jumps", "three-jumps"} {
		t.Run(damage, func(t *testing.T) {
			f, h := snapshotHost(t, false)
			want := slices.Clone(f.ipt["filter"][chainIn])
			switch damage {
			case "missing":
				delete(f.ipt["filter"], chainIn)
			case "order":
				slices.Reverse(f.ipt["filter"][chainIn])
			case "duplicate-rule":
				f.ipt["filter"][chainIn][1] = want[0]
			case "jump-order":
				f.ipt["filter"]["INPUT"] = []string{"-j DROP", "-j " + chainIn}
			case "two-jumps":
				f.ipt["filter"]["INPUT"] = []string{"-j " + chainIn, "-j " + chainIn}
			case "three-jumps":
				f.ipt["filter"]["INPUT"] = []string{"-j " + chainIn, "-j " + chainIn, "-j " + chainIn}
			}
			if fixed := h.heal(); len(fixed) == 0 {
				t.Fatal("did not repair", damage)
			}
			if !slices.Equal(f.ipt["filter"][chainIn], want) {
				t.Fatal(f.ipt["filter"][chainIn])
			}
			jump := f.ipt["filter"]["INPUT"]
			if jump[0] != "-j "+chainIn || strings.Count(strings.Join(jump, "\n"), "-j "+chainIn) != 1 {
				t.Fatal(jump)
			}
			f.calls = nil
			if fixed := h.heal(); len(fixed) != 0 || len(firewallWrites(f.calls)) > 0 {
				t.Fatalf("repaired again: %v %v", fixed, f.calls)
			}
		})
	}
}

func TestFirewallSnapshotBackendAddresses(t *testing.T) {
	for _, backend := range []string{"legacy", "nf_tables"} {
		t.Run(backend, func(t *testing.T) {
			f, h := snapshotHost(t, true)
			f.versionV = "iptables v1.8.11 (" + backend + ")"
			// Both backends can render a host address with its /32 mask.
			h.x = func(args ...string) (string, error) {
				out, err := f.exec(args...)
				return strings.ReplaceAll(out, "--ctorigdst 100.96.0.13 ", "--ctorigdst 100.96.0.13/32 "), err
			}
			if fixed := h.heal(); len(fixed) > 0 || len(firewallWrites(f.calls)) > 0 {
				t.Fatalf("backend spelling repaired: %v %v", fixed, f.calls)
			}
		})
	}
}

func TestFirewallSnapshotQueryFallback(t *testing.T) {
	for _, malformed := range []bool{false, true} {
		t.Run(map[bool]string{false: "error", true: "malformed"}[malformed], func(t *testing.T) {
			f, h := snapshotHost(t, false)
			f.addNFT("inet", "custom", "input", "input", "drop")
			h.heal()
			f.calls = nil
			x := func(args ...string) (string, error) {
				if slices.Equal(args, []string{"iptables", "-w", "-t", "filter", "-S"}) || slices.Equal(args, []string{"nft", "-j", "-a", "list", "ruleset"}) {
					f.calls = append(f.calls, args)
					if malformed {
						return "not a snapshot", nil
					}
					return "", errors.New("query failed")
				}
				return f.exec(args...)
			}
			h.x, h.fw.nft.x = x, x
			if fixed := h.heal(); len(fixed) != 0 || len(firewallWrites(f.calls)) > 0 {
				t.Fatalf("query failure caused repair: %v %v", fixed, f.calls)
			}
			delete(f.ipt["filter"], chainIn)
			if fixed := h.heal(); len(fixed) == 0 {
				t.Fatal("fallback failed to repair missing chain")
			}
		})
	}
}

func TestFirewallSnapshotRechecksConcurrentRepair(t *testing.T) {
	f, h := snapshotHost(t, false)
	want := slices.Clone(f.ipt["filter"][chainIn])
	f.ipt["filter"][chainIn] = nil
	h.x = func(args ...string) (string, error) {
		out, err := f.exec(args...)
		if slices.Equal(args, []string{"iptables", "-w", "-t", "filter", "-S"}) {
			f.ipt["filter"][chainIn] = slices.Clone(want)
		}
		return out, err
	}
	if fixed := h.heal(); len(fixed) != 0 || len(firewallWrites(f.calls)) > 0 {
		t.Fatalf("used stale broken chain: %v %v", fixed, f.calls)
	}
	// A healthy snapshot also expires: next round must notice new damage.
	f.ipt["filter"][chainIn] = nil
	h.x = f.exec
	if fixed := h.heal(); len(fixed) == 0 {
		t.Fatal("snapshot reused across rounds")
	}
}

func TestNFTSnapshotRechecksPolicyAndHandles(t *testing.T) {
	for _, change := range []string{"repaired", "policy", "gone", "ban"} {
		t.Run(change, func(t *testing.T) {
			f, h := snapshotHost(t, false)
			c := f.addNFT("inet", "custom", "input", "input", "drop")
			h.heal()
			want := slices.Clone(c.rules)
			c.rules = nil
			f.calls = nil
			h.fw.nft.x = func(args ...string) (string, error) {
				out, err := f.exec(args...)
				if slices.Equal(args, []string{"nft", "-j", "-a", "list", "ruleset"}) {
					switch change {
					case "repaired":
						c.rules = slices.Clone(want)
					case "policy":
						c.c.Policy = "accept"
					case "gone":
						f.nft = nil
					case "ban":
						f.handle++
						c.rules = []fakeNFTRule{{handle: f.handle, text: "jump f2b-sshd"}}
					}
				}
				return out, err
			}
			h.heal()
			if change == "ban" {
				if len(c.rules) != len(want)+1 || c.rules[0].text != "jump f2b-sshd" {
					t.Fatalf("lost ban position: %v", texts(c))
				}
			} else if writes := firewallWrites(f.calls); len(writes) > 0 {
				t.Fatalf("stale nft snapshot written: %v", writes)
			}
		})
	}
}
