//go:build !linux && !darwin

package fast

import (
	"net"
	"time"
)

// Windows gives up on unacknowledged data after five resends of its own.
func unanswered(*net.TCPConn) (time.Duration, bool) { return 0, false }

func limitRetransmit(*net.TCPConn) {}
