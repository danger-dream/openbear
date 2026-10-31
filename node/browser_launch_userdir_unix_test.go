//go:build darwin || linux

package node

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func owner(t *testing.T, path string) int {
	t.Helper()
	info, err := os.Lstat(path)
	if err != nil {
		t.Fatal(err)
	}
	return int(info.Sys().(*syscall.Stat_t).Uid)
}

func TestMakeUserDirsNeverFollowsTheUsersLinks(t *testing.T) {
	uid, gid := os.Getuid(), os.Getgid()
	if uid == 0 {
		uid, gid = 65534, 65534
	}
	parts := []string{"Library", "Application Support", "OpenBear", "Browser", "OpenBear Downloads"}
	home := t.TempDir()
	os.Mkdir(filepath.Join(home, "Library"), 0o700)
	if err := makeUserDirs(home, parts, 2, uid, gid); err != nil {
		t.Fatal(err)
	}
	path := home
	for i, part := range parts {
		path = filepath.Join(path, part)
		// The existing Library stays as it was; the rest is the user's.
		want := uid
		if i == 0 {
			want = os.Getuid()
		}
		if owner(t, path) != want {
			t.Fatalf("%s owner %d, want %d", part, owner(t, path), want)
		}
	}

	for i := range parts {
		home := t.TempDir()
		elsewhere := t.TempDir()
		if i > 0 {
			os.MkdirAll(filepath.Join(append([]string{home}, parts[:i]...)...), 0o700)
		}
		if err := os.Symlink(elsewhere, filepath.Join(append([]string{home}, parts[:i+1]...)...)); err != nil {
			t.Fatal(err)
		}
		if err := makeUserDirs(home, parts, 2, uid, gid); err == nil {
			t.Fatalf("link at %s followed", parts[i])
		}
		if entries, _ := os.ReadDir(elsewhere); len(entries) != 0 || owner(t, elsewhere) != os.Getuid() {
			t.Fatalf("link at %s: target changed", parts[i])
		}
	}
}
