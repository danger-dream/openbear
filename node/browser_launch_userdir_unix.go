//go:build darwin || linux

package node

import (
	"errors"
	"path/filepath"

	"golang.org/x/sys/unix"
)

// makeUserDirs creates home/parts... as uid's directories for a root node.
// The user owns home and could link any step elsewhere, so every step is
// opened without following links and ownership is changed on the opened
// directory itself. Steps from owned on are given to the user even when
// they already exist; earlier ones only when created here.
func makeUserDirs(home string, parts []string, owned, uid, gid int) error {
	fd, err := unix.Open(home, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	defer func() { unix.Close(fd) }()
	for i, name := range parts {
		err := unix.Mkdirat(fd, name, 0o700)
		if err != nil && !errors.Is(err, unix.EEXIST) {
			return err
		}
		created := err == nil
		next, err := unix.Openat(fd, name, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
		if err != nil {
			return errors.New("not a plain directory: " + filepath.Join(append([]string{home}, parts[:i+1]...)...))
		}
		unix.Close(fd)
		fd = next
		if created || i >= owned {
			if err = unix.Fchown(fd, uid, gid); err != nil {
				return err
			}
		}
	}
	return nil
}
