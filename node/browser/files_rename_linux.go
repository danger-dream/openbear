//go:build linux

package browser

import (
	"errors"
	"os"

	"golang.org/x/sys/unix"
)

// Linux POSIX ACL entries are bounded by the group permission mask: the
// verified 0700 mode already denies every UID except the directory owner.
func secureDownloadRemovalDir(*os.File) error { return nil }

func renameDownloadNoReplace(from int, old string, to int, name string) error {
	err := unix.Renameat2(from, old, to, name, unix.RENAME_NOREPLACE)
	if !errors.Is(err, unix.ENOSYS) && !errors.Is(err, unix.EINVAL) && !errors.Is(err, unix.EOPNOTSUPP) {
		return err
	}
	// Older kernels/filesystems still allow a no-replace restore of files and
	// symlinks. No FOLLOW flag: never restore a symlink's target instead. A raced
	// directory cannot use this fallback and is safely retained in quarantine.
	if err := unix.Linkat(from, old, to, name, 0); err != nil {
		return err
	}
	return unix.Unlinkat(from, old, 0)
}
