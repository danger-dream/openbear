//go:build !linux

package bypass

import "syscall"

// Only Linux nodes look for a transparent proxy; elsewhere the rules are never
// in place and these are not reached.
func control(syscall.RawConn) error { return nil }

func socket(fn func() error) error { return fn() }

func SetProcess() error { return nil }
