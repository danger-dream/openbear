//go:build linux || darwin

package node

import (
	"testing"

	"golang.org/x/net/icmp"
	"golang.org/x/net/ipv4"
	"golang.org/x/net/ipv6"
)

func TestParseEchoResponse(t *testing.T) {
	marshal := func(typ icmp.Type, body icmp.MessageBody) []byte {
		b, err := (&icmp.Message{Type: typ, Body: body}).Marshal(nil)
		if err != nil {
			t.Fatal(err)
		}
		return b
	}
	request4 := marshal(ipv4.ICMPTypeEcho, &icmp.Echo{ID: 7, Seq: 258, Data: probePayload})
	request6 := marshal(ipv6.ICMPTypeEchoRequest, &icmp.Echo{ID: 7, Seq: 258, Data: probePayload})
	header4 := make([]byte, 20)
	header4[0], header4[9] = 0x45, 1
	header6 := make([]byte, 40)
	header6[0], header6[6] = 0x60, 58
	udp4 := append([]byte(nil), header4...)
	udp4[9] = 17
	for _, c := range []struct {
		name string
		v6   bool
		b    []byte
		kind replyKind
		ok   bool
	}{
		{"echo reply", false, marshal(ipv4.ICMPTypeEchoReply, &icmp.Echo{ID: 7, Seq: 258}), replyEcho, true},
		{"own request", false, request4, 0, false},
		{"time exceeded", false, marshal(ipv4.ICMPTypeTimeExceeded, &icmp.TimeExceeded{Data: append(header4, request4[:8]...)}), replyExceeded, true},
		{"unreachable", false, marshal(ipv4.ICMPTypeDestinationUnreachable, &icmp.DstUnreach{Data: append(header4, request4...)}), replyUnreachable, true},
		{"someone's udp", false, marshal(ipv4.ICMPTypeTimeExceeded, &icmp.TimeExceeded{Data: append(udp4, request4[:8]...)}), 0, false},
		{"echo reply v6", true, marshal(ipv6.ICMPTypeEchoReply, &icmp.Echo{ID: 7, Seq: 258}), replyEcho, true},
		{"time exceeded v6", true, marshal(ipv6.ICMPTypeTimeExceeded, &icmp.TimeExceeded{Data: append(header6, request6...)}), replyExceeded, true},
		{"truncated v6", true, marshal(ipv6.ICMPTypeTimeExceeded, &icmp.TimeExceeded{Data: header6}), 0, false},
	} {
		kind, id, seq, ok := parseEchoResponse(c.v6, c.b)
		if ok != c.ok || ok && (kind != c.kind || id != 7 || seq != 258) {
			t.Errorf("%s: got %v %d %d %v", c.name, kind, id, seq, ok)
		}
	}
}
