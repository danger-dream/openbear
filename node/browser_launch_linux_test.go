//go:build linux

package node

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

func TestAccountDirHandsOverOnlyItsOwnTree(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("changing owners needs root")
	}
	const account = 65534
	base := t.TempDir()
	victim := filepath.Join(base, "victim")
	if err := os.Mkdir(victim, 0o700); err != nil {
		t.Fatal(err)
	}
	secret := filepath.Join(victim, "secret")
	if err := os.WriteFile(secret, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	profile := filepath.Join(base, "profile")
	sub := filepath.Join(profile, "sub")
	if err := os.MkdirAll(sub, 0o700); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 200; i++ {
		if err := os.WriteFile(filepath.Join(sub, fmt.Sprintf("f%03d", i)), nil, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Link(secret, filepath.Join(profile, "linked")); err != nil {
		t.Fatal(err)
	}

	stop, stopped := make(chan struct{}), make(chan struct{})
	var swapErr error
	go func() {
		defer close(stopped)
		real := filepath.Join(profile, "sub.real")
		for {
			select {
			case <-stop:
				return
			default:
			}
			if swapErr = os.Rename(sub, real); swapErr != nil {
				return
			}
			if swapErr = os.Symlink(victim, sub); swapErr != nil {
				return
			}
			time.Sleep(time.Microsecond)
			if swapErr = os.Remove(sub); swapErr != nil {
				return
			}
			if swapErr = os.Rename(real, sub); swapErr != nil {
				return
			}
		}
	}()
	stopSwapping := sync.OnceFunc(func() {
		close(stop)
		<-stopped
		if swapErr != nil {
			t.Error(swapErr)
		}
	})
	t.Cleanup(stopSwapping)
	runs := 0
	for deadline := time.Now().Add(3 * time.Second); time.Now().Before(deadline); runs++ {
		// Force a walk rather than the already-owned fast path.
		if err := os.Chown(profile, 0, 0); err != nil {
			t.Fatal(err)
		}
		if err := accountDir(base, "profile", account, account); err != nil {
			t.Fatal(err)
		}
		if owner(t, secret) != 0 || owner(t, victim) != 0 {
			t.Fatalf("run %d changed a file outside the tree", runs)
		}
	}
	stopSwapping()
	t.Logf("%d runs", runs)

	if err := os.Chown(profile, 0, 0); err != nil {
		t.Fatal(err)
	}
	if err := accountDir(base, "profile", account, account); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{profile, sub, filepath.Join(sub, "f000"), filepath.Join(sub, "f199")} {
		if owner(t, path) != account {
			t.Fatalf("%s not handed over", path)
		}
	}
	if owner(t, secret) != 0 {
		t.Fatal("hard-linked file handed over")
	}
}

func TestAccountDirMigratesOrdinaryTree(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("changing owners needs root")
	}
	const account = 65534
	base := t.TempDir()
	profile := filepath.Join(base, "profile")
	sub := filepath.Join(profile, "sub")
	file := filepath.Join(sub, "file")
	if err := os.MkdirAll(sub, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(file, []byte("profile data"), 0o600); err != nil {
		t.Fatal(err)
	}
	// Matching uid alone must not skip a handover to a different gid.
	if err := os.Chown(profile, account, 0); err != nil {
		t.Fatal(err)
	}
	if err := accountDir(base, "profile", account, account); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{profile, sub, file} {
		var st unix.Stat_t
		if err := unix.Lstat(path, &st); err != nil {
			t.Fatal(err)
		}
		if st.Uid != account || st.Gid != account {
			t.Fatalf("%s owner %d:%d, want %d:%d", path, st.Uid, st.Gid, account, account)
		}
	}
	if data, err := os.ReadFile(file); err != nil || string(data) != "profile data" {
		t.Fatalf("profile content changed: %q, %v", data, err)
	}

	// A tree the account already has is not walked again.
	if err := os.Chown(file, 0, 0); err != nil {
		t.Fatal(err)
	}
	if err := accountDir(base, "profile", account, account); err != nil {
		t.Fatal(err)
	}
	if owner(t, file) != 0 {
		t.Fatal("owned tree walked")
	}

	cwd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	relative, err := filepath.Rel(cwd, base)
	if err != nil {
		t.Fatal(err)
	}
	if err := accountDir(relative, "home", account, account); err != nil {
		t.Fatal(err)
	}
	var st unix.Stat_t
	if err := unix.Lstat(filepath.Join(base, "home"), &st); err != nil {
		t.Fatal(err)
	}
	if st.Uid != account || st.Gid != account || st.Mode&0o777 != 0o700 {
		t.Fatalf("new directory: owner %d:%d, mode %o", st.Uid, st.Gid, st.Mode)
	}
}

func TestAccountDirSkipsLinksAndSpecialFiles(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("changing owners needs root")
	}
	base := t.TempDir()
	victim := filepath.Join(base, "victim")
	profile := filepath.Join(base, "profile")
	for _, path := range []string{victim, profile} {
		if err := os.Mkdir(path, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	secret := filepath.Join(victim, "secret")
	if err := os.WriteFile(secret, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	for name, target := range map[string]string{"directory": victim, "file": secret, "dangling": filepath.Join(base, "absent")} {
		if err := os.Symlink(target, filepath.Join(profile, name)); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Link(secret, filepath.Join(profile, "hardlink")); err != nil {
		t.Fatal(err)
	}
	if err := unix.Mkfifo(filepath.Join(profile, "fifo"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := accountDir(base, "profile", 65534, 65534); err != nil {
		t.Fatal(err)
	}
	if owner(t, profile) != 65534 {
		t.Fatal("profile not handed over")
	}
	for _, path := range []string{victim, secret, filepath.Join(profile, "directory"), filepath.Join(profile, "file"), filepath.Join(profile, "dangling"), filepath.Join(profile, "hardlink"), filepath.Join(profile, "fifo")} {
		if owner(t, path) != 0 {
			t.Fatalf("%s changed owner", path)
		}
	}
}

func TestAccountDirRejectsLinkedDirectories(t *testing.T) {
	parts := []string{"parent", "middle", "browser", "profile"}
	for i, part := range parts {
		t.Run(part, func(t *testing.T) {
			base, outside := t.TempDir(), t.TempDir()
			for _, root := range []string{base, outside} {
				if err := os.MkdirAll(filepath.Join(append([]string{root}, parts...)...), 0o700); err != nil {
					t.Fatal(err)
				}
			}
			dir := filepath.Join(append([]string{base}, parts[:3]...)...)
			original := filepath.Join(append([]string{base}, parts[:i+1]...)...)
			target := filepath.Join(append([]string{outside}, parts[:i+1]...)...)
			// Replace a real ancestor before the call, without relying on timing.
			if err := os.Rename(original, original+".real"); err != nil {
				t.Fatal(err)
			}
			if err := os.Symlink(target, original); err != nil {
				t.Fatal(err)
			}
			if err := accountDir(dir, "profile", 65534, 65534); err == nil {
				t.Fatal("linked directory accepted")
			}
			outsideDir := filepath.Join(append([]string{outside}, parts[:3]...)...)
			if owner(t, filepath.Join(outsideDir, "profile")) != os.Getuid() {
				t.Fatal("link target changed owner")
			}
			if i < 3 {
				if err := accountDir(dir, "home", 65534, 65534); err == nil {
					t.Fatal("created directory through linked parent")
				}
				if _, err := os.Lstat(filepath.Join(outsideDir, "home")); !errors.Is(err, os.ErrNotExist) {
					t.Fatalf("outside home: %v", err)
				}
			}
		})
	}
}

func TestAccountDirRejectsInvalidLeaf(t *testing.T) {
	base := t.TempDir()
	for _, name := range []string{"", ".", "..", "nested/profile"} {
		if err := accountDir(base, name, os.Getuid(), os.Getgid()); !errors.Is(err, unix.EINVAL) {
			t.Fatalf("name %q: %v", name, err)
		}
	}
}

func TestAccountDirReturnsTraversalErrors(t *testing.T) {
	if err := handOver(-1, 0, 0); !errors.Is(err, unix.EBADF) {
		t.Fatalf("directory read error: %v", err)
	}
	if os.Geteuid() != 0 {
		t.Skip("changing owners needs root")
	}
	for _, operation := range []string{"read", "chown"} {
		t.Run(operation, func(t *testing.T) {
			base := t.TempDir()
			profile := filepath.Join(base, "profile")
			child := filepath.Join(profile, "child")
			if err := os.Mkdir(profile, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.Chown(profile, 0, 65534); err != nil {
				t.Fatal(err)
			}
			want := unix.EACCES
			if operation == "read" {
				if err := os.Mkdir(child, 0); err != nil {
					t.Fatal(err)
				}
			} else {
				if err := os.WriteFile(child, nil, 0o600); err != nil {
					t.Fatal(err)
				}
				if err := os.Chown(child, 65534, 65534); err != nil {
					t.Fatal(err)
				}
				want = unix.EPERM
			}
			err := func() error {
				// Capabilities are thread-local; restore them before releasing this thread.
				runtime.LockOSThread()
				header := unix.CapUserHeader{Version: unix.LINUX_CAPABILITY_VERSION_3}
				var original [2]unix.CapUserData
				if err := unix.Capget(&header, &original[0]); err != nil {
					runtime.UnlockOSThread()
					t.Fatal(err)
				}
				defer func() {
					if err := unix.Capset(&header, &original[0]); err != nil {
						t.Fatalf("restore capabilities: %v", err)
					}
					runtime.UnlockOSThread()
				}()
				limited := original
				limited[0].Effective &^= 1<<unix.CAP_CHOWN | 1<<unix.CAP_DAC_OVERRIDE | 1<<unix.CAP_DAC_READ_SEARCH
				if err := unix.Capset(&header, &limited[0]); err != nil {
					t.Fatal(err)
				}
				return accountDir(base, "profile", 0, 0)
			}()
			if !errors.Is(err, want) {
				t.Fatalf("handover error: %v, want %v", err, want)
			}
			var st unix.Stat_t
			if err := unix.Lstat(profile, &st); err != nil {
				t.Fatal(err)
			}
			if st.Gid != 65534 {
				t.Fatal("failed handover marked complete")
			}
			if err := accountDir(base, "profile", 0, 0); err != nil {
				t.Fatalf("retry handover: %v", err)
			}
			if err := unix.Lstat(profile, &st); err != nil || st.Gid != 0 {
				t.Fatalf("retry did not complete: gid %d, %v", st.Gid, err)
			}
		})
	}
}

// From the 10-30 check: Ubuntu's chromium-browser is a script starting the snap, which cannot
// use a profile outside the user's home; it was picked and failed to start.
func TestASnapWrapperIsNoCandidate(t *testing.T) {
	dir := t.TempDir()
	wrapper := filepath.Join(dir, "chromium-browser")
	os.WriteFile(wrapper, []byte("#!/bin/sh\nexec /snap/bin/chromium \"$@\"\n"), 0o755)
	plain := filepath.Join(dir, "chromium")
	os.WriteFile(plain, []byte("\x7fELF"), 0o755)
	if !snapWrapper(wrapper) || snapWrapper(plain) {
		t.Fatal("snap wrapper not told apart")
	}
	t.Setenv("PATH", dir)
	for _, c := range browserCandidates() {
		if c == wrapper {
			t.Fatalf("candidates %v", browserCandidates())
		}
	}
}
