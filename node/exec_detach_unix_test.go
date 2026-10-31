//go:build unix

package node

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// pidsOf finds the processes running exactly this command line.
func pidsOf(t *testing.T, cmdline string) []int {
	t.Helper()
	out, _ := exec.Command("pgrep", "-f", "^"+cmdline+"$").Output()
	var pids []int
	for _, f := range strings.Fields(string(out)) {
		if pid, err := strconv.Atoi(f); err == nil {
			pids = append(pids, pid)
		}
	}
	return pids
}

// Background processes a command leaves behind outlive it when it ends by
// itself, even ones still holding its stdout, and the command returns once
// its shell is done instead of waiting for them until the timeout kills
// them all. A timeout still ends the whole group.
func TestExecLeavesDetachedProcessesRunning(t *testing.T) {
	marks := []string{"sleep 61.1", "sleep 61.2", "sleep 61.3"}
	defer func() {
		for _, m := range marks {
			for _, pid := range pidsOf(t, m) {
				syscall.Kill(pid, syscall.SIGKILL)
			}
		}
	}()
	// nohup with its output redirected, a subshell's background job still on
	// stdout, and one in a session of its own (perl, as macOS has no setsid).
	command := "nohup " + marks[0] + " >/dev/null 2>&1 & (" + marks[1] + " &); perl -e 'use POSIX; setsid(); exec @ARGV' " + marks[2] + " & echo started"
	if _, err := exec.LookPath("perl"); err != nil {
		marks = marks[:2]
		command = "nohup " + marks[0] + " >/dev/null 2>&1 & (" + marks[1] + " &); echo started"
	}
	raw, _ := json.Marshal(ExecParams{Command: command, Timeout: 20})
	start := time.Now()
	r, err := Exec(context.Background(), raw)
	if err != nil || r.TimedOut || r.ExitCode != 0 || strings.TrimSpace(r.Stdout) != "started" {
		t.Fatalf("exec: %+v %v", r, err)
	}
	if took := time.Since(start); took > 8*time.Second {
		t.Fatalf("waited %v for the background processes", took)
	}
	for _, m := range marks {
		if len(pidsOf(t, m)) != 1 {
			t.Fatalf("%q did not outlive the command", m)
		}
	}

	// A timeout ends what is still in the command's group.
	raw, _ = json.Marshal(ExecParams{Command: "sleep 61.4 & sleep 61.5", Timeout: 1})
	marks = append(marks, "sleep 61.4", "sleep 61.5")
	start = time.Now()
	if r, err = Exec(context.Background(), raw); err != nil || !r.TimedOut || time.Since(start) > 8*time.Second {
		t.Fatalf("timeout: %+v %v", r, err)
	}
	time.Sleep(200 * time.Millisecond)
	if len(pidsOf(t, "sleep 61.4"))+len(pidsOf(t, "sleep 61.5")) != 0 {
		t.Fatal("the timeout left the group running")
	}
}

// Where nohup itself fails, as macOS's does under launchd, the stand-in
// runs the command with SIGHUP ignored, for the shell's own nohup and for
// bash scripts it starts.
func TestNohupFallback(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "nohup"), []byte("#!/bin/sh\necho nohup: cannot detach from console >&2\nexit 127\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	script := filepath.Join(dir, "start.sh")
	os.WriteFile(script, []byte("#!/bin/bash\nnohup sh -c 'kill -HUP $$; echo script-survived'\n"), 0o755)
	path := "PATH=" + dir + ":" + os.Getenv("PATH")
	for _, shell := range []string{"/bin/sh", "/bin/bash"} {
		if _, err := os.Stat(shell); err != nil {
			continue
		}
		out, err := exec.Command(shell, "-c", path+"; "+nohupFallback+"nohup -- sh -c 'kill -HUP $$; echo survived'; echo line $LINENO").CombinedOutput()
		// dash has no $LINENO and macOS's bash 3.2 says 0 under -c; a newer bash shows the
		// command's lines unshifted.
		got := strings.TrimSpace(string(out))
		if err != nil || got != "survived\nline 1" && got != "survived\nline 0" && (shell == "/bin/bash" || got != "survived\nline") {
			t.Fatalf("%s: %q %v", shell, out, err)
		}
		if shell == "/bin/bash" {
			if out, err = exec.Command(shell, "-c", path+"; "+nohupFallback+script).CombinedOutput(); err != nil || strings.TrimSpace(string(out)) != "script-survived" {
				t.Fatalf("a bash script's nohup: %q %v", out, err)
			}
		}
	}
	// A working nohup is left alone.
	if out, err := exec.Command("/bin/sh", "-c", nohupFallback+"type nohup").CombinedOutput(); err != nil || strings.Contains(string(out), "function") {
		t.Fatalf("the system nohup works here: %q %v", out, err)
	}
}
