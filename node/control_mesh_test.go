package node

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeLink stands in for the mesh here: it carries the addresses listed.
type fakeLink struct {
	mesh map[string]bool
	down atomic.Bool
}

func (l *fakeLink) carries(address string) bool { return l.mesh[address] }

func (l *fakeLink) up() (bool, string) {
	if l.down.Load() {
		return false, "test says down"
	}
	return true, ""
}

var meshTimersOnce sync.Once

// fastMeshTimers shortens the mesh timers for the rest of the run: sessions
// of earlier tests may still read them on their way out.
func fastMeshTimers(*testing.T) {
	meshTimersOnce.Do(func() {
		meshWatchInterval, meshHomeProbeInterval, meshHomeRetry, meshHomeBurst, meshDialTimeout =
			20*time.Millisecond, 2*time.Second, 200*time.Millisecond, time.Second, 2*time.Second
	})
}

// controllerPair is one fake controller reachable at two addresses; every
// session's hello comes out of hellos, tagged with the address.
type controllerPair struct {
	f               *fakeController
	primary, backup *httptest.Server
	hellos          chan string
	primaryConnects atomic.Int32
	primarySessions atomic.Int32
}

func newControllerPair(t *testing.T) *controllerPair {
	p := &controllerPair{f: newFake(), hellos: make(chan string, 16)}
	serve := func(name string, counted bool) *httptest.Server {
		return p.f.serve(t, func(ch *channel, auth map[string]any) {
			if counted {
				p.primaryConnects.Add(1)
			}
			if auth["probe"] == true {
				return
			}
			if counted {
				p.primarySessions.Add(1)
			}
			next(t, ch, "hello")
			p.hellos <- name
			ch.conn.SetIdleTimeout(time.Minute)
			for { // stays connected until the node leaves
				if _, _, err := ch.conn.Read(); err != nil {
					return
				}
			}
		})
	}
	p.primary, p.backup = serve("primary", true), serve("backup", false)
	t.Cleanup(func() { p.primary.Close(); p.backup.Close() })
	return p
}

func (p *controllerPair) expect(t *testing.T, want string, within time.Duration) time.Duration {
	t.Helper()
	start := time.Now()
	select {
	case got := <-p.hellos:
		if got != want {
			t.Fatalf("hello on the %s address, want the %s", got, want)
		}
	case <-time.After(within):
		t.Fatalf("no hello on the %s address within %s", want, within)
	}
	return time.Since(start)
}

func (p *controllerPair) quiet(t *testing.T, d time.Duration) {
	t.Helper()
	select {
	case got := <-p.hellos:
		t.Fatalf("unexpected move to the %s address", got)
	case <-time.After(d):
	}
}

func runAgent(t *testing.T, a *Agent) {
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { a.Run(ctx); close(done) }()
	t.Cleanup(func() { cancel(); <-done })
}

// Starting with the mesh down, a node whose primary is a mesh address goes
// to the backup without trying the primary, and moves back as soon as the
// mesh is up.
func TestMeshPrimarySkippedWhileDownAndRetakenWhenUp(t *testing.T) {
	fastMeshTimers(t)
	p := newControllerPair(t)
	link := &fakeLink{mesh: map[string]bool{p.primary.URL: true}}
	link.down.Store(true)
	a := p.f.agent(p.primary.URL, Capabilities{})
	a.Config.Backup, a.link = p.backup.URL, link
	runAgent(t, a)
	if d := p.expect(t, "backup", 5*time.Second); d > 2*time.Second {
		t.Fatalf("took %s to reach the backup", d)
	}
	time.Sleep(300 * time.Millisecond)
	if n := p.primaryConnects.Load(); n != 0 {
		t.Fatalf("the primary was tried %d times with the mesh down", n)
	}
	link.down.Store(false)
	if d := p.expect(t, "primary", 3*time.Second); d > time.Second {
		t.Fatalf("took %s to move back once the mesh was up", d)
	}
}

// A session on a mesh address leaves at once when the mesh goes down, and
// comes back once it is up again.
func TestMeshDownMovesOffAtOnce(t *testing.T) {
	fastMeshTimers(t)
	p := newControllerPair(t)
	link := &fakeLink{mesh: map[string]bool{p.primary.URL: true}}
	a := p.f.agent(p.primary.URL, Capabilities{})
	a.Config.Backup, a.link = p.backup.URL, link
	runAgent(t, a)
	p.expect(t, "primary", 5*time.Second)
	link.down.Store(true)
	if d := p.expect(t, "backup", 3*time.Second); d > time.Second {
		t.Fatalf("took %s to leave the mesh address", d)
	}
	link.down.Store(false)
	p.expect(t, "primary", 3*time.Second)
}

// On the backup with the mesh up, the primary mesh address is tried every
// meshHomeProbeInterval: a primary that answers again is taken back without
// the mesh going down and up.
func TestMeshPrimaryRetriedEveryInterval(t *testing.T) {
	fastMeshTimers(t)
	fastSwitch(t, 300*time.Millisecond, 50*time.Millisecond)
	p := newControllerPair(t)
	// The primary refuses connections until it is started again.
	hostPort := p.primary.Listener.Addr().String()
	p.primary.Listener.Close()
	link := &fakeLink{mesh: map[string]bool{p.primary.URL: true}}
	a := p.f.agent(p.primary.URL, Capabilities{})
	a.Config.Backup, a.link = p.backup.URL, link
	runAgent(t, a)
	// The mesh being up, an address that refuses is left as any other is.
	if d := p.expect(t, "backup", 5*time.Second); d < 300*time.Millisecond {
		t.Fatalf("left the primary after %s", d)
	}
	listener, err := net.Listen("tcp", hostPort)
	if err != nil {
		t.Fatal(err)
	}
	restarted := &http.Server{Handler: p.primary.Config.Handler}
	go restarted.Serve(listener)
	defer restarted.Close()
	d := p.expect(t, "primary", 5*time.Second)
	if d > meshHomeProbeInterval+time.Second {
		t.Fatalf("took %s to move back", d)
	}
}

// With the mesh address alone, or both addresses on the mesh, there is
// nowhere to go: the node keeps to the old rules and tries the mesh address.
func TestMeshWithoutAnExitKeepsTrying(t *testing.T) {
	fastMeshTimers(t)
	p := newControllerPair(t)
	for _, backup := range []string{"", p.backup.URL} {
		link := &fakeLink{mesh: map[string]bool{p.primary.URL: true, p.backup.URL: true}}
		link.down.Store(true)
		a := p.f.agent(p.primary.URL, Capabilities{})
		a.Config.Backup, a.link = backup, link
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() { a.Run(ctx); close(done) }()
		p.expect(t, "primary", 5*time.Second)
		// Down or not, the session stays.
		p.quiet(t, 500*time.Millisecond)
		cancel()
		<-done
	}
}

// A primary that is not a mesh address behaves as before: a session on it
// ignores the mesh, the backup (a mesh address here) returns home only on
// homeProbeInterval, and the mesh going down on the backup moves back to
// the primary, which is the address that does not need it.
func TestNonMeshPrimaryUnchanged(t *testing.T) {
	fastMeshTimers(t)
	p := newControllerPair(t)
	link := &fakeLink{mesh: map[string]bool{p.backup.URL: true}}
	a := p.f.agent(p.primary.URL, Capabilities{})
	a.Config.Backup, a.link = p.backup.URL, link
	runAgent(t, a)
	p.expect(t, "primary", 5*time.Second)
	link.down.Store(true)
	p.quiet(t, 500*time.Millisecond)
	link.down.Store(false)
	p.quiet(t, 300*time.Millisecond)
	if a.meshExit(false) {
		t.Fatal("a non-mesh primary taken for a mesh address")
	}
}
