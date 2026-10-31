package node

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// unavailable answers its first `refusals` upgrades (all of them when < 0) the
// way a controller does with a 503 for reason, then serves as f.
func unavailable(t *testing.T, f *fakeController, reason string, refusals int64, hellos chan<- map[string]any) (*httptest.Server, *atomic.Int64) {
	var seen atomic.Int64
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if n := seen.Add(1); refusals < 0 || n <= refusals {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusServiceUnavailable)
			w.Write([]byte(`{"ok": false, "error": "` + reason + `"}`))
			return
		}
		if ch, _ := f.handshake(t, w, r, nil); ch != nil {
			hellos <- next(t, ch, "hello")
			ch.conn.Read()
		}
	}))
	return s, &seen
}

// runUntilDone runs a until the test ends, and waits for it to stop.
func runUntilDone(t *testing.T, ctx context.Context, cancel context.CancelFunc, a *Agent) {
	done := make(chan struct{})
	go func() {
		defer close(done)
		a.Run(ctx)
	}()
	t.Cleanup(func() {
		cancel()
		<-done
	})
}

func shortStartup(t *testing.T, retry, grace time.Duration) {
	startingRetry, startingGrace = retry, grace
	t.Cleanup(func() { startingRetry, startingGrace = 2*time.Second, time.Minute })
}

func TestControllerStartingIsRecognised(t *testing.T) {
	for _, c := range []struct {
		err  error
		want bool
	}{
		{&HandshakeError{Status: 503, Body: []byte(`{"ok": false, "error": "service_not_ready"}`)}, true},
		{&HandshakeError{Status: 503, Body: []byte(`{"ok": false, "error": "busy"}`)}, false},
		{&HandshakeError{Status: 503, Body: []byte(`<html>service_not_ready</html>`)}, false},
		{&HandshakeError{Status: 502, Body: []byte(`{"error": "service_not_ready"}`)}, false},
		{errors.New("connection refused"), false},
	} {
		if got := controllerStarting(c.err); got != c.want {
			t.Errorf("%v: %v, want %v", c.err, got, c.want)
		}
	}
}

// A controller that is starting is waited for on the same address, however
// many times it says so: the backup is never tried.
func TestStartingControllerKeepsTheNodeOnItsAddress(t *testing.T) {
	shortStartup(t, 20*time.Millisecond, time.Minute)
	f := newFake()
	hellos := make(chan map[string]any, 2)
	primary, seen := unavailable(t, f, "service_not_ready", 6, hellos)
	defer primary.Close()
	backup, tried := unavailable(t, f, "busy", -1, hellos)
	defer backup.Close()
	a := f.agent(primary.URL, Capabilities{})
	a.Config.Backup = backup.URL
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	started := time.Now()
	runUntilDone(t, ctx, cancel, a)
	select {
	case hello := <-hellos:
		if hello["backup"] == true || tried.Load() != 0 || seen.Load() != 7 {
			t.Fatalf("hello %v after %d refusals, %d tries of the backup", hello, seen.Load()-1, tried.Load())
		}
		// Retried shortly, not on the doubling backoff (1+2+4+... s).
		if took := time.Since(started); took > 2*time.Second {
			t.Fatalf("took %s", took)
		}
	case <-ctx.Done():
		t.Fatalf("not connected; primary saw %d, backup %d", seen.Load(), tried.Load())
	}
}

// One that stays starting counts as failing once the grace is over.
func TestControllerThatNeverFinishesStartingFallsBackToTheBackup(t *testing.T) {
	shortStartup(t, 20*time.Millisecond, 300*time.Millisecond)
	// Shorter than the grace, as in production: the time spent starting
	// counts, so two failures after it move on.
	fastSwitch(t, 200*time.Millisecond, 100*time.Millisecond)
	f := newFake()
	hellos := make(chan map[string]any, 1)
	primary, seen := unavailable(t, f, "service_not_ready", -1, hellos)
	defer primary.Close()
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
		if hello["backup"] != true || time.Since(started) < 300*time.Millisecond || seen.Load() < 3 {
			t.Fatalf("hello %v after %s and %d attempts on the primary", hello, time.Since(started), seen.Load())
		}
	case <-ctx.Done():
		t.Fatal("still not on the backup")
	}
}

// Any other 503, such as a controller with all handshakes taken, is a failed
// attempt as before: they move to the backup once the address has failed for
// switchAfter.
func TestOtherUnavailableAnswersStillMoveToTheBackup(t *testing.T) {
	shortStartup(t, time.Hour, time.Hour)
	fastSwitch(t, 400*time.Millisecond, 100*time.Millisecond)
	f := newFake()
	hellos := make(chan map[string]any, 1)
	primary, seen := unavailable(t, f, "busy", -1, hellos)
	defer primary.Close()
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
		if hello["backup"] != true || seen.Load() < 2 || time.Since(started) < 400*time.Millisecond {
			t.Fatalf("hello %v after %s and %d attempts on the primary", hello, time.Since(started), seen.Load())
		}
	case <-ctx.Done():
		t.Fatal("still not on the backup")
	}
}
