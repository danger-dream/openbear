package fast

import (
	"fmt"
	"slices"
	"strconv"
	"strings"

	"openbear/node/mesh/bypass"
)

const (
	// Table is the standalone ip nftables table capture manages. Never
	// flushes or reloads fw4 or any other table.
	Table = "openbear_fast"
	// Mark tags UDP packets redirected by TPROXY into RouteTable.
	Mark = 0x4f420001
	// RulePref is the ip rule priority for Mark, right after bypass.RulePref (100)
	// and ahead of transparent proxies such as OpenClash.
	RulePref = 101
	// RouteTable is the local routing table ID (1..252 fits 8-bit rtm_table on BusyBox ip too).
	RouteTable = 242
	// The sets of fast ports whose channel is ready: mesh address . port.
	setTCP = "tcp_ready"
	setUDP = "udp_ready"
)

// NFTScript builds the atomic nftables transaction for openbear_fast: the
// connections to a fast port in the ready sets are taken, the rest go over
// the mesh. portLocalTCP is Listener A (output, 256 KB rcvbuf); portLANTCP is
// Listener B (prerouting, kernel auto-tuned); portUDP > 0 enables UDP TPROXY.
func NFTScript(portLocalTCP, portLANTCP, portUDP int, tcp, udp []string) string {
	gid := strconv.Itoa(bypass.GID)
	bmark := fmt.Sprintf("0x%08x", bypass.Mark)
	fmark := fmt.Sprintf("0x%08x", Mark)

	out := "table ip " + Table + "\ndelete table ip " + Table + "\ntable ip " + Table + " {\n" +
		"\tset " + setTCP + " {\n\t\ttype ipv4_addr . inet_service\n" + elements(tcp) + "\t}\n" +
		"\tset " + setUDP + " {\n\t\ttype ipv4_addr . inet_service\n" + elements(udp) + "\t}\n"
	if portLANTCP > 0 {
		out += "\tchain nat_pre {\n" +
			"\t\ttype nat hook prerouting priority -105; policy accept;\n" +
			"\t\tiifname \"lo\" return\n" +
			"\t\tmeta mark " + bmark + " return\n" +
			"\t\tip daddr . tcp dport @" + setTCP + " redirect to :" + strconv.Itoa(portLANTCP) + "\n" +
			"\t}\n"
	}
	if portLocalTCP > 0 {
		out += "\tchain nat_out {\n" +
			"\t\ttype nat hook output priority -105; policy accept;\n" +
			"\t\tmeta skgid " + gid + " return\n" +
			"\t\tmeta mark " + bmark + " return\n" +
			"\t\tip daddr . tcp dport @" + setTCP + " redirect to :" + strconv.Itoa(portLocalTCP) + "\n" +
			"\t}\n"
	}
	if portUDP > 0 {
		out += "\tchain mangle_pre {\n" +
			"\t\ttype filter hook prerouting priority -160; policy accept;\n" +
			"\t\tmeta mark " + bmark + " return\n" +
			"\t\tip daddr . udp dport @" + setUDP + " tproxy to 127.0.0.1:" + strconv.Itoa(portUDP) + " meta mark set " + fmark + " accept\n" +
			"\t}\n" +
			"\tchain route_out {\n" +
			"\t\ttype route hook output priority -55; policy accept;\n" +
			"\t\tmeta skgid " + gid + " return\n" +
			"\t\tmeta mark " + bmark + " return\n" +
			"\t\tip daddr . udp dport @" + setUDP + " meta mark set " + fmark + "\n" +
			"\t}\n"
	}
	out += "}\n"
	return out
}

// NFTSets is the transaction that makes the ready sets tcp and udp.
func NFTSets(tcp, udp []string) string {
	out := "flush set ip " + Table + " " + setTCP + "\nflush set ip " + Table + " " + setUDP + "\n"
	if len(tcp) > 0 {
		out += "add element ip " + Table + " " + setTCP + " { " + strings.Join(tcp, ", ") + " }\n"
	}
	if len(udp) > 0 {
		out += "add element ip " + Table + " " + setUDP + " { " + strings.Join(udp, ", ") + " }\n"
	}
	return out
}

func elements(list []string) string {
	if len(list) == 0 {
		return ""
	}
	return "\t\telements = { " + strings.Join(list, ", ") + " }\n"
}

// readyElements are keys as set elements, "addr . port", by protocol.
func readyElements(keys []targetKey) (tcp, udp []string) {
	for _, k := range keys {
		e := k.addr.String() + " . " + strconv.Itoa(int(k.port))
		if k.proto != protoUDP {
			tcp = append(tcp, e)
		}
		if k.proto != protoTCP {
			udp = append(udp, e)
		}
	}
	slices.Sort(tcp)
	slices.Sort(udp)
	return slices.Compact(tcp), slices.Compact(udp)
}
