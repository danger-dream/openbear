//go:build linux

package node

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const (
	DefaultConfigPath = "/etc/openbear-node/config.json"
	InstalledBinary   = "/usr/local/bin/openbear-node"
	unitPath          = "/etc/systemd/system/openbear-node.service"
)

// inService reports whether this process runs as openbear-node.service, so a
// test or manual run never touches the real unit on the same machine.
func inService() bool {
	b, _ := os.ReadFile("/proc/self/cgroup")
	return strings.Contains(string(b), "/openbear-node.service")
}

func systemctl(args ...string) { exec.Command("systemctl", args...).Run() }

// Uninstall removes what install.sh created: the config, the systemd unit and
// the installed binary. From inside the service, systemd is asked to stop it
// last so Restart=always does not bring it back. A controller-requested
// uninstall outside the service only removes the config it ran with.
func Uninstall(configPath string, fromCLI bool) error {
	uninstallMesh(configPath)
	service := inService()
	_, unitErr := os.Stat(unitPath)
	managed := service || fromCLI && unitErr == nil
	if managed {
		systemctl("disable", "openbear-node")
		if !service {
			systemctl("stop", "openbear-node")
		}
	}
	err := os.Remove(configPath)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	os.RemoveAll(browserDataDir(configPath))
	os.RemoveAll(jobsDir(configPath))
	exec.Command("userdel", browserAccount).Run()
	os.Remove(filepath.Dir(browserDataDir(configPath))) // only when empty
	os.Remove(filepath.Dir(configPath))                 // only when empty
	if managed {
		os.Remove(unitPath)
		systemctl("daemon-reload")
	}
	if exe, err := os.Executable(); err == nil && exe == InstalledBinary {
		os.Remove(exe)
	}
	if service {
		systemctl("stop", "--no-block", "openbear-node")
	}
	return nil
}
