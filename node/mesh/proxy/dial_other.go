//go:build !linux

package proxy

import "syscall"

// bindControl: binding to an interface is Linux's alone; the controller
// offers it only there.
func bindControl(iface string) func(network, address string, c syscall.RawConn) error { return nil }
