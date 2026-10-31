//go:build darwin

package node

import (
	"encoding/xml"
	"errors"
	"os"
	"path/filepath"
	"strings"
)

const (
	meshLabel     = "com.openbear.mesh"
	meshPlistPath = "/Library/LaunchDaemons/com.openbear.mesh.plist"
)

type launchdService struct {
	plist     string
	launchctl func(args ...string) (string, error)
}

func newMeshService(string) meshService {
	return &launchdService{plist: meshPlistPath, launchctl: func(args ...string) (string, error) {
		return serviceCommand("launchctl", args...)
	}}
}

func xmlText(s string) string {
	var b strings.Builder
	xml.EscapeText(&b, []byte(s))
	return b.String()
}

func plistString(s string) string { return "<string>" + xmlText(s) + "</string>" }

// meshPlist keeps the job alive while config.json exists: launchd starts it
// once the first config is written, at boot, and after any exit.
func meshPlist(bin, dir string) string {
	return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>` + meshLabel + `</string>
  <key>ProgramArguments</key>
  <array>` + plistString(bin) + `<string>mesh</string><string>run</string><string>--dir</string>` + plistString(dir) + `</array>
  <key>KeepAlive</key>
  <dict><key>PathState</key><dict><key>` + xmlText(filepath.Join(dir, "config.json")) + `</key><true/></dict></dict>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardErrorPath</key>` + plistString(filepath.Join(dir, "mesh.log")) + `
</dict>
</plist>
`
}

func (s *launchdService) loaded() bool {
	_, err := s.launchctl("print", "system/"+meshLabel)
	return err == nil
}

// install loads the job; a changed plist takes effect the next time it is loaded.
func (s *launchdService) install(bin, dir string) error {
	plist := meshPlist(bin, dir)
	if old, err := os.ReadFile(s.plist); err != nil || string(old) != plist {
		if err = replaceFile(s.plist, strings.NewReader(plist), 0o644); err != nil {
			return err
		}
	}
	if s.loaded() {
		return nil
	}
	_, err := s.launchctl("bootstrap", "system", s.plist)
	return err
}

func (s *launchdService) state() string {
	if _, err := os.Stat(s.plist); err != nil {
		return "absent"
	}
	out, err := s.launchctl("print", "system/"+meshLabel)
	if err == nil && strings.Contains(out, "state = running") {
		return "running"
	}
	return "stopped"
}

func (s *launchdService) start() error {
	if !s.loaded() {
		_, err := s.launchctl("bootstrap", "system", s.plist)
		return err
	}
	_, err := s.launchctl("kickstart", "system/"+meshLabel)
	return err
}

// stop unloads the job: KeepAlive would restart a job that is only signalled.
func (s *launchdService) stop() error {
	if !s.loaded() {
		return nil
	}
	_, err := s.launchctl("bootout", "system/"+meshLabel)
	return err
}

func (s *launchdService) remove() error {
	if err := s.stop(); err != nil && s.state() == "running" {
		return err
	}
	if err := os.Remove(s.plist); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

func meshInstallsWintun() bool { return false }

func removeWintun(string) {}
