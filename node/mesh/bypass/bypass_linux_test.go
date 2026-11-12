package bypass

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/net/dns/dnsmessage"
)

// The first upstream takes the queries and never answers, the second answers: the resolver's
// retry reaches the second rather than asking the first again.
func TestResolverMovesOnFromAnUpstreamThatNeverAnswers(t *testing.T) {
	silent, err := net.ListenPacket("udp4", "127.97.1.1:53")
	if err != nil {
		t.Skipf("no DNS fixture on loopback: %v", err)
	}
	defer silent.Close()
	good, err := net.ListenPacket("udp4", "127.97.1.2:53")
	if err != nil {
		t.Skipf("no DNS fixture on loopback: %v", err)
	}
	defer good.Close()
	var asked atomic.Int32
	go func() {
		b := make([]byte, 4096)
		for {
			if _, _, err := silent.ReadFrom(b); err != nil {
				return
			}
		}
	}()
	go func() {
		b := make([]byte, 4096)
		for {
			n, from, err := good.ReadFrom(b)
			if err != nil {
				return
			}
			asked.Add(1)
			var m dnsmessage.Message
			if m.Unpack(b[:n]) != nil {
				continue
			}
			m.Header.Response, m.Header.RecursionAvailable = true, true
			for _, q := range m.Questions {
				if q.Type == dnsmessage.TypeA {
					m.Answers = append(m.Answers, dnsmessage.Resource{Header: dnsmessage.ResourceHeader{Name: q.Name, Type: q.Type, Class: q.Class, TTL: 60},
						Body: &dnsmessage.AResource{A: [4]byte{192, 0, 2, 1}}})
				}
			}
			out, _ := m.Pack()
			good.WriteTo(out, from)
		}
	}()
	keepState, keepConfs, keepFallback := StateFile, resolvConfs, fallbackDNS
	defer func() { StateFile, resolvConfs, fallbackDNS = keepState, keepConfs, keepFallback; Forget() }()
	StateFile = filepath.Join(t.TempDir(), "active")
	os.WriteFile(StateFile, nil, 0o600)
	Forget()
	if raw, err := silent.(*net.UDPConn).SyscallConn(); err == nil {
		if err := Control("udp4", "", raw); err != nil {
			t.Skipf("a bypassed socket takes a mark, which needs CAP_NET_ADMIN: %v", err)
		}
	}
	resolvConfs = []string{filepath.Join(t.TempDir(), "none")}
	fallbackDNS = []string{"127.97.1.1", "127.97.1.2"}
	Forget()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	got, err := Resolver().LookupHost(ctx, "upstream.test.")
	if err != nil || len(got) == 0 || got[0] != "192.0.2.1" || asked.Load() == 0 {
		t.Fatalf("%v %v, the answering upstream asked %d times", got, err, asked.Load())
	}
}
