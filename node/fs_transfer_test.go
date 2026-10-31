package node

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestVerifiedFileTransferPublishesOnlyMatchingCompleteContent(t *testing.T) {
	for _, noReplace := range []bool{true, false} {
		t.Run(map[bool]string{true: "new", false: "replace"}[noReplace], func(t *testing.T) {
			u := newUploads()
			path := filepath.Join(t.TempDir(), "target.bin")
			data := strings.Repeat("binary\x00中文", 500000)
			if !noReplace {
				if err := os.WriteFile(path, []byte("old"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			call := func(method string, p fileParams) (any, error) {
				raw, _ := json.Marshal(p)
				return u.handle(method, raw)
			}
			offset := 0
			for offset < len(data) {
				end := min(offset+1024*1024, len(data))
				_, err := call("fs.transfer", fileParams{Path: path, Upload: "transfer123", Offset: int64(offset), Data: base64.StdEncoding.EncodeToString([]byte(data[offset:end])), NoReplace: noReplace})
				if err != nil {
					t.Fatal(err)
				}
				offset = end
			}
			if noReplace {
				if _, err := os.Stat(path); !os.IsNotExist(err) {
					t.Fatal("partial target published")
				}
			} else {
				b, _ := os.ReadFile(path)
				if string(b) != "old" {
					t.Fatal("old target changed before commit")
				}
			}
			sum := sha256.Sum256([]byte(data))
			digest := hex.EncodeToString(sum[:])
			value, err := call("fs.transfer", fileParams{Path: path, Upload: "transfer123", Offset: int64(offset), Done: true, NoReplace: noReplace, Size: int64(offset), SHA256: digest})
			if err != nil {
				t.Fatal(err)
			}
			if value.(map[string]any)["sha256"] != digest {
				t.Fatal(value)
			}
			b, _ := os.ReadFile(path)
			if string(b) != data {
				t.Fatal("content differs")
			}
			hash, err := hashFile(path)
			if err != nil || hash.SHA256 != digest {
				t.Fatalf("hash: %+v %v", hash, err)
			}
			if !noReplace {
				st, _ := os.Stat(path)
				if st.Mode().Perm() != 0600 {
					t.Fatal("lost permissions")
				}
			}
			if _, err = call("fs.cancel", fileParams{Path: path, Upload: "transfer123"}); err != nil {
				t.Fatal(err)
			}
			if b, _ := os.ReadFile(path); string(b) != data {
				t.Fatal("cancel removed committed target")
			}
		})
	}
}

func TestVerifiedTransferMismatchAndCancellationKeepOldFile(t *testing.T) {
	for _, action := range []string{"digest", "size", "cancel"} {
		t.Run(action, func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "target")
			os.WriteFile(path, []byte("old"), 0600)
			u := newUploads()
			p := fileParams{Path: path, Upload: "transfer456", Data: base64.StdEncoding.EncodeToString([]byte("new"))}
			raw, _ := json.Marshal(p)
			if _, err := u.handle("fs.transfer", raw); err != nil {
				t.Fatal(err)
			}
			p.Offset = 3
			p.Data = ""
			p.Done = true
			p.Size = 3
			p.SHA256 = strings.Repeat("0", 64)
			method := "fs.transfer"
			if action == "size" {
				s := sha256.Sum256([]byte("new"))
				p.SHA256 = hex.EncodeToString(s[:])
				p.Size = 4
			}
			if action == "cancel" {
				method = "fs.cancel"
			}
			raw, _ = json.Marshal(p)
			_, err := u.handle(method, raw)
			if action != "cancel" && (err == nil || err.Error() != "checksum_mismatch") {
				t.Fatalf("mismatch: %v", err)
			}
			b, _ := os.ReadFile(path)
			if string(b) != "old" {
				t.Fatal("changed old file")
			}
			entries, _ := os.ReadDir(dir)
			if len(entries) != 1 || len(u.open) != 0 {
				t.Fatal("temporary upload leaked")
			}
		})
	}
}

func TestTransferNoReplaceIsAtomicAndEmptyFilesWork(t *testing.T) {
	dir := t.TempDir()
	temp := filepath.Join(dir, "temp")
	path := filepath.Join(dir, "target")
	os.WriteFile(temp, []byte("new"), 0600)
	os.WriteFile(path, []byte("race"), 0600)
	if err := commitNew(temp, path, ""); err == nil || err.Error() != "file_exists" {
		t.Fatalf("overwrite race: %v", err)
	}
	b, _ := os.ReadFile(path)
	if string(b) != "race" {
		t.Fatal("overwrote racing target")
	}
	path = filepath.Join(dir, "empty")
	sum := sha256.Sum256(nil)
	p := fileParams{Path: path, Upload: "empty1234", Done: true, NoReplace: true, SHA256: hex.EncodeToString(sum[:])}
	raw, _ := json.Marshal(p)
	if _, err := newUploads().handle("fs.transfer", raw); err != nil {
		t.Fatal(err)
	}
	if st, err := os.Stat(path); err != nil || st.Size() != 0 {
		t.Fatal("empty file not committed")
	}
	if _, err := hashFile(dir); err == nil || err.Error() != "not_regular_file" {
		t.Fatal("hashed directory")
	}
}
