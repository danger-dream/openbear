package mesh

import (
	"os"
	"path/filepath"
	"testing"
)

func TestStatusGoesToRAMOnOpenWrt(t *testing.T) {
	dir := t.TempDir()
	keepRelease, keepRun := OpenWrtRelease, runDir
	defer func() { OpenWrtRelease, runDir = keepRelease, keepRun }()
	OpenWrtRelease, runDir = filepath.Join(dir, "openwrt_release"), filepath.Join(dir, "run")
	if OpenWrt() || StatusPath(dir) != filepath.Join(dir, "status.json") {
		t.Fatalf("elsewhere: %v %s", OpenWrt(), StatusPath(dir))
	}
	os.WriteFile(OpenWrtRelease, []byte("DISTRIB_ID='OpenWrt'\n"), 0o644)
	if !OpenWrt() || StatusPath(dir) != filepath.Join(runDir, "status.json") {
		t.Fatalf("OpenWrt: %v %s", OpenWrt(), StatusPath(dir))
	}
	r := &runner{dir: dir, events: &eventLog{}}
	r.write(Status{ConfigRevision: 3})
	if _, err := os.Stat(filepath.Join(runDir, "status.json")); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "status.json")); err == nil {
		t.Fatal("status.json written to the flash directory")
	}
}
