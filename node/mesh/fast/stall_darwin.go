//go:build darwin

package fast

import (
	"net"
	"time"

	"golang.org/x/sys/unix"
)

// unanswered: macOS tells no time of the last acknowledgement; there
// limitRetransmit has the kernel drop a stalled socket instead.
func unanswered(*net.TCPConn) (time.Duration, bool) { return 0, false }

// limitRetransmit has the kernel drop c once what it resends goes
// unacknowledged for stallAfter, not after minutes. Window probes a paused
// receiver answers do not count.
func limitRetransmit(c *net.TCPConn) {
	rc, err := c.SyscallConn()
	if err != nil {
		return
	}
	_ = rc.Control(func(fd uintptr) {
		_ = unix.SetsockoptInt(int(fd), unix.IPPROTO_TCP, unix.TCP_RXT_CONNDROPTIME, int(stallAfter/time.Second))
	})
}
