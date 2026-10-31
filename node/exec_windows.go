//go:build windows

package node

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unicode/utf16"

	"golang.org/x/sys/windows"
)

// powershell is Windows PowerShell 5.1, present on every supported release.
func powershell() string {
	return filepath.Join(os.Getenv("SystemRoot"), `System32\WindowsPowerShell\v1.0\powershell.exe`)
}

// The prologue makes PowerShell and the console programs it starts write
// UTF-8 and keeps progress records out of redirected output. The epilogue
// exits like a shell would: 0 when the last command succeeded, else the
// native exit code, or 1 for a failed cmdlet.
//
// Write-Error labels its message with the caller's $MyInvocation, which
// for a -EncodedCommand script is the whole script: the prologue, the
// command and the epilogue would lead every error, folded at the console
// width. Without it the error names Write-Error and the line of the
// command it came from, as other cmdlets' errors do. The prologue is one
// line so the command's line numbers are off by only one.
const (
	psPrologue = "$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.Encoding]::UTF8;$OutputEncoding=[Text.Encoding]::UTF8;try{$MyInvocation=$null}catch{}\n"
	psEpilogue = "\nif ($?) { exit 0 } elseif ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 }\n"
	// A command line holds 32767 UTF-16 units; longer scripts go through a file.
	encodedLimit = 30000
)

func encodeScript(script string) string {
	units := utf16.Encode([]rune(script))
	b := make([]byte, len(units)*2)
	for i, u := range units {
		b[i*2], b[i*2+1] = byte(u), byte(u>>8)
	}
	return base64.StdEncoding.EncodeToString(b)
}

// powershellCommand runs script non-interactively; the returned cleanup
// removes the script file when one was needed.
func powershellCommand(script string) (*exec.Cmd, func(), error) {
	args := []string{"-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-OutputFormat", "Text"}
	cleanup := func() {}
	if encoded := encodeScript(script); len(encoded) <= encodedLimit {
		args = append(args, "-EncodedCommand", encoded)
	} else {
		f, err := os.CreateTemp("", "openbear-exec-*.ps1")
		if err != nil {
			return nil, nil, err
		}
		// Windows PowerShell reads a script without a BOM as the ANSI code page.
		_, err = f.WriteString("\ufeff" + script)
		if closeErr := f.Close(); err == nil {
			err = closeErr
		}
		if err != nil {
			os.Remove(f.Name())
			return nil, nil, err
		}
		args = append(args, "-File", f.Name())
		cleanup = func() { os.Remove(f.Name()) }
	}
	cmd := exec.Command(powershell(), args...)
	// A hidden console lets [Console]::OutputEncoding reach native programs.
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: windows.CREATE_NO_WINDOW}
	return cmd, cleanup, nil
}

// killJob holds a process and everything it starts so a timeout or a lost
// controller connection ends them all, like a process group on Unix.
func killJob(pid int) (windows.Handle, error) {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return 0, err
	}
	process, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err == nil {
		err = windows.AssignProcessToJobObject(job, process)
		windows.CloseHandle(process)
	}
	if err != nil {
		windows.CloseHandle(job)
		return 0, err
	}
	return job, nil
}

// Exec runs one PowerShell command in its own job object.
func Exec(ctx context.Context, raw json.RawMessage) (ExecResult, error) {
	var p ExecParams
	if err := json.Unmarshal(raw, &p); err != nil || strings.TrimSpace(p.Command) == "" {
		return ExecResult{}, errors.New("invalid_params")
	}
	timeout := p.Timeout
	if timeout <= 0 {
		timeout = 120
	}
	timeout = min(timeout, 3600)
	ctx, cancel := context.WithTimeout(ctx, time.Duration(timeout)*time.Second)
	defer cancel()
	cmd, cleanup, err := powershellCommand(psPrologue + p.Command + psEpilogue)
	if err != nil {
		return ExecResult{}, errors.New("start_failed")
	}
	defer cleanup()
	cmd.Dir = p.Cwd
	cmd.Stdin = strings.NewReader(p.Stdin)
	var stdout, stderr capped
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	// Done when PowerShell is: what it left running in the background (start /b, Start-Process
	// sharing the console) may hold the output pipes for as long as it lives, and keeps running
	// once the job's handle closes (the job does not kill on close).
	cmd.WaitDelay = execGrace
	start := time.Now()
	if err := cmd.Start(); err != nil {
		return ExecResult{}, errors.New("start_failed")
	}
	job, jobErr := killJob(cmd.Process.Pid)
	if jobErr == nil {
		defer windows.CloseHandle(job)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	timedOut := false
	select {
	case err = <-done:
	case <-ctx.Done():
		if jobErr == nil {
			windows.TerminateJobObject(job, 1)
		} else {
			cmd.Process.Kill()
		}
		err = <-done
		timedOut = errors.Is(ctx.Err(), context.DeadlineExceeded)
	}
	result := ExecResult{Stdout: stdout.String(), Stderr: plainErrors(stderr.String()), Truncated: stdout.truncated() || stderr.truncated(),
		TimedOut: timedOut, DurationMs: time.Since(start).Milliseconds(), Shell: "powershell"}
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		result.ExitCode = exitErr.ExitCode()
	} else if err != nil && !errors.Is(err, exec.ErrWaitDelay) {
		result.ExitCode = -1
	}
	return result, nil
}
