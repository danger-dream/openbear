package fast

import (
	"io"
	"net"
	"time"
)

// A stream's peer can go away without a word: a machine that crashed, a link
// cut, a firewall dropping what it no longer knows. The kernel then resends
// what waits on the socket for about 15 minutes, and keepalive stays quiet
// while anything does. Every stallCheck the sockets of the open streams are
// looked at, and a stream is cut once one of them has had something waiting
// on its peer at two looks in a row with nothing acknowledged for stallAfter.
var (
	stallCheck = 5 * time.Second
	stallAfter = 15 * time.Second
)

func (s *Service) watchStalls(stop <-chan struct{}, every, after time.Duration) {
	t := time.NewTicker(every)
	defer t.Stop()
	var waiting map[*net.TCPConn]bool
	for {
		select {
		case <-stop:
			return
		case <-t.C:
		}
		waiting = s.cutStalled(waiting, after)
	}
}

// cutStalled cuts the streams with a socket stalled for after that was
// already waiting at the last look, and returns the sockets waiting now.
func (s *Service) cutStalled(was map[*net.TCPConn]bool, after time.Duration) map[*net.TCPConn]bool {
	type watched struct {
		c    io.Closer
		sock *net.TCPConn
	}
	var socks []watched
	s.openMu.Lock()
	for c, t := range s.open {
		for _, sock := range t.socks {
			socks = append(socks, watched{c, sock})
		}
	}
	s.openMu.Unlock()
	waiting := make(map[*net.TCPConn]bool, len(was))
	stalled := map[io.Closer]bool{}
	for _, w := range socks {
		since, ok := unanswered(w.sock)
		if !ok {
			continue
		}
		waiting[w.sock] = true
		if was[w.sock] && since >= after {
			stalled[w.c] = true
		}
	}
	if len(stalled) > 0 {
		s.closeConns(func(c io.Closer, _ trackedConn) bool { return stalled[c] })
	}
	return waiting
}

// watchToo adds sock to the sockets watched for the stream tracked as c.
func (s *Service) watchToo(c io.Closer, sock *net.TCPConn) {
	s.openMu.Lock()
	defer s.openMu.Unlock()
	if t, ok := s.open[c]; ok {
		t.socks = append(t.socks[:len(t.socks):len(t.socks)], sock)
		s.open[c] = t
	} else {
		// The connection was revoked while this socket was being opened.
		abortTCP(sock)
	}
}
