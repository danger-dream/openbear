package fast

import (
	"encoding/binary"
	"io"
	"net"
	"net/netip"
	"testing"
	"time"

	"openbear/node/mesh/proxy"
)

func TestReviewFastForwardHandoffKeepsEntryIdentity(t *testing.T) {
	backend, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer backend.Close()
	backendPort := uint16(backend.Addr().(*net.TCPAddr).Port)
	go func() {
		for {
			c, e := backend.Accept()
			if e != nil {
				return
			}
			go func() { defer c.Close(); io.Copy(c, c) }()
		}
	}()
	self := netip.MustParseAddr("127.0.0.1")
	a, b := netip.MustParseAddr("127.0.0.2"), netip.MustParseAddr("127.0.0.3")
	port := freeTCPPort(t)
	cfg := &proxy.ForwardConfig{Port: freeTCPPort(t), FastPort: port, Targets: []proxy.ForwardTarget{
		{From: a, Fast: true, Host: "127.0.0.1", Port: int(backendPort), Proto: "tcp"},
		{From: b, Fast: true, Host: "127.0.0.1", Port: int(backendPort) + 1, Proto: "tcp"},
	}}
	var f proxy.Forwarder
	f.Apply(cfg, self)
	defer f.Close()
	s, st := reviewState(t)
	st.selfAddr = self
	st.idByAddr = map[netip.Addr]string{a: "A", b: "B", self: "T"}
	s.hooks.LocalTCP = f.ServeFast
	request := func(origin netip.Addr, backend uint16, fast bool) byte {
		t.Helper()
		var client *net.TCPConn
		if fast {
			down, up := clientPair(t)
			client = up
			go s.serveRelay(down)
			wire, e := st.sealer.seal(header{at: time.Now(), proto: protoTCP, origin: origin, source: origin, dest: self, port: uint16(port), visited: []netip.Addr{origin}})
			if e != nil {
				t.Fatal(e)
			}
			client.Write(wire)
		} else {
			d := net.Dialer{LocalAddr: &net.TCPAddr{IP: origin.AsSlice()}}
			c, e := d.Dial("tcp", netip.AddrPortFrom(self, uint16(port)).String())
			if e != nil {
				t.Fatal(e)
			}
			client = c.(*net.TCPConn)
		}
		defer client.Close()
		client.SetDeadline(time.Now().Add(2 * time.Second))
		// The caller can choose arbitrary ordinary forward payload, but not the
		// trusted handoff identity. This requests B's backend from A.
		head := []byte{1, 1, 1, 127, 0, 0, 1, 0, 0}
		binary.BigEndian.PutUint16(head[7:], backend)
		client.Write(head)
		var status [1]byte
		if _, e := io.ReadFull(client, status[:]); e != nil {
			return 255
		}
		if status[0] == 0 {
			client.Write([]byte("ok"))
			buf := make([]byte, 2)
			if _, e := io.ReadFull(client, buf); e != nil || string(buf) != "ok" {
				t.Fatal("authorized handoff broken", e)
			}
		}
		return status[0]
	}
	for _, fast := range []bool{false, true} {
		if got := request(a, backendPort, fast); got != 0 {
			t.Fatalf("allowed entry fast=%v: %d", fast, got)
		}
		if got := request(a, backendPort+1, fast); got != 2 {
			t.Fatalf("cross-entry request fast=%v: %d", fast, got)
		}
	}
	if got := request(self, backendPort, false); got == 0 {
		t.Fatal("ordinary local self became trusted")
	}
}
