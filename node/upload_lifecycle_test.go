//go:build unix || windows

package node

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func uploadChunk(u *uploads, path, id string, offset int64, text string, done bool) error {
	_, err := u.write(path, fileParams{Upload: id, Offset: offset, Data: base64.StdEncoding.EncodeToString([]byte(text)), Done: done})
	return err
}

func waitUploadRefs(t *testing.T, u *uploads, id string, want int) {
	t.Helper()
	deadline := time.After(5 * time.Second)
	for {
		u.mu.Lock()
		lock := u.locks[id]
		n := 0
		if lock != nil {
			n = lock.refs
		}
		u.mu.Unlock()
		if n == want {
			return
		}
		select {
		case <-deadline:
			t.Fatalf("upload lock references: got %d, want %d", n, want)
		case <-time.After(time.Millisecond):
		}
	}
}

func uploadResult(t *testing.T, result <-chan error) error {
	t.Helper()
	select {
	case err := <-result:
		return err
	case <-time.After(5 * time.Second):
		t.Fatal("upload operation did not finish")
		return nil
	}
}

func TestUploadConcurrentChunksKeepOneCopy(t *testing.T) {
	u := newUploads()
	path, id := filepath.Join(t.TempDir(), "target"), "upload0001"
	if err := uploadChunk(u, path, id, 0, "head", false); err != nil {
		t.Fatal(err)
	}
	unlock := u.lock(id)
	defer func() {
		if unlock != nil {
			unlock()
		}
	}()
	results := make(chan error, 16)
	chunk := strings.Repeat("x", 64<<10)
	for i := 0; i < cap(results); i++ {
		go func() { results <- uploadChunk(u, path, id, 4, chunk, false) }()
	}
	waitUploadRefs(t, u, id, cap(results)+1)
	unlock()
	unlock = nil
	accepted := 0
	for i := 0; i < cap(results); i++ {
		if err := uploadResult(t, results); err == nil {
			accepted++
		} else if err.Error() != "upload_out_of_order" {
			t.Fatal(err)
		}
	}
	if accepted != 1 {
		t.Fatalf("accepted %d copies of one offset", accepted)
	}
	if err := uploadChunk(u, path, id, int64(4+len(chunk)), "tail", true); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil || string(data) != "head"+chunk+"tail" {
		t.Fatalf("committed bytes differ: size=%d err=%v", len(data), err)
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	if len(u.open) != 0 || len(u.locks) != 0 {
		t.Fatal("completed upload retained state or lock")
	}
}

func TestUploadRestartAndFinalizationDoNotShareOldState(t *testing.T) {
	for attempt := 0; attempt < 16; attempt++ {
		u := newUploads()
		path, id := filepath.Join(t.TempDir(), "target"), "upload0001"
		if err := uploadChunk(u, path, id, 0, "old", false); err != nil {
			t.Fatal(err)
		}
		unlock := u.lock(id)
		old, fresh := make(chan error, 1), make(chan error, 1)
		go func() { old <- uploadChunk(u, path, id, 3, "tail", true) }()
		go func() { fresh <- uploadChunk(u, path, id, 0, "new", true) }()
		func() {
			defer unlock()
			waitUploadRefs(t, u, id, 3)
		}()
		if err := uploadResult(t, fresh); err != nil {
			t.Fatal(err)
		}
		if err := uploadResult(t, old); err != nil && err.Error() != "upload_out_of_order" {
			t.Fatal(err)
		}
		if b, err := os.ReadFile(path); err != nil || string(b) != "new" {
			t.Fatalf("old upload changed replacement: %q %v", b, err)
		}
	}
}

func TestUploadLocksArePerIDAndReleasedAfterFailure(t *testing.T) {
	u := newUploads()
	dir, id := t.TempDir(), "upload0001"
	path := filepath.Join(dir, "one")
	unlock := u.lock(id)
	defer func() {
		if unlock != nil {
			unlock()
		}
	}()
	one, two := make(chan error, 1), make(chan error, 1)
	go func() { one <- uploadChunk(u, path, id, 0, "one", true) }()
	waitUploadRefs(t, u, id, 2)
	go func() { two <- uploadChunk(u, filepath.Join(dir, "two"), "upload0002", 0, "two", true) }()
	if err := uploadResult(t, two); err != nil {
		t.Fatal(err)
	}
	select {
	case <-one:
		t.Fatal("write ignored its ID lock")
	default:
	}
	unlock()
	unlock = nil
	if err := uploadResult(t, one); err != nil {
		t.Fatal(err)
	}
	_, err := u.write(path, fileParams{Upload: id, Data: "eA==", Done: true, Mode: "invalid"})
	if err == nil || err.Error() != "invalid_mode" {
		t.Fatalf("commit failure: %v", err)
	}
	if b, _ := os.ReadFile(path); string(b) != "one" {
		t.Fatalf("failed commit changed target: %q", b)
	}
	if err := uploadChunk(u, path, id, 0, "retry", true); err != nil {
		t.Fatal(err)
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	if len(u.locks) != 0 || len(u.open) != 0 {
		t.Fatal("failure/retry leaked upload state")
	}
}

func TestUploadCleanupWaitsAndRechecksIdleTime(t *testing.T) {
	for _, action := range []string{"drop", "sweep-expired", "sweep-refreshed"} {
		t.Run(action, func(t *testing.T) {
			u := newUploads()
			path, id := filepath.Join(t.TempDir(), "target"), "upload0001"
			if err := uploadChunk(u, path, id, 0, "partial", false); err != nil {
				t.Fatal(err)
			}
			unlock := u.lock(id)
			defer func() {
				if unlock != nil {
					unlock()
				}
			}()
			u.mu.Lock()
			up := u.open[id]
			up.touched = time.Now().Add(-2 * uploadIdle)
			u.mu.Unlock()
			result := make(chan error, 1)
			go func() {
				if action == "drop" {
					u.drop(id)
				} else {
					u.sweepIdle(time.Now())
				}
				result <- nil
			}()
			waitUploadRefs(t, u, id, 2)
			select {
			case <-result:
				t.Fatal("cleanup ignored the active upload lock")
			default:
			}
			if action == "sweep-refreshed" {
				u.mu.Lock()
				up.touched = time.Now()
				u.mu.Unlock()
			}
			unlock()
			unlock = nil
			if err := uploadResult(t, result); err != nil {
				t.Fatal(err)
			}
			if action == "sweep-refreshed" {
				if err := uploadChunk(u, path, id, 7, "-done", true); err != nil {
					t.Fatal(err)
				}
			} else {
				if _, err := os.Stat(up.temp); !os.IsNotExist(err) {
					t.Fatalf("cleanup left the temporary file: %v", err)
				}
				if err := uploadChunk(u, path, id, 7, "stale", true); err == nil || err.Error() != "upload_out_of_order" {
					t.Fatalf("cleaned upload accepted stale continuation: %v", err)
				}
			}
		})
	}
}
