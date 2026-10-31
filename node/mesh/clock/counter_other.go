//go:build !windows

package clock

import "time"

// counter: time.Now resolves to the nanosecond here.
func counter() func() time.Duration { return nil }
