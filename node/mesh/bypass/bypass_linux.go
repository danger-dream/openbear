//go:build linux

package bypass

import (
	"runtime"
	"syscall"

	"golang.org/x/sys/unix"
)

func control(c syscall.RawConn) error {
	var serr error
	err := c.Control(func(fd uintptr) { serr = unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_MARK, Mark) })
	if err != nil {
		return err
	}
	return serr
}

// socket gives this thread GID as its file system group while fn runs: a
// socket records the creator's credentials, which nftables matches as skgid.
// setfsgid changes the calling thread alone, unlike setgid.
func socket(fn func() error) error {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	before, err := unix.SetfsgidRetGid(GID)
	if err != nil {
		return fn()
	}
	defer unix.Setfsgid(before)
	return fn()
}

// SetProcess makes GID the group of this whole process, so every socket the
// mesh data plane opens is bypassed; it stays root.
func SetProcess() error {
	return syscall.Setgid(GID)
}
