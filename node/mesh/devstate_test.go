package mesh

import (
	"net/netip"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"golang.zx2c4.com/wireguard/device"
)

// deviceStateText is how deviceState read the device before Stats: IpcGet's
// text, line by line.
func deviceStateText(dev *device.Device) (listenPort int, peers map[string]*peerState) {
	peers = map[string]*peerState{}
	text, err := dev.IpcGet()
	if err != nil {
		return 0, peers
	}
	var cur *peerState
	var sec int64
	for _, line := range strings.Split(text, "\n") {
		k, v, _ := strings.Cut(line, "=")
		switch k {
		case "listen_port":
			listenPort, _ = strconv.Atoi(v)
		case "public_key":
			cur = &peerState{}
			peers[v] = cur
		case "endpoint":
			cur.endpoint = v
		case "last_handshake_time_sec":
			sec, _ = strconv.ParseInt(v, 10, 64)
		case "last_handshake_time_nsec":
			n, _ := strconv.ParseInt(v, 10, 64)
			if sec > 0 {
				cur.handshakeNano = sec*1e9 + n
			}
		case "rx_bytes":
			cur.rx, _ = strconv.ParseUint(v, 10, 64)
		case "tx_bytes":
			cur.tx, _ = strconv.ParseUint(v, 10, 64)
		case "persistent_keepalive_interval":
			cur.keepalive, _ = strconv.Atoi(v)
		case "allowed_ip":
			if p, err := netip.ParsePrefix(v); err == nil {
				cur.allowed = append(cur.allowed, p)
			}
		}
	}
	return listenPort, peers
}

// TestDeviceStateMatchesIpcGet: on relays and members that shook hands and
// carry their probes, deviceState reads what IpcGet writes.
func TestDeviceStateMatchesIpcGet(t *testing.T) {
	l := newLab(t, spec{relays: []string{"R1", "R2"}, members: map[string][]string{"A": {"R1", "R2"}}})
	nodes := l.up()
	for name, n := range nodes {
		for try := 0; ; try++ {
			port, before := deviceStateText(n.dev)
			gotPort, got := deviceState(n.dev)
			_, after := deviceStateText(n.dev)
			if !reflect.DeepEqual(before, after) {
				if try < 100 {
					continue // a packet came between the reads
				}
				t.Fatalf("%s: the device never held still", name)
			}
			if gotPort != port || !reflect.DeepEqual(got, before) {
				t.Fatalf("%s: deviceState port %d %+v, IpcGet port %d %+v", name, gotPort, got, port, before)
			}
			busy := false
			for _, p := range got {
				busy = busy || p.handshakeNano > 0 && p.rx > 0 && len(p.allowed) > 0
			}
			if len(got) == 0 || !busy {
				t.Fatalf("%s: nothing to compare: %+v", name, got)
			}
			break
		}
	}
}
