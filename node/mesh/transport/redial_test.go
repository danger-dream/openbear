package transport

import (
	"context"
	"net/netip"
	"testing"
	"time"
)

func localPort(t *testing.T, s string) uint16 {
	t.Helper()
	a, err := netip.ParseAddrPort(s)
	if err != nil {
		t.Fatal(err)
	}
	return a.Port()
}

// A redial moves a dialed TCP or TLS path to a connection from another
// source port, keeping its PathRef: control and data go on, the other end
// sees a new path of the same peer and, until it commits that one, still
// sends over the old connection, which Retire then closes without the path
// closing.
func TestRedialMovesThePath(t *testing.T) {
	for _, kind := range []string{"tcp", "tls"} {
		t.Run(kind, func(t *testing.T) {
			l := newPathLab(t)
			uri := l.tcp
			if kind == "tls" {
				uri = l.tls
			}
			a, z := l.prepare(t, uri)
			l.commit(t, a, z)
			for len(l.a.Events()) > 0 {
				<-l.a.Events()
			}
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			got, err := l.a.Redial(ctx, a)
			cancel()
			if err != nil {
				t.Fatal(err)
			}
			if localPort(t, got.From) == localPort(t, got.To) {
				t.Fatalf("same source port: %+v", got)
			}
			if _, err := l.a.Redial(context.Background(), a); err != ErrRedialBusy {
				t.Fatalf("second redial while the first holds the old connection: %v", err)
			}
			// The other end, still committed to its old path, sends over the
			// old connection; this end over the new one.
			transfer(t, l.zd, l.ad, "over the old connection")
			p := sendControl(t, l.ac, l.zc, l.a, a, "over the new connection", 20)
			info, ok := l.z.PathInfo(p.recv.Endpoint)
			if !ok || info.PeerID != "a" || info.Ref == z || localPort(t, info.RemoteAddr) != localPort(t, got.To) {
				t.Fatalf("the other end got it on %+v", info)
			}
			back := sendControl(t, l.zc, l.ac, l.z, info.Ref, "answer on the new connection", 21)
			if bi, ok := l.a.PathInfo(back.recv.Endpoint); !ok || bi.Ref != a {
				t.Fatalf("answer arrived on %+v", bi)
			}
			l.commit(t, a, info.Ref)
			l.a.Retire(a)
			waitUntil(t, 3*time.Second, "old connection closed", func() bool {
				l.a.mu.Lock()
				defer l.a.mu.Unlock()
				for s := range l.a.run.sessions {
					if s.ep.id == a.ID && s.replaced {
						return false
					}
				}
				return true
			})
			transfer(t, l.ad, l.zd, "forward after retiring")
			transfer(t, l.zd, l.ad, "back after retiring")
			for len(l.a.Events()) > 0 {
				if e := <-l.a.Events(); e.Kind == "closed" && e.Path.Ref.ID == a.ID {
					t.Fatalf("the path closed: %+v", e)
				}
			}
			l.a.mu.Lock()
			ep := l.a.ids[a.ID]
			gen, local, redials := ep.generation, ep.stats.LocalAddr, ep.stats.Redials
			l.a.mu.Unlock()
			if gen != a.Generation || local != got.To || redials != 1 {
				t.Fatalf("generation %d (was %d), local %s, redials %d", gen, a.Generation, local, redials)
			}
			// Once the new connection ends, the path is dialed again as
			// after any lost connection.
			l.a.mu.Lock()
			l.a.ids[a.ID].current.conn.Close()
			l.a.mu.Unlock()
			waitUntil(t, 5*time.Second, "next generation", func() bool {
				l.a.mu.Lock()
				defer l.a.mu.Unlock()
				ep := l.a.ids[a.ID]
				return ep.generation > a.Generation && ep.current != nil && ep.current.ready
			})
		})
	}
}

// A new connection that authenticates nothing within redialAuthWait hands
// the path back to the old one.
func TestRedialFallsBack(t *testing.T) {
	l := newPathLab(t)
	a, z := l.prepare(t, l.tcp)
	l.commit(t, a, z)
	got, err := l.a.Redial(context.Background(), a)
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(redialAuthWait + time.Second)
	l.a.mu.Lock()
	ep := l.a.ids[a.ID]
	back := ep.current != nil && ep.current.conn.LocalAddr().String() == got.From && ep.stats.LocalAddr == got.From && ep.generation == a.Generation
	sessions := len(l.a.run.sessions)
	l.a.mu.Unlock()
	if !back || sessions != 1 {
		t.Fatalf("not back on %s (sessions %d)", got.From, sessions)
	}
	transfer(t, l.ad, l.zd, "forward after falling back")
	transfer(t, l.zd, l.ad, "back after falling back")
	if _, err := l.a.Redial(context.Background(), a); err != nil {
		t.Fatalf("redial after falling back: %v", err)
	}
}

// Only a dialed stream path redials.
func TestRedialOnlyDialedStreams(t *testing.T) {
	l := newPathLab(t)
	a, z := l.prepare(t, l.udp)
	l.commit(t, a, z)
	if _, err := l.a.Redial(context.Background(), a); err == nil {
		t.Fatal("UDP path redialed")
	}
	b, y := l.prepare(t, l.tcp)
	l.commit(t, b, y)
	if _, err := l.z.Redial(context.Background(), y); err == nil {
		t.Fatal("accepted path redialed")
	}
}
