//go:build unix || windows

package node

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestSafeWriteNoReplaceSurvivesRacingDestination(t *testing.T) {
	for _, chunked := range []bool{false, true} {
		dir := t.TempDir()
		path := filepath.Join(dir, "target")
		u := newUploads()
		p := fileParams{Path: path, Data: "bmV3", Done: !chunked}
		if chunked {
			p.Upload = "safety0003"
			raw, _ := json.Marshal(p)
			if _, err := u.handle("fs.writeSafe", raw); err != nil {
				t.Fatal(err)
			}
			p.Offset, p.Data, p.Done = 3, "", true
		}
		os.WriteFile(path, []byte("race"), 0600)
		raw, _ := json.Marshal(p)
		if _, err := u.handle("fs.writeSafe", raw); err == nil || err.Error() != "file_exists" {
			t.Fatalf("noReplace race: %v", err)
		}
		if b, _ := os.ReadFile(path); string(b) != "race" {
			t.Fatal("overwrote racing destination")
		}
		entries, _ := os.ReadDir(dir)
		if len(entries) != 1 || len(u.open) != 0 {
			t.Fatalf("left staging objects: %v", entries)
		}
	}
}

func TestUploadContinuationCannotDowngradeNoReplace(t *testing.T) {
	u := newUploads()
	path := filepath.Join(t.TempDir(), "target")
	_, err := u.write(path, fileParams{Upload: "safety0004", Data: "eA==", NoReplace: true})
	if err != nil {
		t.Fatal(err)
	}
	defer u.drop("safety0004")
	_, err = u.write(path, fileParams{Upload: "safety0004", Offset: 1, Done: true})
	if err == nil || err.Error() != "upload_out_of_order" {
		t.Fatalf("downgraded contract: %v", err)
	}
}
