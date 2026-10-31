//go:build windows

package node

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

const ServiceName = "openbear-node"

var (
	DefaultConfigPath = filepath.Join(knownDir("ProgramData", `C:\ProgramData`), `OpenBear\node\config.json`)
	InstalledBinary   = filepath.Join(knownDir("ProgramFiles", `C:\Program Files`), `OpenBear\openbear-node.exe`)
)

func knownDir(env, fallback string) string {
	if v := os.Getenv(env); v != "" {
		return v
	}
	return fallback
}

func inService() bool {
	ok, _ := svc.IsWindowsService()
	return ok
}

// Uninstall removes what install.ps1 created: the config, the service and the
// installed binary. A service marked for deletion disappears once it stops,
// and a running executable cannot be deleted, so a detached cmd removes it
// after this process exits.
func Uninstall(configPath string, fromCLI bool) error {
	uninstallMesh(configPath)
	service := inService()
	if m, err := mgr.Connect(); err == nil {
		if s, err := m.OpenService(ServiceName); err == nil {
			if service || fromCLI {
				if !service {
					stopService(s)
				}
				s.Delete()
			}
			s.Close()
		}
		m.Disconnect()
	}
	err := os.Remove(configPath)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	os.RemoveAll(browserDataDir(configPath))
	os.RemoveAll(jobsDir(configPath))
	os.Remove(filepath.Dir(configPath)) // only when empty
	if exe, err := os.Executable(); err == nil && exe == InstalledBinary {
		RemoveReplaced(exe)
		dir := filepath.Dir(configPath)
		dirs := []string{filepath.Dir(exe), dir}
		if configPath == DefaultConfigPath {
			dirs = append(dirs, filepath.Dir(dir))
		}
		removeAfterExit([]string{exe, filepath.Join(dir, "node.log")}, dirs)
	}
	return nil
}

// removeAfterExit deletes files this process still holds open, then the
// directories if they are empty, from a detached cmd that outlives it.
func removeAfterExit(files, dirs []string) {
	line := `cmd.exe /d /c "ping -n 6 127.0.0.1 >nul`
	for _, f := range files {
		line += ` & del /f /q "` + f + `"`
	}
	for _, d := range dirs {
		line += ` & rmdir "` + d + `"`
	}
	cmd := exec.Command(filepath.Join(os.Getenv("SystemRoot"), `System32\cmd.exe`))
	cmd.SysProcAttr = &syscall.SysProcAttr{CmdLine: line + `"`, CreationFlags: windows.CREATE_NO_WINDOW | windows.CREATE_NEW_PROCESS_GROUP}
	if cmd.Start() == nil {
		cmd.Process.Release()
	}
}

func stopService(s *mgr.Service) {
	status, err := s.Control(svc.Stop)
	for deadline := time.Now().Add(20 * time.Second); err == nil && status.State != svc.Stopped && time.Now().Before(deadline); {
		time.Sleep(300 * time.Millisecond)
		status, err = s.Query()
	}
}
