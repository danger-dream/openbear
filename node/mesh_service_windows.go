//go:build windows

package node

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"golang.org/x/sys/windows"
	"golang.org/x/sys/windows/svc"
	"golang.org/x/sys/windows/svc/mgr"
)

const MeshServiceName = "openbear-mesh"

type windowsService struct{}

func newMeshService(string) meshService { return windowsService{} }

func openMeshService() (*mgr.Mgr, *mgr.Service, error) {
	m, err := mgr.Connect()
	if err != nil {
		return nil, nil, err
	}
	s, err := m.OpenService(MeshServiceName)
	if err != nil {
		m.Disconnect()
		return nil, nil, err
	}
	return m, s, nil
}

// install creates the service set to start on demand until the first config
// arrives; start switches it to automatic. Any exit with an error restarts it.
func (windowsService) install(bin, dir string) error {
	m, err := mgr.Connect()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	s, err := m.OpenService(MeshServiceName)
	if err != nil {
		start := uint32(mgr.StartManual)
		if _, statErr := os.Stat(filepath.Join(dir, "config.json")); statErr == nil {
			start = mgr.StartAutomatic
		}
		s, err = m.CreateService(MeshServiceName, bin, mgr.Config{StartType: start, DisplayName: "OpenBear Mesh",
			Description: "OpenBear mesh data plane, managed by openbear-node."}, "mesh", "run", "--dir", dir)
		if err != nil {
			return err
		}
	} else {
		line := syscall.EscapeArg(bin) + " mesh run --dir " + syscall.EscapeArg(dir)
		c, err := s.Config()
		if err == nil && c.BinaryPathName != line {
			c.BinaryPathName = line
			err = s.UpdateConfig(c)
		}
		if err != nil {
			s.Close()
			return err
		}
	}
	defer s.Close()
	restart := mgr.RecoveryAction{Type: mgr.ServiceRestart, Delay: 5 * time.Second}
	if err = s.SetRecoveryActions([]mgr.RecoveryAction{restart, restart, restart}, 86400); err != nil {
		return err
	}
	return s.SetRecoveryActionsOnNonCrashFailures(true)
}

func (windowsService) state() string {
	m, s, err := openMeshService()
	if err != nil {
		return "absent"
	}
	defer m.Disconnect()
	defer s.Close()
	status, err := s.Query()
	if err == nil && (status.State == svc.Running || status.State == svc.StartPending || status.State == svc.ContinuePending) {
		return "running"
	}
	return "stopped"
}

func (windowsService) start() error {
	m, s, err := openMeshService()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	defer s.Close()
	if c, err := s.Config(); err == nil && c.StartType != mgr.StartAutomatic {
		c.StartType = mgr.StartAutomatic
		if err = s.UpdateConfig(c); err != nil {
			return err
		}
	}
	if err = s.Start(); err != nil && !errors.Is(err, windows.ERROR_SERVICE_ALREADY_RUNNING) {
		return err
	}
	return nil
}

func (windowsService) stop() error {
	m, s, err := openMeshService()
	if err != nil {
		return err
	}
	defer m.Disconnect()
	defer s.Close()
	stopService(s)
	if status, err := s.Query(); err != nil || status.State != svc.Stopped {
		return errors.New("service did not stop")
	}
	return nil
}

// remove marks the service for deletion; it disappears once stopped.
func (windowsService) remove() error {
	m, s, err := openMeshService()
	if err != nil {
		if errors.Is(err, windows.ERROR_SERVICE_DOES_NOT_EXIST) {
			return nil
		}
		return err
	}
	defer m.Disconnect()
	defer s.Close()
	stopService(s)
	if status, err := s.Query(); err != nil || status.State != svc.Stopped {
		return errors.New("service did not stop")
	}
	return s.Delete()
}

// meshInstallsWintun reports whether the data plane will be the one to
// install the wintun driver: no driver service exists yet.
func meshInstallsWintun() bool {
	m, err := mgr.Connect()
	if err != nil {
		return false
	}
	defer m.Disconnect()
	s, err := m.OpenService("wintun")
	if err != nil {
		return errors.Is(err, windows.ERROR_SERVICE_DOES_NOT_EXIST)
	}
	s.Close()
	return false
}

// removeWintun deletes the driver through the wintun.dll the data plane
// released; wintun refuses while any adapter still uses it.
func removeWintun(dir string) {
	dll, err := windows.LoadDLL(filepath.Join(dir, "bin", "wintun.dll"))
	if err != nil {
		return
	}
	if proc, err := dll.FindProc("WintunDeleteDriver"); err == nil {
		proc.Call()
	}
	dll.Release()
}
