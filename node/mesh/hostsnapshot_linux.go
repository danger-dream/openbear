package mesh

import (
	"encoding/json"
	"net/netip"
	"slices"
	"strings"
)

// iptablesSnapshot is one table's -S output, used only by one heal. It is
// never restored: repairs still use individual commands on our own chains.
type iptablesSnapshot struct {
	raw    string
	chains map[string][]string
}

func readIPTables(x execFn, table string) *iptablesSnapshot {
	out, err := x("iptables", "-w", "-t", table, "-S")
	if err != nil {
		return nil
	}
	s := &iptablesSnapshot{raw: out, chains: map[string][]string{}}
	for _, l := range strings.Split(strings.TrimSpace(out), "\n") {
		f := strings.Fields(l)
		if len(f) < 2 {
			return nil
		}
		switch f[0] {
		case "-N", "-P":
			s.chains[f[1]] = []string{l}
		case "-A":
			if _, ok := s.chains[f[1]]; !ok {
				return nil
			}
			s.chains[f[1]] = append(s.chains[f[1]], l)
		default:
			return nil
		}
	}
	return s
}

// ruleWords normalizes the address spellings emitted by both iptables
// backends. Everything else, including rule order and match options, counts.
func ruleWords(line string) []string {
	f := strings.Fields(line)
	for i := 0; i+1 < len(f); i++ {
		switch f[i] {
		case "-s", "-d", "--ctorigdst":
			if a, err := netip.ParseAddr(f[i+1]); err == nil {
				f[i+1] = netip.PrefixFrom(a, a.BitLen()).String()
			} else if p, err := netip.ParsePrefix(f[i+1]); err == nil {
				f[i+1] = p.Masked().String()
			}
		}
	}
	return f
}

func chainIntact(lines []string, name string, c chain) bool {
	if len(lines) != len(c.rules)+1 {
		return false
	}
	for i, r := range c.rules {
		if !slices.Equal(ruleWords(lines[i+1]), ruleWords("-A "+name+" "+strings.Join(r, " "))) {
			return false
		}
	}
	return true
}

func jumpPosition(lines []string, name, parent string) (at, pos int) {
	at = -1
	count := 0
	var others []string
	for i, l := range lines {
		if i == 0 { // chain declaration
			continue
		}
		if l == "-A "+parent+" -j "+name {
			count++
			at = i - 1
		} else {
			others = append(others, l)
		}
	}
	if count > 1 {
		at = -2
	}
	return at, jumpAt(others)
}

type nftChainKey struct{ family, table, name string }

func (c nftChain) key() nftChainKey { return nftChainKey{c.Family, c.Table, c.Name} }

// readNFT reads the rules and chain policies together, with handles for
// repairs. A caller must re-read a damaged chain before changing it.
func readNFT(x execFn) ([]nftChain, map[nftChainKey][]nftRule, bool) {
	out, err := x("nft", "-j", "-a", "list", "ruleset")
	if err != nil {
		return nil, nil, false
	}
	var doc struct {
		Items []struct {
			Chain *nftChain `json:"chain"`
			Rule  *struct {
				nftRule
				Family string `json:"family"`
				Table  string `json:"table"`
				Chain  string `json:"chain"`
			} `json:"rule"`
		} `json:"nftables"`
	}
	if json.Unmarshal([]byte(out), &doc) != nil || doc.Items == nil {
		return nil, nil, false
	}
	var chains []nftChain
	rules := map[nftChainKey][]nftRule{}
	for _, it := range doc.Items {
		if it.Chain != nil {
			chains = append(chains, *it.Chain)
		}
		if r := it.Rule; r != nil {
			k := nftChainKey{r.Family, r.Table, r.Chain}
			rules[k] = append(rules[k], r.nftRule)
		}
	}
	return chains, rules, true
}
