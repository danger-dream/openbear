//go:build !windows

package browser

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"testing"
	"time"
)

func downloadIdentityForTest(t *testing.T, path string) fileIdentity {
	t.Helper()
	f, err := openRegular(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	id, _, err := identify(f)
	if err != nil {
		t.Fatal(err)
	}
	return id
}

// Split at the actual atomic move, not at an earlier open: these deterministically
// cover the old final-stat/unlink race without timing or global syscall hooks.
func TestNP02ReplacementBetweenInspectionAndIsolationIsRestored(t *testing.T) {
	for _, kind := range []string{"file", "symlink", "directory", "hardlink"} {
		for _, occupied := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/occupied=%v", kind, occupied), func(t *testing.T) {
				root := t.TempDir()
				dir, err := OpenDownloadDir(root, false)
				if err != nil {
					t.Fatal(err)
				}
				defer dir.Close()
				path := filepath.Join(root, guidA)
				if err := os.WriteFile(path, []byte("download"), 0600); err != nil {
					t.Fatal(err)
				}
				id := downloadIdentityForTest(t, path)
				if err := os.Rename(path, path+".old"); err != nil {
					t.Fatal(err)
				}
				outside := filepath.Join(root, "outside")
				if err := os.WriteFile(outside, []byte("unrelated"), 0640); err != nil {
					t.Fatal(err)
				}
				switch kind {
				case "file":
					err = os.WriteFile(path, []byte("replacement"), 0640)
				case "symlink":
					err = os.Symlink(outside, path)
				case "directory":
					err = os.Mkdir(path, 0750)
					if err == nil {
						err = os.WriteFile(filepath.Join(path, "child"), []byte("child"), 0600)
					}
				case "hardlink":
					err = os.Link(outside, path)
				}
				if err != nil {
					t.Fatal(err)
				}
				before, err := os.Lstat(path)
				if err != nil {
					t.Fatal(err)
				}
				q, err := isolateDownload(dir, guidA)
				if err != nil {
					t.Fatal(err)
				}
				defer q.private.Close()
				if occupied {
					if err := os.WriteFile(path, []byte("new occupant"), 0600); err != nil {
						t.Fatal(err)
					}
				}
				if err := q.remove(id, true); !errors.Is(err, errNotRegular) {
					t.Fatalf("mismatch: %v", err)
				}
				restored := path
				if occupied {
					if data, err := os.ReadFile(path); err != nil || string(data) != "new occupant" {
						t.Fatalf("overwrote occupant: %q %v", data, err)
					}
					recovered, err := filepath.Glob(filepath.Join(root, ".openbear-recovered-*"))
					if err != nil || len(recovered) != 1 {
						t.Fatalf("recovery: %v %v", recovered, err)
					}
					restored = recovered[0]
				}
				after, err := os.Lstat(restored)
				if err != nil || !os.SameFile(before, after) || before.Mode() != after.Mode() {
					t.Fatalf("replacement not restored intact: %v %v", after, err)
				}
				if data, err := os.ReadFile(outside); err != nil || string(data) != "unrelated" {
					t.Fatalf("outside modified: %q %v", data, err)
				}
				if kind == "directory" {
					if data, err := os.ReadFile(filepath.Join(restored, "child")); err != nil || string(data) != "child" {
						t.Fatal("directory contents lost", err)
					}
				}
				entries, err := q.private.ReadDir(-1)
				if err != nil || len(entries) != 0 {
					t.Fatalf("recoverable replacement left hidden: %v %v", entries, err)
				}
			})
		}
	}
}

func TestNP02NewHardLinkIsNotDeletedAfterIsolation(t *testing.T) {
	root := t.TempDir()
	dir, err := OpenDownloadDir(root, false)
	if err != nil {
		t.Fatal(err)
	}
	defer dir.Close()
	path := filepath.Join(root, guidA)
	if err := os.WriteFile(path, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	id := downloadIdentityForTest(t, path)
	q, err := isolateDownload(dir, guidA)
	if err != nil {
		t.Fatal(err)
	}
	defer q.private.Close()
	alias := filepath.Join(root, "alias")
	if err := os.Link(filepath.Join(q.private.Name(), q.name), alias); err != nil {
		t.Fatal(err)
	}
	if err := q.remove(id, true); !errors.Is(err, errNotRegular) {
		t.Fatalf("new hard link accepted: %v", err)
	}
	for _, name := range []string{path, alias} {
		if data, err := os.ReadFile(name); err != nil || string(data) != "keep" {
			t.Fatalf("lost link: %q %v", data, err)
		}
	}
}

func TestNP02IsolationFDRejectsPublicDirectoryReplacement(t *testing.T) {
	root := t.TempDir()
	dir, err := OpenDownloadDir(root, false)
	if err != nil {
		t.Fatal(err)
	}
	defer dir.Close()
	path := filepath.Join(root, guidA)
	if err := os.WriteFile(path, []byte("download"), 0600); err != nil {
		t.Fatal(err)
	}
	id := downloadIdentityForTest(t, path)
	q, err := isolateDownload(dir, guidA)
	if err != nil {
		t.Fatal(err)
	}
	defer q.private.Close()
	originalDir := q.private.Name()
	if err := os.Rename(originalDir, originalDir+"-moved"); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(originalDir, 0700); err != nil {
		t.Fatal(err)
	}
	victim := filepath.Join(originalDir, q.name)
	if err := os.WriteFile(victim, []byte("keep"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("new download"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := q.remove(id, true); err != nil {
		t.Fatal(err)
	}
	for p, expected := range map[string]string{victim: "keep", path: "new download"} {
		if data, err := os.ReadFile(p); err != nil || string(data) != expected {
			t.Fatalf("wrong entry changed: %q %v", data, err)
		}
	}
	if _, err := os.Lstat(filepath.Join(originalDir+"-moved", q.name)); !os.IsNotExist(err) {
		t.Fatal("identified download not deleted", err)
	}
}

func TestNP02NormalAutomaticCleanupAndPrivateNameCollision(t *testing.T) {
	root := t.TempDir()
	if err := os.Chmod(root, 0777); err != nil {
		t.Fatal(err)
	}
	// A public/symlink namesake must not be chmodded, traversed or trusted.
	outside := t.TempDir()
	name := filepath.Join(root, fmt.Sprintf(".openbear-cleanup-%d", os.Geteuid()))
	if err := os.Symlink(outside, name); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		path := filepath.Join(root, guidA)
		if err := os.WriteFile(path, []byte("download"), 0600); err != nil {
			t.Fatal(err)
		}
		var err error
		if i == 0 {
			err = removeIdentifiedFile(path, downloadIdentityForTest(t, path))
		} else {
			err = removeFile(path)
		}
		if err != nil {
			t.Fatal(err)
		}
		if _, err := os.Lstat(path); !os.IsNotExist(err) {
			t.Fatal("automatic cleanup disabled", err)
		}
	}
	if info, err := os.Stat(root); err != nil || info.Mode().Perm() != 0777 {
		t.Fatal("shared directory permissions changed", err)
	}
	if got, err := os.Readlink(name); err != nil || got != outside {
		t.Fatal("namesake changed", err)
	}
	if entries, err := os.ReadDir(outside); err != nil || len(entries) != 0 {
		t.Fatal("followed foreign directory", err)
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatal(err)
	}
	privateDirs := 0
	for _, entry := range entries {
		if entry.IsDir() && strings.HasPrefix(entry.Name(), ".openbear-cleanup-") {
			privateDirs++
		}
	}
	if privateDirs != 1 {
		t.Fatalf("did not reuse fallback cleanup directory: %d", privateDirs)
	}
}

func TestNP02PrivateChildrenDenyOtherUIDEvenWithSharedParent(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("requires root only to run the synthetic attacker as another UID")
	}
	root := t.TempDir()
	if err := os.Chmod(root, 0777); err != nil {
		t.Fatal(err)
	}
	dir, err := OpenDownloadDir(root, false)
	if err != nil {
		t.Fatal(err)
	}
	defer dir.Close()
	// A positive control ensures /dev/fd and the credential switch really work.
	runAsOther := func(fd *os.File, script string) error {
		cmd := exec.Command("/bin/sh", "-c", script)
		cmd.ExtraFiles = []*os.File{fd}
		cmd.SysProcAttr = &syscall.SysProcAttr{Credential: &syscall.Credential{Uid: 65534, Gid: 65534}}
		return cmd.Run()
	}
	if err := runAsOther(dir, "printf control > /dev/fd/3/control"); err != nil {
		t.Skip("credential/FD test unavailable:", err)
	}
	if runtime.GOOS == "darwin" {
		// Exercise inherited macOS ACLs, which can grant access despite 0700.
		acl := "everyone allow list,add_file,search,delete_child,add_subdirectory,file_inherit,directory_inherit"
		if out, err := exec.Command("/bin/chmod", "+a", acl, root).CombinedOutput(); err != nil {
			t.Fatalf("synthetic inherited ACL: %v %s", err, out)
		}
	}
	private, err := openDownloadRemovalDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer private.Close()
	if info, err := private.Stat(); err != nil || info.Mode().Perm() != 0700 {
		t.Fatalf("private permissions: %v %v", info, err)
	}
	if err := os.WriteFile(filepath.Join(private.Name(), "target"), []byte("keep"), 0666); err != nil {
		t.Fatal(err)
	}
	for _, script := range []string{
		"printf attack > /dev/fd/3/injected",
		"/bin/mv /dev/fd/3/target /dev/fd/3/replaced",
		"/bin/rm /dev/fd/3/target",
	} {
		if err := runAsOther(private, script); err == nil {
			t.Fatalf("other UID altered private namespace: %s", script)
		}
	}
	if data, err := os.ReadFile(filepath.Join(private.Name(), "target")); err != nil || string(data) != "keep" {
		t.Fatal("protected data changed", err)
	}
	if !strings.HasPrefix(filepath.Base(private.Name()), ".openbear-cleanup-") {
		t.Fatal("unexpected private directory")
	}
}

func TestDownloadsNeverTraverseAncestorLinks(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(root, "outside")
	if err := os.Mkdir(outside, 0o700); err != nil {
		t.Fatal(err)
	}
	victim := filepath.Join(outside, guidA)
	if err := os.WriteFile(victim, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-2 * downloadTTL)
	os.Chtimes(victim, old, old)
	link := filepath.Join(root, "link")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}
	if file, err := openRegular(filepath.Join(link, guidA)); err == nil {
		file.Close()
		t.Fatal("opened through ancestor link")
	}
	if err := removeFile(filepath.Join(link, guidA)); err == nil {
		t.Fatal("removed through ancestor link")
	}
	if err := mkdirAll(filepath.Join(link, "new")); err == nil {
		t.Fatal("created through ancestor link")
	}
	e, _, _ := testEngine(t, nil)
	loop(e, func() {
		e.config.Set("downloadHostPath", link)
		e.files.prune()
	})
	if data, err := os.ReadFile(victim); err != nil || string(data) != "keep" {
		t.Fatalf("outside file changed: %q %v", data, err)
	}
	if _, err := os.Stat(filepath.Join(outside, "new")); !os.IsNotExist(err) {
		t.Fatal("created outside directory")
	}
}

func TestDownloadListingKeepsOpenedDirectoryMetadata(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "downloads")
	if err := mkdirAll(dir); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, guidA), []byte("original"), 0o600); err != nil {
		t.Fatal(err)
	}
	entries, err := readDir(dir)
	if err != nil || len(entries) != 1 {
		t.Fatalf("list: %v %v", entries, err)
	}
	if err := os.Rename(dir, dir+"-old"); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	os.WriteFile(filepath.Join(dir, guidA), []byte("different length"), 0o600)
	info, err := entries[0].Info()
	if err != nil || info.Size() != int64(len("original")) {
		t.Fatalf("metadata followed replaced path: %v %v", info, err)
	}
}
