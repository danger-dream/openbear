package mesh

import (
	"encoding/hex"
	"fmt"
	"log"
	"net/netip"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.zx2c4.com/wireguard/device"

	"openbear/node/mesh/transport"
)

// wgLog passes on at most one message per format string a minute. Under
// backpressure every refused batch is an error, and on Windows and macOS
// mesh.log is not rotated while the service runs.
type wgLog struct {
	mu         sync.Mutex
	last       map[string]time.Time
	suppressed map[string]int
	out        func(string)
}

func newWGLog(out func(string)) *wgLog {
	return &wgLog{last: map[string]time.Time{}, suppressed: map[string]int{}, out: out}
}

func (l *wgLog) errorf(format string, args ...any) {
	msg := fmt.Sprintf(format, args...)
	// Peers that dial in have no address until they do, and keep
	// trying to handshake when they have a keepalive; a peer whose last
	// path was released (a cold candidate after its check) the same.
	if strings.HasSuffix(msg, "no known endpoint for peer") || strings.HasSuffix(msg, transport.ErrNoPath.Error()) {
		return
	}
	l.mu.Lock()
	now := time.Now()
	if now.Sub(l.last[format]) < time.Minute {
		l.suppressed[format]++
		l.mu.Unlock()
		return
	}
	l.last[format] = now
	n := l.suppressed[format]
	delete(l.suppressed, format)
	l.mu.Unlock()
	if n > 0 {
		msg += fmt.Sprintf(" (%d similar not shown since the last one)", n)
	}
	l.out(msg)
}

func newDevice(t *meshTUN, b meshBind) *device.Device {
	l := newWGLog(func(msg string) { log.Print("wireguard: " + msg) })
	logger := &device.Logger{Verbosef: device.DiscardLogf, Errorf: l.errorf}
	dev := device.NewDevice(t, b, logger)
	// Before any peer exists: a configured endpoint stays fixed, so a late
	// UDP packet cannot move a peer off its chosen transport, while peers
	// without one still learn where their packets come from.
	dev.DisableSomeRoamingForBrokenMobileSemantics()
	return dev
}

// peerDiff turns a change of the peer list into UAPI that leaves untouched
// peers, and their sessions, alone. wire maps keys to the endpoints given to
// the bind; released lists endpoints no peer uses any more.
func peerDiff(old, cur []peerSpec, wire map[string]string) (uapi string, released []string) {
	before := map[string]peerSpec{}
	for _, p := range old {
		before[p.Key] = p
	}
	var removals, body strings.Builder
	seen := map[string]bool{}
	for _, p := range cur {
		seen[p.Key] = true
		o, existed := before[p.Key]
		if existed && (o.Endpoint == "") != (p.Endpoint == "") {
			// Only a new peer takes up a different roaming policy.
			fmt.Fprintf(&removals, "public_key=%s\nremove=true\n", p.Key)
			if o.Endpoint != "" {
				released = append(released, wire[o.Key+"@"+o.Endpoint])
			}
			existed = false
		}
		var lines strings.Builder
		if !existed {
			lines.WriteString("replace_allowed_ips=true\n")
			for _, a := range p.Allowed {
				fmt.Fprintf(&lines, "allowed_ip=%s\n", a)
			}
		} else {
			for _, a := range o.Allowed {
				if !slices.Contains(p.Allowed, a) {
					fmt.Fprintf(&lines, "allowed_ip=-%s\n", a)
				}
			}
			for _, a := range p.Allowed {
				if !slices.Contains(o.Allowed, a) {
					fmt.Fprintf(&lines, "allowed_ip=%s\n", a)
				}
			}
		}
		if p.Endpoint != "" && (!existed || o.Endpoint != p.Endpoint) {
			fmt.Fprintf(&lines, "endpoint=%s\n", wire[p.Key+"@"+p.Endpoint])
			if existed {
				released = append(released, wire[o.Key+"@"+o.Endpoint])
			}
		}
		if !existed || o.keepalive() != p.keepalive() {
			fmt.Fprintf(&lines, "persistent_keepalive_interval=%d\n", p.keepalive())
		}
		if lines.Len() > 0 {
			fmt.Fprintf(&body, "public_key=%s\n%s", p.Key, lines.String())
		}
	}
	// Removals go first so an address can move to another peer in one set.
	for _, o := range old {
		if !seen[o.Key] {
			fmt.Fprintf(&removals, "public_key=%s\nremove=true\n", o.Key)
			if o.Endpoint != "" {
				released = append(released, wire[o.Key+"@"+o.Endpoint])
			}
		}
	}
	return removals.String() + body.String(), released
}

// peerState is what the device reports about one peer.
type peerState struct {
	endpoint      string
	handshakeNano int64
	rx, tx        uint64
	allowed       []netip.Prefix
	keepalive     int
}

func deviceState(dev *device.Device) (listenPort int, peers map[string]*peerState) {
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

// kick starts a handshake now instead of on the first packet or keepalive.
func kick(dev *device.Device, keyHex string) {
	raw, err := hex.DecodeString(keyHex)
	if err != nil || len(raw) != device.NoisePublicKeySize {
		return
	}
	if peer := dev.LookupPeer(device.NoisePublicKey(raw)); peer != nil {
		peer.SendHandshakeInitiation(false)
	}
}
