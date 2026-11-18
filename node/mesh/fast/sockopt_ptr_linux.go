//go:build linux

package fast

import (
	"unsafe"

	"golang.org/x/sys/unix"
)

func rawPointer(m *unix.IPv6MTUInfo) unsafe.Pointer {
	return unsafe.Pointer(m)
}
