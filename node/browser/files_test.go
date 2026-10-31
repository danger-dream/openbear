package browser

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

const (
	guidA = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
	guidB = "1b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e"
	guidC = "2c3d4e5f-6a7b-4c8d-ae9f-1a2b3c4d5e6f"
)

func saveError(e *Engine, page *Page, id int, out string) string {
	_, err := e.files.execute(context.Background(), page, O("op", "save", "id", id, "outputPath", out))
	if err == nil {
		return ""
	}
	return err.(*Error).Code
}

func TestUnownedDownloadsAreNotCancelled(t *testing.T) {
	e, page, fb := testEngine(t, nil)
	loop(e, func() {
		// A GUID that is not one is never used as a file name.
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", "../../etc/passwd", "suggestedFilename", "x"))
		e.files.event("Browser.downloadWillBegin", O("frameId", "unowned-frame", "guid", guidA, "suggestedFilename", "personal"))
		// Begun before this engine attached.
		e.files.event("Browser.downloadProgress", O("guid", guidA, "state", "inProgress", "receivedBytes", 1))
		e.files.event("Browser.downloadProgress", O("guid", guidB, "state", "completed", "receivedBytes", 1))
		if page.downloads.Len() != 0 {
			t.Fatal("invalid download adopted")
		}
	})
	loop(e, func() { e.block(e.wg.Wait) })
	if n := fb.count("Browser.cancelDownload"); n != 0 {
		t.Fatalf("cancelled %d unowned downloads: %v", n, fb.methods())
	}
	// A closed page's downloads go with it.
	loop(e, func() {
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidC, "suggestedFilename", "x"))
	})
	e.onEvent("Target.targetDestroyed", O("targetId", page.target), "")
	loop(e, func() {
		if len(e.files.byGUID) != 0 {
			t.Fatal("closed page's download still tracked")
		}
	})
	for deadline := time.Now().Add(2 * time.Second); fb.count("Browser.cancelDownload") < 1; time.Sleep(10 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatalf("closed page's download not cancelled: %v", fb.methods())
		}
	}
}

func TestDownloadDirectoryQuota(t *testing.T) {
	defer func(q int64) { downloadQuota = q }(downloadQuota)
	downloadQuota = 1000
	e, page, fb := testEngine(t, nil)
	host := asStr(e.config.Must("downloadHostPath"))
	// Left by an earlier engine, still within its time.
	os.WriteFile(filepath.Join(host, guidC), make([]byte, 300), 0o600)
	loop(e, func() {
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidA, "suggestedFilename", "a"))
		e.files.event("Browser.downloadProgress", O("guid", guidA, "state", "completed", "receivedBytes", 500))
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidB, "suggestedFilename", "b"))
		e.files.event("Browser.downloadProgress", O("guid", guidB, "state", "inProgress", "receivedBytes", 150))
	})
	if fb.count("Browser.cancelDownload") != 0 {
		t.Fatal("cancelled within the quota")
	}
	loop(e, func() {
		e.files.event("Browser.downloadProgress", O("guid", guidB, "state", "inProgress", "receivedBytes", 250))
	})
	waitMethod(t, fb, "Browser.cancelDownload")
	loop(e, func() {
		if code := saveError(e, page, 2, filepath.Join(t.TempDir(), "b")); code != "download_quota_exceeded" {
			t.Fatalf("over quota: %q", code)
		}
	})
}

func TestExpiredDownloadsArePruned(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	host := asStr(e.config.Must("downloadHostPath"))
	old := time.Now().Add(-2 * downloadTTL)
	for _, name := range []string{guidB, "keep.txt"} {
		os.WriteFile(filepath.Join(host, name), []byte("x"), 0o600)
		os.Chtimes(filepath.Join(host, name), old, old)
	}
	os.WriteFile(filepath.Join(host, guidC), []byte("recent"), 0o600)
	os.WriteFile(filepath.Join(host, guidA), []byte("done"), 0o600)
	loop(e, func() {
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidA, "suggestedFilename", "a"))
		e.files.event("Browser.downloadProgress", O("guid", guidA, "state", "completed", "receivedBytes", 4))
		if saveError(e, page, 1, filepath.Join(t.TempDir(), "a")) != "" {
			t.Fatal("fresh download not saved")
		}
		record, _ := page.downloads.Get(1)
		record.updated = old
		if code := saveError(e, page, 1, filepath.Join(t.TempDir(), "a")); code != "download_expired" {
			t.Fatalf("expired download: %q", code)
		}
		if e.files.untracked != int64(len("recent")) {
			t.Fatalf("untracked %d", e.files.untracked)
		}
	})
	for name, kept := range map[string]bool{guidA: false, guidB: false, guidC: true, "keep.txt": true} {
		if _, err := os.Lstat(filepath.Join(host, name)); (err == nil) != kept {
			t.Fatalf("%s kept=%v", name, err == nil)
		}
	}
}

func TestEvictedDownloadFileIsRemoved(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	host := asStr(e.config.Must("downloadHostPath"))
	os.WriteFile(filepath.Join(host, guidA), []byte("first"), 0o600)
	loop(e, func() {
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidA, "suggestedFilename", "a"))
		e.files.event("Browser.downloadProgress", O("guid", guidA, "state", "completed", "receivedBytes", 5))
		for i := 0; i < 20; i++ {
			guid := "3d4e5f6a-7b8c-4d9e-8f0a-" + "0000000000" + string(rune('a'+i/10)) + string(rune('0'+i%10))
			e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guid, "suggestedFilename", "n"))
			e.files.event("Browser.downloadProgress", O("guid", guid, "state", "completed", "receivedBytes", 1))
		}
		if page.downloads.Has(1) {
			t.Fatal("21st download kept the first")
		}
	})
	if _, err := os.Lstat(filepath.Join(host, guidA)); err == nil {
		t.Fatal("evicted download's file kept")
	}
}

func TestDownloadSaveDoesNotFollowLinks(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	host := asStr(e.config.Must("downloadHostPath"))
	secret := filepath.Join(t.TempDir(), "secret")
	os.WriteFile(secret, []byte("secret"), 0o600)
	if err := os.Symlink(secret, filepath.Join(host, guidA)); err != nil {
		t.Skip("symlinks unavailable:", err)
	}
	out := filepath.Join(t.TempDir(), "saved")
	loop(e, func() {
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidA, "suggestedFilename", "a"))
		e.files.event("Browser.downloadProgress", O("guid", guidA, "state", "completed", "receivedBytes", 6))
		if code := saveError(e, page, 1, out); code != "download_file_unavailable: configure shared download paths for a remote browser" {
			t.Fatalf("linked download: %q", code)
		}
	})
	if _, err := os.Stat(out); err == nil {
		t.Fatal("link target copied")
	}
}

func TestDownloadSaveRefusesHardLinks(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	host := asStr(e.config.Must("downloadHostPath"))
	secret := filepath.Join(host, "secret")
	os.WriteFile(secret, []byte("secret"), 0o600)
	out := filepath.Join(t.TempDir(), "saved")
	loop(e, func() {
		// Linked before the browser finishes, and after.
		if err := os.Link(secret, filepath.Join(host, guidA)); err != nil {
			t.Skip("hard links unavailable:", err)
		}
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidA, "suggestedFilename", "a"))
		e.files.event("Browser.downloadProgress", O("guid", guidA, "state", "completed", "receivedBytes", 6))
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidB, "suggestedFilename", "b"))
		e.files.event("Browser.downloadProgress", O("guid", guidB, "state", "completed", "receivedBytes", 6))
		os.Link(secret, filepath.Join(host, guidB))
		for id := 1; id <= 2; id++ {
			if code := saveError(e, page, id, out); code != "download_file_unavailable: configure shared download paths for a remote browser" {
				t.Fatalf("hard-linked download %d: %q", id, code)
			}
		}
	})
	if _, err := os.Stat(out); err == nil {
		t.Fatal("link target copied")
	}
}

func TestDownloadSaveReadsOnlyTheFileTheBrowserWrote(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	host := asStr(e.config.Must("downloadHostPath"))
	out := filepath.Join(t.TempDir(), "saved")
	loop(e, func() {
		os.WriteFile(filepath.Join(host, guidA), []byte("wrote"), 0o600)
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidA, "suggestedFilename", "a"))
		e.files.event("Browser.downloadProgress", O("guid", guidA, "state", "completed", "receivedBytes", 5))
		// Replaced by another file of the same size after the browser finished.
		// (Moved aside, not removed, so its inode number is not reused.)
		os.Rename(filepath.Join(host, guidA), filepath.Join(host, "kept-a"))
		os.WriteFile(filepath.Join(host, guidA), []byte("other"), 0o600)
		if code := saveError(e, page, 1, out); code != "download_file_unavailable: configure shared download paths for a remote browser" {
			t.Fatalf("replaced download: %q", code)
		}
		// Not the size the browser reported.
		os.WriteFile(filepath.Join(host, guidB), []byte("longer than reported"), 0o600)
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidB, "suggestedFilename", "b"))
		e.files.event("Browser.downloadProgress", O("guid", guidB, "state", "completed", "receivedBytes", 5))
		if code := saveError(e, page, 2, out); code != "download_file_unavailable: configure shared download paths for a remote browser" {
			t.Fatalf("resized download: %q", code)
		}
		// The file first seen at save time (a remote browser's shared
		// directory) is the one later saves must find.
		e.files.event("Browser.downloadWillBegin", O("frameId", page.mainID, "guid", guidC, "suggestedFilename", "c"))
		e.files.event("Browser.downloadProgress", O("guid", guidC, "state", "completed", "receivedBytes", 5))
		os.WriteFile(filepath.Join(host, guidC), []byte("first"), 0o600)
		if code := saveError(e, page, 3, out); code != "" {
			t.Fatalf("first save: %q", code)
		}
		os.Rename(filepath.Join(host, guidC), filepath.Join(host, "kept-c"))
		os.WriteFile(filepath.Join(host, guidC), []byte("other"), 0o600)
		if code := saveError(e, page, 3, out); code != "download_file_unavailable: configure shared download paths for a remote browser" {
			t.Fatalf("replaced after first save: %q", code)
		}
	})
	if data, _ := os.ReadFile(out); string(data) != "first" {
		t.Fatalf("saved %q", data)
	}
}
