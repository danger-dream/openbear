//go:build linux

package node

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
)

const (
	DefaultConfigPath = "/etc/openbear-node/config.json"
	InstalledBinary   = "/usr/local/bin/openbear-node"
	unitPath          = "/etc/systemd/system/openbear-node.service"
	// initScript and keepList are what install.sh puts on OpenWrt: the procd
	// service, and the files a firmware upgrade keeps.
	initScript = "/etc/init.d/openbear-node"
	keepList   = "/lib/upgrade/keep.d/openbear-node"
)

// procSelfCgroup is read by inService; tests point it elsewhere.
var procSelfCgroup = "/proc/self/cgroup"

// procdPids are the pids of the openbear-node service's instances as procd lists them; tests
// set it.
var procdPids = func() []int {
	out, err := serviceCommand("ubus", "call", "service", "list", `{"name":"openbear-node"}`)
	if err != nil {
		return nil
	}
	var list map[string]struct {
		Instances map[string]struct {
			Pid int `json:"pid"`
		} `json:"instances"`
	}
	json.Unmarshal([]byte(out), &list)
	var pids []int
	for _, instance := range list["openbear-node"].Instances {
		pids = append(pids, instance.Pid)
	}
	return pids
}

// inService reports whether this process runs as the openbear-node service
// (systemd's openbear-node.service, procd's services/openbear-node), so a
// test or manual run never touches the real service on the same machine. A
// procd without cgroups is asked which pid its instance has.
func inService() bool {
	b, _ := os.ReadFile(procSelfCgroup)
	if strings.Contains(string(b), "/openbear-node.service") || strings.Contains(string(b), "/services/openbear-node/") {
		return true
	}
	return procdInit() && slices.Contains(procdPids(), os.Getpid())
}

func systemctl(args ...string) { exec.Command("systemctl", args...).Run() }

// Uninstall removes what install.sh created: the config, the service (a
// systemd unit, or on OpenWrt a procd init script and the firmware upgrade's
// keep list) and the installed binary. From inside the service, the service
// manager is asked to stop it last so it does not restart it. A
// controller-requested uninstall outside the service only removes the config
// it ran with.
func Uninstall(configPath string, fromCLI bool) error {
	uninstallMesh(configPath)
	if bypassInstalled() || bypassLeft() {
		removeBypass()
	}
	service := inService()
	procd := procdInit()
	installed := unitPath
	if procd {
		installed = initScript
	}
	_, missing := os.Stat(installed)
	managed := service || fromCLI && missing == nil
	if managed {
		if procd {
			exec.Command(initScript, "disable").Run()
			if !service {
				exec.Command(initScript, "stop").Run()
			}
		} else {
			systemctl("disable", "openbear-node")
			if !service {
				systemctl("stop", "openbear-node")
			}
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
		if procd {
			os.Remove(initScript)
			os.Remove(keepList)
		} else {
			os.Remove(unitPath)
			systemctl("daemon-reload")
		}
	}
	if exe, err := os.Executable(); err == nil && exe == InstalledBinary {
		os.Remove(exe)
	}
	if service {
		if procd {
			// The init script is gone: procd drops the service and ends this process without respawning it.
			exec.Command("ubus", "call", "service", "delete", `{"name":"openbear-node"}`).Run()
		} else {
			systemctl("stop", "--no-block", "openbear-node")
		}
	}
	return nil
}
