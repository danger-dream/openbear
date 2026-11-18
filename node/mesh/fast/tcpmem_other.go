//go:build !linux

package fast

type tcpMemManager struct{}

func (*tcpMemManager) init(_ string)         {}
func (*tcpMemManager) apply()                {}
func (*tcpMemManager) restore()              {}
func (*tcpMemManager) status() *TCPMemStatus { return nil }

// SweepTCPMem is a no-op outside Linux.
func SweepTCPMem(_ string) {}
