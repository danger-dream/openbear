//go:build !linux

package fast

import "testing"

// isolateHost: tcp_mem is tuned on Linux only.
func isolateHost(*testing.T, *Service) {}
