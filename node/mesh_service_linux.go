//go:build linux

package node

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
)

const (
	meshUnitName = "openbear-mesh"
	meshUnitPath = "/etc/systemd/system/openbear-mesh.service"
)

type systemdService struct {
	unit      string
	systemctl func(args ...string) (string, error)
}

func newMeshService(string) meshService {
	return &systemdService{unit: meshUnitPath, systemctl: func(args ...string) (string, error) {
		return serviceCommand("systemctl", args...)
	}}
}

// meshUnit starts at boot only once there is a config to run.
func meshUnit(bin, dir string) string {
	return "[Unit]\nDescription=OpenBear mesh data plane\nAfter=network-online.target\nWants=network-online.target\n" +
		"ConditionPathExists=" + strings.ReplaceAll(filepath.Join(dir, "config.json"), "%", "%%") + "\nStartLimitIntervalSec=0\n\n" +
		"[Service]\nExecStart=" + systemdQuote(bin) + " mesh run --dir " + systemdQuote(dir) + "\n" +
		"Restart=always\nRestartSec=5\nTimeoutStopSec=20\n\n[Install]\nWantedBy=multi-user.target\n"
}

func systemdQuote(s string) string {
	s = strings.NewReplacer("%", "%%", "$", "$$").Replace(s)
	if !strings.ContainsAny(s, " \t\"'\\") {
		return s
	}
	return `"` + strings.NewReplacer(`\`, `\\`, `"`, `\"`).Replace(s) + `"`
}

// install enables the unit without starting it; a changed unit is reloaded
// but a running data plane keeps going until its next restart.
func (s *systemdService) install(bin, dir string) error {
	unit := meshUnit(bin, dir)
	if old, err := os.ReadFile(s.unit); err != nil || string(old) != unit {
		if err = replaceFile(s.unit, strings.NewReader(unit), 0o644); err != nil {
			return err
		}
		if _, err = s.systemctl("daemon-reload"); err != nil {
			return err
		}
	}
	_, err := s.systemctl("enable", meshUnitName)
	return err
}

func (s *systemdService) state() string {
	if _, err := os.Stat(s.unit); err != nil {
		return "absent"
	}
	switch out, _ := s.systemctl("is-active", meshUnitName); out {
	case "active", "activating", "reloading":
		return "running"
	}
	return "stopped"
}

func (s *systemdService) start() error {
	_, err := s.systemctl("start", meshUnitName)
	return err
}

func (s *systemdService) stop() error {
	_, err := s.systemctl("stop", meshUnitName)
	return err
}

func (s *systemdService) remove() error {
	s.systemctl("stop", meshUnitName)
	s.systemctl("disable", meshUnitName)
	if err := os.Remove(s.unit); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	s.systemctl("daemon-reload")
	s.systemctl("reset-failed", meshUnitName)
	return nil
}

func meshInstallsWintun() bool { return false }

func removeWintun(string) {}
