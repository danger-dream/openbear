//go:build linux

package fast

import (
	"net"
	"time"

	"golang.org/x/sys/unix"
)

// unanswered reports how long ago c's peer last acknowledged anything, when
// data or two window or keepalive probes in a row wait on c. A receiver that
// only stopped reading answers every probe and keeps them under two.
func unanswered(c *net.TCPConn) (time.Duration, bool) {
	rc, err := c.SyscallConn()
	if err != nil {
		return 0, false
	}
	var info *unix.TCPInfo
	var gerr error
	if err := rc.Control(func(fd uintptr) {
		info, gerr = unix.GetsockoptTCPInfo(int(fd), unix.IPPROTO_TCP, unix.TCP_INFO)
	}); err != nil || gerr != nil {
		return 0, false
	}
	if info.Unacked == 0 && info.Probes < 2 {
		return 0, false
	}
	return time.Duration(info.Last_ack_recv) * time.Millisecond, true
}

func limitRetransmit(*net.TCPConn) {}
