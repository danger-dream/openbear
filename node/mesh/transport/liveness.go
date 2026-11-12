package transport

import (
	"sync/atomic"
	"time"

	"openbear/node/mesh/wire"
)

// On a stream or companion of keepaliveVersion or later each end writes a
// keepalive frame, of a plane every reader of protocol 3 or later skips,
// whenever its writer is free every keepaliveEvery, and a reader that gets
// nothing for silenceLimit closes the connection. A connection the network
// stops carrying, while TCP itself keeps it open, then ends at both ends
// within silenceLimit: a companion is replaced, a stream redialed, instead
// of control waiting minutes behind it.
const (
	keepaliveVersion = wire.LargeFrames
	planeKeepalive   = 2
)

// keepaliveEvery and silenceLimit, in nanoseconds; tests shorten them.
var keepaliveEvery, silenceLimit atomic.Int64

func init() {
	keepaliveEvery.Store(int64(5 * time.Second))
	silenceLimit.Store(int64(20 * time.Second))
}

var keepaliveFrame = []byte{0, 2, planeKeepalive, 0}

// idleLimit is how long s's reader waits for the next frame.
func idleLimit(s *session) time.Duration {
	if s.version >= keepaliveVersion {
		return time.Duration(silenceLimit.Load())
	}
	return 180 * time.Second
}

// keepAlive writes s's keepalives until it ends.
func (b *Bind) keepAlive(r *bindRun, s *session) {
	if s.version < keepaliveVersion {
		return
	}
	go func() {
		t := time.NewTicker(time.Duration(keepaliveEvery.Load()))
		defer t.Stop()
		for {
			select {
			case <-s.done:
				return
			case <-r.ctx.Done():
				return
			case <-t.C:
			}
			b.mu.Lock()
			if s.closed.Load() || s.writing || !s.ready {
				// A write under way says as much.
				b.mu.Unlock()
				continue
			}
			s.writing = true
			b.mu.Unlock()
			err := s.conn.SetWriteDeadline(time.Now().Add(sendTimeout))
			if err == nil {
				err = writeAll(s.conn, keepaliveFrame)
			}
			if err != nil {
				s.closed.Store(true)
				closeConn(s.conn)
			}
			b.mu.Lock()
			s.releaseSend()
			b.mu.Unlock()
		}
	}()
}
