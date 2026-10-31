package health

import (
	"crypto/ecdh"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"net"
	"net/netip"
	"testing"
	"time"

	"openbear/node/mesh/transport"
)

func testKey(t *testing.T) (string, string) {
	t.Helper()
	k, e := ecdh.X25519().GenerateKey(rand.Reader)
	if e != nil {
		t.Fatal(e)
	}
	return base64.StdEncoding.EncodeToString(k.Bytes()), base64.StdEncoding.EncodeToString(k.PublicKey().Bytes())
}
func testPort(t *testing.T) int {
	t.Helper()
	c, e := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	return c.LocalAddr().(*net.UDPAddr).Port
}
func waitHealth(t *testing.T, d time.Duration, f func() bool) {
	t.Helper()
	end := time.Now().Add(d)
	for time.Now().Before(end) {
		if f() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("health condition timed out")
}

func TestEncryptedControlPrepareCommitAndHotFailover(t *testing.T) {
	type local struct {
		cfg  Config
		key  string
		bind *transport.Bind
		ctl  *Control
		m    *Manager
	}
	nodes := map[string]*local{}
	endpoints := map[string]string{}
	var auth []Node
	for i, id := range []string{"A", "B", "Z", "M", "N"} {
		key, pub := testKey(t)
		_, data := testKey(t)
		role := "relay"
		if i >= 3 {
			role = "member"
		}
		n := Node{ID: id, Role: role, PublicKey: data, ControlKey: pub, Address: netip.MustParseAddr(fmt.Sprintf("100.96.255.%d", i+1))}
		if role == "member" {
			n.Relays = []string{"A", "B", "Z"}
		}
		auth = append(auth, n)
		cfg := Config{Network: "test", Self: id, Role: role, Revision: 1, Address: n.Address, MTU: 1400, Mode: "failover", Parameters: Defaults()}
		nodes[id] = &local{cfg: cfg, key: key}
	}
	for _, id := range []string{"A", "B", "Z", "M", "N"} {
		n := nodes[id]
		port := 0
		if n.cfg.Role == "relay" {
			port = testPort(t)
		}
		b, err := transport.New(transport.Options{ListenUDP: port})
		if err != nil {
			t.Fatal(err)
		}
		n.bind = b
		if _, _, err = b.Open(0); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { b.Close() })
		if port != 0 {
			endpoints[id] = fmt.Sprintf("udp://127.0.0.1:%d", port)
		}
	}
	for _, id := range []string{"A", "B", "Z", "M", "N"} {
		n := nodes[id]
		var links []Link
		for _, p := range auth {
			if p.ID != id {
				n.cfg.Nodes = append(n.cfg.Nodes, p)
			}
			if p.Role == "relay" && p.ID != id {
				endpoint := endpoints[p.ID]
				if n.cfg.Role == "relay" {
					links = append(links, Link{Peer: p.ID, Dial: id < p.ID, Endpoint: endpoint})
				} else {
					n.cfg.Candidates = append(n.cfg.Candidates, Candidate{ID: p.ID, Peer: p.ID, Endpoint: endpoint})
				}
			}
		}
		if n.cfg.Role == "relay" {
			n.cfg.Links = links
		}
	}
	for _, id := range []string{"A", "B", "Z", "M", "N"} {
		n := nodes[id]
		c, err := NewControl(n.cfg, n.key, n.bind, nil, t.Logf)
		if err != nil {
			t.Fatal(err)
		}
		n.ctl = c
		t.Cleanup(c.Close)
		n.cfg = withKeys(n.cfg)
		m, err := New(t.TempDir(), n.cfg, c, Hooks{Event: func(k, d string) { t.Log(id, k, d) }})
		if err != nil {
			t.Fatal(err)
		}
		n.m = m
		t.Cleanup(m.Close)
		if err = c.Start(m); err != nil {
			t.Fatal(err)
		}
	}
	waitHealth(t, 8*time.Second, func() bool {
		return nodes["M"].m.Snapshot().Candidate == "A" && nodes["N"].m.Snapshot().Candidate == "A"
	})
	waitHealth(t, 5*time.Second, func() bool { owner, ok := nodes["Z"].m.Owner("M"); return ok && owner == "A" })
	waitHealth(t, 5*time.Second, func() bool {
		for _, id := range []string{"M", "N"} {
			nodes[id].m.mu.RLock()
			ready := nodes[id].m.candidates["B"].routeReady
			nodes[id].m.mu.RUnlock()
			if !ready {
				return false
			}
		}
		return true
	})
	before := time.Now()
	nodes["A"].ctl.Close()
	nodes["A"].bind.Close()
	waitHealth(t, 8*time.Second, func() bool {
		return nodes["M"].m.Snapshot().Candidate == "B" && nodes["N"].m.Snapshot().Candidate == "B"
	})
	t.Log("hot failover control commit", time.Since(before))
	waitHealth(t, 3*time.Second, func() bool { owner, ok := nodes["Z"].m.Owner("M"); return ok && owner == "B" })
}
