//go:build !linux

package fast

// captureRunner is capture outside Linux: in the data plane, by a user-space
// TCP/IP stack (stack.go), which has no rules of its own to keep.
type captureRunner struct {
	svc *Service
	tun *tunCapture
	err string
}

func (s *Service) applyCaptureLocked(c *Config) {
	if !c.Capture {
		if s.capture != nil {
			s.capture.close()
			s.capture = nil
		}
		return
	}
	if s.capture != nil && !s.capture.failed() {
		return
	}
	if s.capture != nil {
		s.capture.close()
	}
	cr := &captureRunner{svc: s}
	tc, err := newTunCapture(s, s.hooks.ToOS, s.udpSessions)
	if err != nil {
		cr.err = err.Error()
	} else {
		cr.tun = tc
		s.tun.Store(tc)
	}
	s.capture = cr
}

func (t *captureRunner) failed() bool { return t.tun == nil }

func (t *captureRunner) status() *CaptureStatus {
	if t.tun == nil {
		return &CaptureStatus{Mode: "tun", Error: t.err}
	}
	return t.tun.status()
}

func (*captureRunner) heal()              {}
func (*captureRunner) update([]targetKey) {}

func (t *captureRunner) close() {
	if t.tun != nil {
		t.svc.tun.CompareAndSwap(t.tun, nil)
		t.tun.close()
		t.tun = nil
	}
}

// SweepTransparent is a no-op outside Linux.
func SweepTransparent() {}
