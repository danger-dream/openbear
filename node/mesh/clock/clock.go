// Package clock is the time round trips are measured with.
//
// time.Now is that on Linux and macOS. On Windows its monotonic reading
// is the system's interrupt time, which moves only at clock interrupts:
// every 15.625 ms by default, or at whatever timer happens to fire. A
// probe answered within one such step, as on a LAN, measures 0. There Now
// adds the high-resolution performance counter: its readings differ by
// what the counter measured, never go back, and stay within maxDrift of
// time.Now (fine, below).
package clock

import (
	"sync"
	"time"
)

// Now is the time to stamp a probe's sending or answer with.
func Now() time.Time {
	mu.RLock()
	c := current
	mu.RUnlock()
	if c == nil {
		return time.Now()
	}
	return c.now()
}

var (
	mu      sync.RWMutex
	current *fine // nil: time.Now
)

func init() {
	if f := counter(); f != nil {
		current = newFine(time.Now, f)
	}
}

// Simulate makes Now see a system whose time.Now is coarse and, unless
// counter is nil, that has a fine counter as Windows does, until restore.
// For tests.
func Simulate(coarse func() time.Time, counter func() time.Duration) (restore func()) {
	mu.Lock()
	was := current
	current = &fine{coarse: coarse}
	if counter != nil {
		current = newFine(coarse, counter)
	}
	mu.Unlock()
	return func() {
		mu.Lock()
		current = was
		mu.Unlock()
	}
}

const (
	// maxDrift is how far readings may part from the coarse clock, four of
	// Windows' default 15.625 ms steps: they part by up to a step as the
	// coarse clock steps, by more only were the counter to run at another
	// rate, or after a suspend.
	maxDrift = 62500 * time.Microsecond
	// Past maxDrift, readings run slewRate slower or faster than the
	// counter until they are back with the coarse clock: a counter off by
	// less than that converges, and a round trip measures within it.
	slewRate = 32
	// maxSlew is the farthest apart that converges so. Readings further
	// behind, as after a suspend the counter did not count, take the
	// coarse reading at once; readings further ahead, which no Windows
	// counter was seen to cause, hold until the coarse clock passes them.
	maxSlew = time.Second
)

// fine is a coarse clock with a counter's resolution. A reading is the
// base moved by what the counter measured since: readings differ by what
// the counter measured (by slewRate less or more while slewing), and the
// base, first a coarse reading, is taken anew from the last reading where
// slewing starts or ends, so that they never go back nor repeat while the
// counter moves.
type fine struct {
	coarse  func() time.Time
	counter func() time.Duration // nil: the coarse reading only

	mu   sync.Mutex
	base time.Time
	at   time.Duration // the counter at base
	slew int           // -1 slower, +1 faster than the counter, 0 neither
	last time.Time
}

func newFine(coarse func() time.Time, counter func() time.Duration) *fine {
	return &fine{coarse: coarse, counter: counter}
}

// reading is the time at counter c.
func (f *fine) reading(c time.Duration) time.Time {
	d := c - f.at
	return f.base.Add(d + time.Duration(f.slew)*(d/slewRate))
}

func (f *fine) now() time.Time {
	if f.counter == nil {
		return f.coarse()
	}
	// Read outside the lock: callers do not wait on each other's system
	// calls. One that read earlier but locks later gets the last reading.
	t, c := f.coarse(), f.counter()
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.base.IsZero() {
		f.base, f.at = t, c
	}
	out := f.reading(c)
	ahead := out.Sub(t)
	if ahead > maxSlew || ahead < -maxSlew || f.slew == 0 && (ahead > maxDrift || ahead < -maxDrift) {
		// A thread held up between the two reads parts them as much:
		// a drift counts once read again.
		t, c = f.coarse(), f.counter()
		out = f.reading(c)
		ahead = out.Sub(t)
	}
	switch {
	case ahead < -maxSlew || ahead > maxSlew:
		f.base, f.at, f.slew = t, c, 0
		out = t
	case f.slew == 0 && ahead > maxDrift:
		f.base, f.at, f.slew = out, c, -1
	case f.slew == 0 && ahead < -maxDrift:
		f.base, f.at, f.slew = out, c, 1
	case f.slew < 0 && ahead <= 0, f.slew > 0 && ahead >= 0:
		f.base, f.at, f.slew = out, c, 0
	}
	// Only taking the coarse reading past maxSlew ahead goes back; then
	// readings hold.
	if out.Before(f.last) {
		out = f.last
	}
	f.last = out
	return out
}
