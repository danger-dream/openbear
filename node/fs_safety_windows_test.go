//go:build windows

package node

import (
	"os"
	"path/filepath"
	"testing"
)

func TestWindowsUploadHoldsSourceAgainstRenameAndWrite(t *testing.T) {
	u := newUploads()
	dir := t.TempDir()
	path := filepath.Join(dir, "target")
	if err := uploadChunk(u, path, "windows0001", 0, "head", false); err != nil {
		t.Fatal(err)
	}
	temp := u.open["windows0001"].temp
	defer u.drop("windows0001")
	if err := os.Rename(temp, temp+".replaced"); err == nil {
		t.Fatal("source handle allowed rename")
	}
	if f, err := os.OpenFile(temp, os.O_WRONLY|os.O_TRUNC, 0600); err == nil {
		f.Close()
		t.Fatal("source handle allowed outside writer")
	}
	if err := uploadChunk(u, path, "windows0001", 4, "tail", true); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(path); string(b) != "headtail" {
		t.Fatalf("wrong committed source %q", b)
	}
}
