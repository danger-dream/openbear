//go:build darwin

package node

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
)

const (
	DefaultConfigPath = "/etc/openbear-node/config.json"
	InstalledBinary   = "/usr/local/bin/openbear-node"
	launchLabel       = "com.openbear.node"
	plistPath         = "/Library/LaunchDaemons/com.openbear.node.plist"
)

// inService reports whether launchd started this process as the node's job;
// launchd names the job in XPC_SERVICE_NAME.
func inService() bool { return os.Getenv("XPC_SERVICE_NAME") == launchLabel }

// Uninstall removes what install.sh created: the config, the launchd job and
// the installed binary. From inside the job, the bootout runs in its own
// process group so launchd does not kill it along with this process.
func Uninstall(configPath string, fromCLI bool) error {
	uninstallMesh(configPath)
	service := inService()
	_, plistErr := os.Stat(plistPath)
	managed := service || fromCLI && plistErr == nil
	if managed && !service {
		exec.Command("launchctl", "bootout", "system/"+launchLabel).Run()
	}
	err := os.Remove(configPath)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	os.RemoveAll(browserDataDir(configPath))
	os.RemoveAll(jobsDir(configPath))
	os.Remove(filepath.Dir(configPath)) // only when empty
	if managed {
		os.Remove(plistPath)
	}
	if exe, err := os.Executable(); err == nil && exe == InstalledBinary {
		os.Remove(exe)
	}
	if service {
		bootout := exec.Command("launchctl", "bootout", "system/"+launchLabel)
		bootout.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		bootout.Start()
	}
	return nil
}
