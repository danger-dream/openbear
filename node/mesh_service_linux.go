//go:build linux

package node

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	meshUnitName = "openbear-mesh"
	meshUnitPath = "/etc/systemd/system/openbear-mesh.service"
	// meshInitScript is the mesh service on OpenWrt, where procd starts it.
	meshInitScript = "/etc/init.d/openbear-mesh"
)

// initComm names the init system: procd on OpenWrt. Tests point it elsewhere.
var initComm = "/proc/1/comm"

func procdInit() bool {
	b, _ := os.ReadFile(initComm)
	return strings.TrimSpace(string(b)) == "procd"
}

type systemdService struct {
	unit      string
	systemctl func(args ...string) (string, error)
}

func newMeshService(string) meshService {
	if procdInit() {
		return &procdService{script: meshInitScript, name: meshUnitName, run: serviceCommand, poll: 250 * time.Millisecond}
	}
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

// procdService is the mesh service as an OpenWrt init script: like the
// systemd unit it starts at boot only once there is a config to run, and procd
// restarts it 5 seconds after it exits, without limit.
type procdService struct {
	script, name string
	run          func(name string, args ...string) (string, error)
	poll         time.Duration
}

func procdScript(bin, dir string) string {
	return "#!/bin/sh /etc/rc.common\n# OpenBear mesh data plane\nSTART=99\nSTOP=10\nUSE_PROCD=1\n\nstart_service() {\n" +
		"\t[ -f " + shellQuote(filepath.Join(dir, "config.json")) + " ] || return 0\n" +
		"\tprocd_open_instance\n" +
		"\tprocd_set_param command " + shellQuote(bin) + " mesh run --dir " + shellQuote(dir) + "\n" +
		"\tprocd_set_param respawn 3600 5 0\n\tprocd_set_param term_timeout 20\n" +
		"\tprocd_set_param stdout 1\n\tprocd_set_param stderr 1\n\tprocd_close_instance\n}\n"
}

func shellQuote(s string) string { return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'" }

func (s *procdService) install(bin, dir string) error {
	script := procdScript(bin, dir)
	if old, err := os.ReadFile(s.script); err != nil || string(old) != script {
		if err = replaceFile(s.script, strings.NewReader(script), 0o755); err != nil {
			return err
		}
	}
	_, err := s.run(s.script, "enable")
	return err
}

func (s *procdService) state() string {
	if _, err := os.Stat(s.script); err != nil {
		return "absent"
	}
	if s.running() {
		return "running"
	}
	return "stopped"
}

// running asks procd over ubus whether an instance of the service runs.
func (s *procdService) running() bool {
	out, err := s.run("ubus", "call", "service", "list", `{"name":"`+s.name+`"}`)
	if err != nil {
		return false
	}
	var list map[string]struct {
		Instances map[string]struct {
			Running bool `json:"running"`
		} `json:"instances"`
	}
	json.Unmarshal([]byte(out), &list)
	for _, instance := range list[s.name].Instances {
		if instance.Running {
			return true
		}
	}
	return false
}

func (s *procdService) start() error {
	_, err := s.run(s.script, "start")
	return err
}

// stop returns once procd no longer runs the instance; it gives the data plane
// term_timeout (20 s) to take down its interface and rules.
func (s *procdService) stop() error {
	if _, err := s.run(s.script, "stop"); err != nil {
		return err
	}
	for deadline := time.Now().Add(25 * time.Second); s.running(); {
		if time.Now().After(deadline) {
			return errors.New("still running")
		}
		time.Sleep(s.poll)
	}
	return nil
}

func (s *procdService) remove() error {
	s.stop()
	s.run(s.script, "disable")
	if err := os.Remove(s.script); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func meshInstallsWintun() bool { return false }

func removeWintun(string) {}
