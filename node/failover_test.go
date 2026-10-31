package node

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fastSwitch shortens the failover rules for a test; a session counts as one
// that ran after 200 ms.
func fastSwitch(t *testing.T, after, retry time.Duration) {
	switchAfter, failingRetry, sessionSettled = after, retry, 200*time.Millisecond
	t.Cleanup(func() { switchAfter, failingRetry, sessionSettled = 45*time.Second, 5*time.Second, time.Minute })
}

// attempts records which address each upgrade went to, and when.
type attempts struct {
	mu   sync.Mutex
	list []attempt
}

type attempt struct {
	at   time.Time
	name string
}

func (r *attempts) add(name string) {
	r.mu.Lock()
	r.list = append(r.list, attempt{time.Now(), name})
	r.mu.Unlock()
}

func (r *attempts) all() []attempt {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]attempt(nil), r.list...)
}

// badGateway is an address whose proxy answers 502 while the controller is down.
func badGateway(name string, log *attempts) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		log.add(name)
		w.WriteHeader(http.StatusBadGateway)
	}))
}

// A controller restart: the session that ran ends, the address answers 502
// for a while (shorter than switchAfter) and the node, retrying it every
// failingRetry, is back on it moments after it answers. The backup is never
// tried.
func TestRestartingControllerKeepsTheNodeOnItsAddress(t *testing.T) {
	fastSwitch(t, 3*time.Second, 200*time.Millisecond)
	f := newFake()
	hellos := make(chan map[string]any, 2)
	var down atomic.Bool
	var whileDown atomic.Int64
	drop := make(chan struct{}, 1)
	primary := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if down.Load() {
			whileDown.Add(1)
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		ch, _ := f.handshake(t, w, r, nil)
		if ch == nil {
			return
		}
		hellos <- next(t, ch, "hello")
		gone := make(chan struct{})
		go func() {
			defer close(gone)
			for {
				if _, _, err := ch.conn.Read(); err != nil {
					return
				}
			}
		}()
		select {
		case <-drop:
			ch.conn.Close()
		case <-gone:
		}
	}))
	defer primary.Close()
	var log attempts
	backup := badGateway("backup", &log)
	defer backup.Close()
	a := f.agent(primary.URL, Capabilities{})
	a.Config.Backup = backup.URL
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	runUntilDone(t, ctx, cancel, a)
	if hello := <-hellos; hello["backup"] == true {
		t.Fatalf("first hello: %v", hello)
	}
	time.Sleep(300 * time.Millisecond) // the session ran
	down.Store(true)
	drop <- struct{}{}
	time.Sleep(2500 * time.Millisecond)
	down.Store(false)
	up := time.Now()
	select {
	case hello := <-hellos:
		if hello["backup"] == true {
			t.Fatalf("back on the backup: %v", hello)
		}
		if took := time.Since(up); took > time.Second {
			t.Fatalf("reconnected %s after the controller answered", took)
		}
	case <-ctx.Done():
		t.Fatal("never reconnected")
	}
	if n := len(log.all()); n != 0 || whileDown.Load() < 3 {
		t.Fatalf("%d attempts on the primary while it was down, %d on the backup", whileDown.Load(), n)
	}
}

// A primary that stays down is left once it has failed for switchAfter.
func TestDownPrimaryIsLeftAfterSwitchAfter(t *testing.T) {
	fastSwitch(t, 1500*time.Millisecond, 100*time.Millisecond)
	f := newFake()
	var log attempts
	primary := badGateway("primary", &log)
	defer primary.Close()
	hellos := make(chan map[string]any, 1)
	backup, _ := unavailable(t, f, "", 0, hellos)
	defer backup.Close()
	a := f.agent(primary.URL, Capabilities{})
	a.Config.Backup = backup.URL
	ctx, cancel := context.WithTimeout(context.Background(), 6*time.Second)
	defer cancel()
	started := time.Now()
	runUntilDone(t, ctx, cancel, a)
	select {
	case hello := <-hellos:
		took := time.Since(started)
		if hello["backup"] != true || took > 4*time.Second {
			t.Fatalf("hello %v after %s", hello, took)
		}
		list := log.all()
		if n := len(list); n < 4 || list[n-1].at.Sub(list[0].at) < 1500*time.Millisecond {
			t.Fatalf("left the primary after %d attempts: %v", n, list)
		}
	case <-ctx.Done():
		t.Fatal("still not on the backup")
	}
}

// Both addresses down: each is kept for switchAfter before the other is
// tried, rather than the node flipping between them.
func TestBothAddressesDownDoNotFlipQuickly(t *testing.T) {
	fastSwitch(t, 2500*time.Millisecond, 100*time.Millisecond)
	f := newFake()
	var log attempts
	primary, backup := badGateway("primary", &log), badGateway("backup", &log)
	defer primary.Close()
	defer backup.Close()
	a := f.agent(primary.URL, Capabilities{})
	a.Config.Backup = backup.URL
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	runUntilDone(t, ctx, cancel, a)
	firstBackup := func() int {
		for i, at := range log.all() {
			if at.name == "backup" {
				return i
			}
		}
		return -1
	}
	deadline := time.Now().Add(6 * time.Second)
	for firstBackup() < 0 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	i := firstBackup()
	if i < 2 {
		t.Fatalf("attempts: %v", log.all())
	}
	list := log.all()
	if span := list[i-1].at.Sub(list[0].at); span < 2400*time.Millisecond {
		t.Fatalf("left the primary after %d attempts over %s", i, span)
	}
	// On the backup the same rule holds before it goes back.
	time.Sleep(1500 * time.Millisecond)
	for _, at := range log.all()[i:] {
		if at.name != "backup" {
			t.Fatalf("back on the primary %s after reaching the backup", at.at.Sub(list[i].at))
		}
	}
}
