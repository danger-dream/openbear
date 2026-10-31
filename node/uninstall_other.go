//go:build !linux && !darwin && !windows

package node

import (
	"os"
	"path/filepath"
)

const DefaultConfigPath = "/etc/openbear-node/config.json"

// Uninstall removes the config; nothing else is installed on this platform.
func Uninstall(configPath string, _ bool) error {
	if err := os.Remove(configPath); err != nil && !os.IsNotExist(err) {
		return err
	}
	os.RemoveAll(browserDataDir(configPath))
	os.Remove(filepath.Dir(configPath))
	return nil
}
