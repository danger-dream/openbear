//go:build !windows

package browser

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

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
