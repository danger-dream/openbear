//go:build unix

package node

import (
	"encoding/base64"
	"golang.org/x/sys/unix"
	"io/fs"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestUploadReplacementNeverWritesOrPublishesSubstitutedObject(t *testing.T) {
	dir := t.TempDir()
	victim, path := filepath.Join(dir, "victim"), filepath.Join(dir, "target")
	os.WriteFile(victim, []byte("keep"), 0600)
	u := newUploads()
	if err := uploadChunk(u, path, "safety0001", 0, "head", false); err != nil {
		t.Fatal(err)
	}
	up := u.open["safety0001"]
	if err := os.Remove(up.temp); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(victim, up.temp); err != nil {
		t.Fatal(err)
	}
	if err := uploadChunk(u, path, "safety0001", 4, "tail", true); err == nil || err.Error() != "upload_changed" {
		t.Fatalf("commit substituted object: %v", err)
	}
	if b, _ := os.ReadFile(victim); string(b) != "keep" {
		t.Fatalf("wrote through substituted link: %q", b)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("published replacement")
	}
}

func TestUploadPinnedPrivateDirectorySurvivesPublicNameReplacement(t *testing.T) {
	dir := t.TempDir()
	u := newUploads()
	path := filepath.Join(dir, "target")
	if err := uploadChunk(u, path, "safety0002", 0, "head", false); err != nil {
		t.Fatal(err)
	}
	up := u.open["safety0002"]
	stageDir := filepath.Dir(up.temp)
	info, _ := os.Stat(stageDir)
	if info.Mode().Perm() != 0700 {
		t.Fatalf("staging directory not private: %v", info.Mode())
	}
	moved := stageDir + "-moved"
	if err := os.Rename(stageDir, moved); err != nil {
		t.Fatal(err)
	}
	fake := filepath.Join(dir, "fake")
	os.Mkdir(fake, 0700)
	os.WriteFile(filepath.Join(fake, "data"), []byte("untrusted"), 0600)
	if err := os.Symlink(fake, stageDir); err != nil {
		t.Fatal(err)
	}
	if err := uploadChunk(u, path, "safety0002", 4, "tail", true); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(path); string(b) != "headtail" {
		t.Fatalf("lost pinned source: %q", b)
	}
	if b, _ := os.ReadFile(filepath.Join(fake, "data")); string(b) != "untrusted" {
		t.Fatal("touched substituted directory")
	}
}

func TestUploadPinsDestinationDirectoryAcrossRename(t *testing.T) {
	root := t.TempDir()
	dir, moved := filepath.Join(root, "dest"), filepath.Join(root, "moved")
	os.Mkdir(dir, 0700)
	path := filepath.Join(dir, "target")
	u := newUploads()
	if err := uploadChunk(u, path, "safety0005", 0, "head", false); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(dir, moved); err != nil {
		t.Fatal(err)
	}
	os.Mkdir(dir, 0700)
	os.WriteFile(path, []byte("unrelated"), 0600)
	if err := uploadChunk(u, path, "safety0005", 4, "tail", true); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(path); string(b) != "unrelated" {
		t.Fatal("commit followed substituted parent")
	}
	if b, _ := os.ReadFile(filepath.Join(moved, "target")); string(b) != "headtail" {
		t.Fatalf("lost original directory: %q", b)
	}
}

func TestFileModeSpecialBitsAndTargetSymlinkPreserved(t *testing.T) {
	for _, mode := range []uint64{04755, 02750, 01777, 07755} {
		if got := uint64(posixMode(goMode(mode))); got != mode {
			t.Fatalf("mode round trip %o != %o", got, mode)
		}
	}
	dir := t.TempDir()
	dest, link := filepath.Join(dir, "dest"), filepath.Join(dir, "link")
	os.WriteFile(dest, []byte("old"), 0600)
	if err := os.Chmod(dest, goMode(06751)); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(dest, link); err != nil {
		t.Fatal(err)
	}
	u := newUploads()
	if _, err := u.write(link, fileParams{Data: base64.StdEncoding.EncodeToString([]byte("new"))}); err != nil {
		t.Fatal(err)
	}
	info, _ := os.Stat(dest)
	if got := describe(dest, "dest", info).Mode; got != "6751" {
		t.Fatalf("chown/chmod lost special bits: %s", got)
	}
	if l, _ := os.Lstat(link); l.Mode()&fs.ModeSymlink == 0 {
		t.Fatal("replaced target symlink")
	}
	if b, _ := os.ReadFile(dest); string(b) != "new" {
		t.Fatal("did not follow existing target link")
	}
	if _, err := u.write(dest, fileParams{Data: "", Mode: "4755"}); err != nil {
		t.Fatal(err)
	}
	info, _ = os.Stat(dest)
	if posixMode(info.Mode()) != 04755 {
		t.Fatal("explicit mode lost setuid")
	}
}

func TestNoReplaceCountsDanglingTargetLinkAsExisting(t *testing.T) {
	dir := t.TempDir()
	target, missing := filepath.Join(dir, "link"), filepath.Join(dir, "missing")
	if err := os.Symlink(missing, target); err != nil {
		t.Fatal(err)
	}
	_, err := newUploads().write(target, fileParams{NoReplace: true, Data: "eA=="})
	if err == nil || err.Error() != "file_exists" {
		t.Fatalf("overwrote dangling link: %v", err)
	}
	if info, err := os.Lstat(target); err != nil || info.Mode()&fs.ModeSymlink == 0 {
		t.Fatal("lost target link")
	}
	if _, err := os.Stat(missing); !os.IsNotExist(err) {
		t.Fatal("noReplace followed dangling link")
	}
}

func TestFIFOReadAndHashRejectBeforeBlockingIncludingLinks(t *testing.T) {
	dir := t.TempDir()
	fifo, link := filepath.Join(dir, "fifo"), filepath.Join(dir, "link")
	if err := unix.Mkfifo(fifo, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(fifo, link); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{fifo, link} {
		for _, hash := range []bool{false, true} {
			done := make(chan error, 1)
			go func() {
				var err error
				if hash {
					_, err = hashFile(path)
				} else {
					_, err = read(path, 0, 4)
				}
				done <- err
			}()
			select {
			case err := <-done:
				if err == nil || err.Error() != "not_regular_file" {
					t.Fatalf("FIFO accepted: %v", err)
				}
			case <-time.After(time.Second):
				fd, _ := unix.Open(fifo, unix.O_WRONLY|unix.O_NONBLOCK, 0)
				if fd >= 0 {
					unix.Close(fd)
				}
				t.Fatal("FIFO blocked before fstat")
			}
		}
	}
}
