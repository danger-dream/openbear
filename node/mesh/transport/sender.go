package transport

import (
	"net"
	"slices"
	"time"
)

const (
	sendTimeout    = 5 * time.Second
	maxSendWaiters = 4
)

type sendWaiter struct {
	ready   chan struct{}
	granted bool
}

// Both methods require b.mu; acquireSend temporarily releases it while waiting.
func (b *Bind) acquireSend(r *bindRun, s *session, plane uint8, deadline time.Time) error {
	if !time.Now().Before(deadline) {
		return ErrBackpressure
	}
	if !s.writing {
		s.writing = true
		return nil
	}
	if len(s.waiters[plane]) >= maxSendWaiters {
		return ErrBackpressure
	}
	w := &sendWaiter{ready: make(chan struct{})}
	s.waiters[plane] = append(s.waiters[plane], w)
	b.mu.Unlock()
	timer := time.NewTimer(time.Until(deadline))
	var err error
	select {
	case <-w.ready:
	case <-s.done:
		err = net.ErrClosed
	case <-r.ctx.Done():
		err = net.ErrClosed
	case <-timer.C:
		err = ErrBackpressure
	}
	timer.Stop()
	b.mu.Lock()
	if err == nil && !time.Now().Before(deadline) {
		err = ErrBackpressure
	}
	if b.run != r || s.ep.retired || s.ep.current != s || s.closed.Load() {
		err = net.ErrClosed
	}
	if err != nil {
		if w.granted {
			s.releaseSend()
		} else {
			i := slices.Index(s.waiters[plane], w)
			s.waiters[plane] = slices.Delete(s.waiters[plane], i, i+1)
		}
	}
	return err
}

func (s *session) releaseSend() {
	for _, plane := range []uint8{1, 0} {
		if len(s.waiters[plane]) != 0 {
			w := s.waiters[plane][0]
			s.waiters[plane] = slices.Delete(s.waiters[plane], 0, 1)
			w.granted = true
			close(w.ready)
			return
		}
	}
	s.writing = false
}
