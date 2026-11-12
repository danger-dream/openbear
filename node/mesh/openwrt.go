package mesh

import (
	"os"
	"path/filepath"
)

// OpenWrtRelease marks OpenWrt and its derivatives (Kwrt, ImmortalWrt); tests point it elsewhere.
var OpenWrtRelease = "/etc/openwrt_release"

// OpenWrt: init is procd, /var is a RAM disk, and the flash below / wears with every write.
func OpenWrt() bool {
	_, err := os.Stat(OpenWrtRelease)
	return err == nil
}

// runDir holds what the data plane rewrites every second on OpenWrt, off the flash.
var runDir = "/var/run/openbear-mesh"

// StatusPath is where the data plane in dir writes status.json, once a second: dir itself, but
// a RAM disk on OpenWrt, whose dir is on flash.
func StatusPath(dir string) string {
	if OpenWrt() {
		return filepath.Join(runDir, "status.json")
	}
	return filepath.Join(dir, "status.json")
}
