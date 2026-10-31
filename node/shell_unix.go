//go:build unix

package node

import (
	"os"
	"os/exec"
	"os/user"
	"runtime"
	"strings"
)

// bashPath is the login shell commands use when the machine has it; tests
// point it elsewhere.
var bashPath = "/bin/bash"

// execShell is bash when present, else /bin/sh. As a login shell bash reads
// /etc/profile and ~/.profile, so PATH matches what an SSH login sees rather
// than the service's bare one.
func execShell() string {
	if info, err := os.Stat(bashPath); err == nil && info.Mode().IsRegular() && info.Mode()&0o111 != 0 {
		return bashPath
	}
	return "/bin/sh"
}

// shellCommand runs command through shell: bash as a login shell, anything
// else as plain sh -c.
func shellCommand(shell, command string) *exec.Cmd {
	if runtime.GOOS == "darwin" {
		command = nohupFallback + command
	}
	var cmd *exec.Cmd
	if strings.HasSuffix(shell, "bash") {
		cmd = exec.Command(shell, "-l", "-c", command)
	} else {
		cmd = exec.Command(shell, "-c", command)
	}
	cmd.Env = shellEnv(shell)
	return cmd
}

// nohupFallback stands in for nohup where macOS's refuses to run: it first
// detaches from the launchd session, which fails for a launchd daemon's
// children ("can't detach from console"), so "nohup cmd &" started nothing.
// The stand-in only ignores SIGHUP, which is all nohup does for output that
// is not a terminal. It is on the command's first line, keeping the
// command's line numbers, and exported to the bash scripts it runs.
const nohupFallback = `if ! command nohup true >/dev/null 2>&1; then nohup() ( trap '' HUP; if [ "$1" = -- ]; then shift; fi; exec "$@" ); if [ -n "$BASH_VERSION" ]; then export -f nohup; fi; fi; `

// shellEnv fills in HOME, USER, LOGNAME and SHELL, which a systemd service
// may run without; profiles and many tools expect them.
func shellEnv(shell string) []string {
	env := append(os.Environ(), "LANG="+utf8Locale())
	if os.Getenv("SHELL") == "" {
		env = append(env, "SHELL="+shell)
	}
	if os.Getenv("HOME") != "" && os.Getenv("USER") != "" && os.Getenv("LOGNAME") != "" {
		return env
	}
	u, err := user.Current()
	if err != nil {
		return env
	}
	// exec uses the last value of a repeated key.
	for key, value := range map[string]string{"HOME": u.HomeDir, "USER": u.Username, "LOGNAME": u.Username} {
		if os.Getenv(key) == "" && value != "" {
			env = append(env, key+"="+value)
		}
	}
	return env
}

// utf8Locale is a UTF-8 locale this system has: macOS has no C.UTF-8, and without a locale
// its shells and ls treat multibyte characters as unprintable.
func utf8Locale() string {
	if runtime.GOOS == "darwin" {
		return "en_US.UTF-8"
	}
	return "C.UTF-8"
}
