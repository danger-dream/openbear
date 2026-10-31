package clock

import (
	"testing"
	"time"
)

// winClock is a system as Windows is to Go: time.Now moves in steps of
// step, at interrupts; the performance counter does not.
type winClock struct {
	start time.Time
	true  time.Duration // since start
	step  time.Duration
	drift time.Duration // the counter's lead
}

func (w *winClock) coarse() time.Time      { return w.start.Add(w.true.Truncate(w.step)) }
func (w *winClock) counter() time.Duration { return 5*time.Hour + w.true + w.drift }

// A LAN's round trip within a step of time.Now: Now measures it.
func TestFineResolvesWithinAStep(t *testing.T) {
	w := &winClock{start: time.Now(), true: 3 * time.Millisecond, step: 15625 * time.Microsecond}
	f := newFine(w.coarse, w.counter)
	f.now()
	for _, c := range []struct{ at, rtt time.Duration }{
		{5 * time.Millisecond, 5750 * time.Microsecond},
		{100 * time.Millisecond, 180 * time.Microsecond},
		{1 * time.Second, 6480 * time.Microsecond},
		{10*time.Second + 15*time.Millisecond, 2 * time.Millisecond}, // across a step
	} {
		w.true = c.at
		sent, coarseSent := f.now(), w.coarse()
		w.true += c.rtt
		answered, coarseAnswered := f.now(), w.coarse()
		if got := answered.Sub(sent); got != c.rtt {
			t.Errorf("at %v: measured %v, want %v (time.Now: %v)", c.at, got, c.rtt, coarseAnswered.Sub(coarseSent))
		}
		if lag := w.start.Add(w.true).Sub(answered); lag < 0 || lag >= w.step {
			t.Errorf("at %v: %v behind the true time, want within a step", c.at, lag)
		}
		if d := answered.Sub(coarseAnswered); d < -w.step || d > w.step {
			t.Errorf("at %v: %v from time.Now", c.at, d)
		}
	}
}

// The counter parting from time.Now (a suspend): taken again, never
// stepping back.
func TestFineFollowsADrift(t *testing.T) {
	w := &winClock{start: time.Now(), step: 15625 * time.Microsecond}
	f := newFine(w.coarse, w.counter)
	w.true = 7 * time.Millisecond
	before := f.now()
	w.true = time.Minute + 7*time.Millisecond
	w.drift = -30 * time.Second
	after := f.now()
	if after.Before(before) {
		t.Fatalf("stepped back: %v, then %v", before, after)
	}
	if d := after.Sub(w.coarse()); d != 0 {
		t.Fatalf("%v from time.Now after the drift, want taken from it", d)
	}
	w.true += 4 * time.Millisecond
	if got := f.now().Sub(after); got != 4*time.Millisecond {
		t.Fatalf("measured %v after the drift, want 4ms", got)
	}
}

// A counter running 1% fast or slow against the coarse clock, for five
// minutes: every round trip, and every time between two, measures what
// the counter did, give or take the slewing, never 0 nor a jump; readings
// stay within 100 ms of time.Now.
func TestFineCounterOffRate(t *testing.T) {
	for _, c := range []struct {
		name string
		rate float64
	}{{"fast", 1.01}, {"slow", 0.99}} {
		t.Run(c.name, func(t *testing.T) {
			w := &winClock{start: time.Now(), step: 15625 * time.Microsecond}
			f := newFine(w.coarse, func() time.Duration { return 5*time.Hour + time.Duration(float64(w.true)*c.rate) })
			const rtt, every = 500 * time.Microsecond, 50 * time.Millisecond
			within := func(got, want time.Duration) bool { return got >= want*95/100 && got <= want*105/100 }
			var last time.Time
			bad := 0
			for w.true = 0; w.true < 5*time.Minute; w.true += every {
				sent := f.now()
				w.true += rtt
				answered := f.now()
				w.true -= rtt
				got, gap := answered.Sub(sent), sent.Sub(last)
				if !within(got, rtt) || !last.IsZero() && !within(gap, every-rtt) {
					if bad++; bad <= 3 {
						t.Errorf("at %v: measured %v for %v, %v since the last reading", w.true, got, rtt, gap)
					}
				}
				if d := answered.Sub(w.coarse()); d < -100*time.Millisecond || d > 100*time.Millisecond {
					if bad++; bad <= 3 {
						t.Errorf("at %v: %v from time.Now", w.true, d)
					}
				}
				last = answered
			}
			if bad > 0 {
				t.Errorf("%d bad readings", bad)
			}
		})
	}
}

// A thread held up between reading the coarse clock and the counter: no
// drift is taken from it.
func TestFineReadsADriftAgain(t *testing.T) {
	w := &winClock{start: time.Now(), step: 15625 * time.Microsecond}
	held := time.Duration(0)
	f := newFine(w.coarse, func() time.Duration {
		c := w.counter() + held
		if held > 0 {
			w.true += held
			held = 0
		}
		return c
	})
	w.true = 3 * time.Millisecond
	f.now()
	w.true = time.Second
	held = 200 * time.Millisecond
	f.now()
	if f.slew != 0 {
		t.Fatalf("slewing (%d) after a held-up read", f.slew)
	}
}

func TestSimulate(t *testing.T) {
	mu.RLock()
	was := current
	mu.RUnlock()
	w := &winClock{start: time.Now(), step: 15625 * time.Microsecond}
	restore := Simulate(w.coarse, nil)
	a := Now()
	w.true = 5 * time.Millisecond
	if d := Now().Sub(a); d != 0 {
		t.Fatalf("time.Now alone measured %v", d)
	}
	restore()
	restore = Simulate(w.coarse, w.counter)
	a = Now()
	w.true += 5 * time.Millisecond
	if d := Now().Sub(a); d != 5*time.Millisecond {
		t.Fatalf("with the counter: %v", d)
	}
	restore()
	mu.RLock()
	defer mu.RUnlock()
	if current != was {
		t.Fatal("not restored")
	}
}
