//go:build windows

package node

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"golang.org/x/sys/windows"
)

func TestCollectorReportsThisWindows(t *testing.T) {
	c := NewCollector()
	c.Sample()
	time.Sleep(time.Second)
	s := c.Sample()
	info := c.Info()
	if info.Platform != "windows" || !strings.HasPrefix(info.OS, "Windows") || strings.Count(info.Kernel, ".") != 3 || info.CPU == "" || len(info.MachineID) < 16 {
		t.Fatalf("info: %+v", info)
	}
	if s.CPU < 0 || s.CPU > 100 || s.Mem.Used <= 0 || s.Mem.Used >= s.Mem.Total || s.Disk.Used <= 0 || s.Disk.Total != info.DiskTotal {
		t.Fatalf("cpu/mem/disk: %+v", s)
	}
	if s.Net.RX == 0 || s.Processes < 20 || s.Uptime <= 0 || s.Boot <= 0 || s.TCP <= 0 || s.NodeRSS <= 1<<20 {
		t.Fatalf("net/misc: %+v", s)
	}
}

func run(t *testing.T, command, stdin string, timeout int) ExecResult {
	t.Helper()
	raw, _ := json.Marshal(ExecParams{Command: command, Stdin: stdin, Timeout: timeout})
	r, err := Exec(context.Background(), raw)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func TestPowerShellExitCodesAndUTF8(t *testing.T) {
	if r := run(t, `Write-Output "中文 ok"; cmd /c echo 原生输出`, "", 30); r.ExitCode != 0 || !strings.Contains(r.Stdout, "中文 ok") || !strings.Contains(r.Stdout, "原生输出") {
		t.Fatalf("utf-8 output: %+v", r)
	}
	if r := run(t, "cmd /c exit 3", "", 30); r.ExitCode != 3 {
		t.Fatalf("native exit code: %+v", r)
	}
	if r := run(t, `Get-Item C:\no-such-dir-xyz`, "", 30); r.ExitCode != 1 || !strings.Contains(r.Stderr, `C:\no-such-dir-xyz`) || strings.ContainsAny(r.Stderr, "<>") || !strings.Contains(r.Stderr, "\r\n") {
		t.Fatalf("failed cmdlet: %+v", r)
	}
	if r := run(t, "cmd /c exit 2\nGet-Date | Out-Null", "", 30); r.ExitCode != 0 {
		t.Fatalf("the last command decides: %+v", r)
	}
	if r := run(t, "exit 7", "", 30); r.ExitCode != 7 {
		t.Fatalf("explicit exit: %+v", r)
	}
	if r := run(t, `$input | ForEach-Object { "got:" + $_ }`, "abc\n", 30); !strings.Contains(r.Stdout, "got:abc") {
		t.Fatalf("stdin: %+v", r)
	}
	long := "# " + strings.Repeat("x", 40000) + "\nWrite-Output long-ok"
	if r := run(t, long, "", 30); r.ExitCode != 0 || !strings.Contains(r.Stdout, "long-ok") {
		t.Fatalf("long script through a file: %+v", r)
	}
}

// stderr carries the error and the user's line, never the wrapper around
// the command, both inline and through a script file.
func TestPowerShellErrorsLeaveOutTheWrapper(t *testing.T) {
	long := "# " + strings.Repeat("x", 40000) + "\n"
	for _, prefix := range []string{"", long} {
		r := run(t, prefix+"Write-Output '中文'; Write-Error '错误输出'; exit 3", "", 30)
		if r.ExitCode != 3 || !strings.Contains(r.Stdout, "中文") || !strings.Contains(r.Stderr, "错误输出") {
			t.Fatalf("write-error: %+v", r)
		}
		for _, wrapper := range []string{"ProgressPreference", "OutputEncoding", "LASTEXITCODE", "MyInvocation"} {
			if strings.Contains(r.Stderr, wrapper) {
				t.Fatalf("stderr shows the wrapper (%s): %q", wrapper, r.Stderr)
			}
		}
		r = run(t, prefix+"function f { Write-Error 'in-function' }\nf", "", 30)
		if !strings.Contains(r.Stderr, "f : in-function") || strings.Contains(r.Stderr, "LASTEXITCODE") {
			t.Fatalf("function error: %+v", r)
		}
	}
	if r := run(t, "1..300000", "", 60); !r.Truncated || !strings.HasPrefix(r.Stdout, "1\r\n2\r\n") || !strings.HasSuffix(strings.TrimRight(r.Stdout, "\r\n"), "299999\r\n300000") ||
		!omission.MatchString(r.Stdout) {
		t.Fatalf("long output: truncated %v, %d bytes", r.Truncated, len(r.Stdout))
	}
}

func alive(pid int) bool {
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION|windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(h)
	event, _ := windows.WaitForSingleObject(h, 0)
	return event == uint32(windows.WAIT_TIMEOUT)
}

func TestExecTimeoutEndsTheWholeTree(t *testing.T) {
	start := time.Now()
	r := run(t, "$p = Start-Process ping.exe -ArgumentList '-n','60','127.0.0.1' -PassThru -WindowStyle Hidden\nWrite-Output \"child:$($p.Id)\"\nping -n 60 127.0.0.1 | Out-Null", "", 3)
	if !r.TimedOut || time.Since(start) > 15*time.Second {
		t.Fatalf("timeout not enforced: %+v", r)
	}
	m := regexp.MustCompile(`child:(\d+)`).FindStringSubmatch(r.Stdout)
	if m == nil {
		t.Fatalf("child pid not reported: %+v", r)
	}
	pid, _ := strconv.Atoi(m[1])
	for deadline := time.Now().Add(5 * time.Second); alive(pid); time.Sleep(100 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatalf("child %d survived the timeout", pid)
		}
	}
}

// From the 10-30 check: a program left running with the command's output kept exec waiting
// until the timeout, which then ended the program too.
func TestExecReturnsWhenABackgroundProgramKeepsItsOutput(t *testing.T) {
	start := time.Now()
	r := run(t, "cmd /c \"start /b ping -n 30 127.0.0.1 & echo started\"\nWrite-Output 'done'", "", 60)
	if r.TimedOut || time.Since(start) > 15*time.Second || !strings.Contains(r.Stdout, "done") {
		t.Fatalf("exec waited on the background program: %+v after %v", r, time.Since(start))
	}
	out, _ := exec.Command("tasklist", "/fi", "imagename eq PING.EXE").Output()
	if !strings.Contains(strings.ToUpper(string(out)), "PING.EXE") {
		t.Fatal("the background program was ended with the command")
	}
	exec.Command("taskkill", "/im", "ping.exe", "/f").Run()
}

// From the 10-30 check: Go reports a junction as irregular, so the file manager listed it as
// "other" and could not enter it.
func TestAJunctionIsListedAsALinkToADirectory(t *testing.T) {
	base := t.TempDir()
	target := filepath.Join(base, "real")
	os.MkdirAll(target, 0o700)
	os.WriteFile(filepath.Join(target, "a.txt"), []byte("x"), 0o600)
	link := filepath.Join(base, "moved")
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", link, target).CombinedOutput(); err != nil {
		t.Fatal(err, string(out))
	}
	info, err := os.Lstat(link)
	if err != nil {
		t.Fatal(err)
	}
	if e := describe(link, "moved", info); e.Type != "link" || !e.Dir {
		t.Fatalf("junction listed as %+v", e)
	}
	if reparseFile(info) {
		t.Fatal("a junction is no file")
	}
}

func TestConPTYRunsResizesAndCloses(t *testing.T) {
	terminalSession(t, Capabilities{Terminal: true}, func(ch *channel) {
		control := map[string]map[string]any{}
		open(t, ch, 7)
		termInput(ch, 7, "echo \"ready-$(40+2) 你好\"\r")
		output(t, ch, 7, "ready-42 你好", nil)
		ch.send(map[string]any{"t": "term.resize", "sid": 7, "cols": 132, "rows": 40})
		time.Sleep(300 * time.Millisecond)
		termInput(ch, 7, "echo cols-$($Host.UI.RawUI.WindowSize.Width)\r")
		output(t, ch, 7, "cols-132", nil)
		ch.send(map[string]any{"t": "term.close", "sid": 7})
		output(t, ch, 7, "term.closed", control)
		if control["term.closed"]["reason"] != "closed" {
			t.Fatalf("closed: %v", control["term.closed"])
		}
		// A shell that exits by itself ends its terminal too.
		open(t, ch, 8)
		termInput(ch, 8, "exit\r")
		output(t, ch, 8, "term.closed", control)
		if control["term.closed"]["sid"].(float64) != 8 || control["term.closed"]["reason"] != "exited" {
			t.Fatalf("exited: %v", control["term.closed"])
		}
	})
}

func TestFilesUseDrivesAndForwardSlashes(t *testing.T) {
	u := newUploads()
	root, err := fileCall(t, u, "fs.list", map[string]any{"path": "/"})
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(mustJSON(root["entries"]), `"name":"C:"`) {
		t.Fatalf("drives: %v", root)
	}
	dir := filepath.ToSlash(t.TempDir())
	enc := base64.StdEncoding.EncodeToString
	if _, err = fileCall(t, u, "fs.write", map[string]any{"path": dir + "/sub/a.txt", "data": enc([]byte("hi")), "mkdirs": true}); err != nil {
		t.Fatal(err)
	}
	list, err := fileCall(t, u, "fs.list", map[string]any{"path": dir + "/sub"})
	if err != nil || list["path"] != dir+"/sub" || !strings.Contains(mustJSON(list["entries"]), `"name":"a.txt"`) {
		t.Fatalf("list: %v %v", list, err)
	}
	read, err := fileCall(t, u, "fs.read", map[string]any{"path": dir + "/sub/a.txt"})
	if err != nil || read["data"] != enc([]byte("hi")) {
		t.Fatalf("read: %v %v", read, err)
	}
	if _, err = fileCall(t, u, "fs.list", map[string]any{"path": "sub"}); err == nil {
		t.Fatal("a relative path must be refused")
	}
}

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func copyFile(t *testing.T, from, to string) {
	t.Helper()
	in, err := os.Open(from)
	if err != nil {
		t.Fatal(err)
	}
	defer in.Close()
	out, err := os.Create(to)
	if err != nil {
		t.Fatal(err)
	}
	io.Copy(out, in)
	out.Close()
}

func TestRunningExecutableIsReplacedByRenaming(t *testing.T) {
	dir := t.TempDir()
	exe := filepath.Join(dir, "openbear-node.exe")
	copyFile(t, filepath.Join(os.Getenv("SystemRoot"), `System32\ping.exe`), exe)
	running := exec.Command(exe, "-n", "4", "127.0.0.1")
	if err := running.Start(); err != nil {
		t.Fatal(err)
	}
	next := stagingPath(exe)
	os.WriteFile(next, []byte("new build"), 0o755)
	if err := os.Rename(next, exe); err == nil {
		t.Fatal("expected Windows to refuse replacing a running executable")
	}
	if err := replaceExecutable(next, exe); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(exe); string(b) != "new build" {
		t.Fatalf("not replaced: %q", b)
	}
	running.Wait()
	RemoveReplaced(exe)
	if _, err := os.Stat(replacedPath(exe)); !os.IsNotExist(err) {
		t.Fatalf("old build left behind: %v", err)
	}
}

func TestConfigDirectoryIsLimitedToAdministrators(t *testing.T) {
	path := filepath.Join(t.TempDir(), "node", "config.json")
	f := newFake()
	if err := SaveConfig(path, f.config("https://a.example", Capabilities{})); err != nil {
		t.Fatal(err)
	}
	sd, err := windows.GetNamedSecurityInfo(filepath.Dir(path), windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		t.Fatal(err)
	}
	if got := sd.String(); got != "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)" {
		t.Fatalf("directory ACL: %s", got)
	}
	if _, err = LoadConfig(path); err != nil {
		t.Fatal(err)
	}
}

func TestJobRunsApartAndStopsTheWholeTree(t *testing.T) {
	exe, _ := os.Executable()
	dir := t.TempDir()
	s := &jobStore{dir: filepath.Join(dir, "jobs"), runner: func() (string, error) { return runnerPath(exe, filepath.Join(dir, "jobs"), "test") }}
	start := func(command string) string {
		v, err := s.handle("job.start", json.RawMessage(mustJSON(map[string]any{"command": command})))
		if err != nil {
			t.Fatal(err)
		}
		return v.(jobView).ID
	}
	status := func(id string) (jobView, string) {
		v, err := s.handle("job.status", json.RawMessage(mustJSON(map[string]any{"id": id})))
		if err != nil {
			t.Fatal(err)
		}
		m := v.(map[string]any)
		return m["job"].(jobView), m["log"].(map[string]any)["data"].(string)
	}
	id := start(`Write-Output "中文 ok"; Write-Error "bad"; exit 4`)
	var view jobView
	var log string
	for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(200 * time.Millisecond) {
		if view, log = status(id); view.State != "running" {
			break
		}
	}
	if view.State != "exited" || view.ExitCode == nil || *view.ExitCode != 4 || !strings.Contains(log, "中文 ok") || !strings.Contains(log, "bad") || strings.Contains(log, "CLIXML") {
		t.Fatalf("job: %+v %q", view, log)
	}
	id = start("$p = Start-Process ping.exe -ArgumentList '-n','300','127.0.0.1' -PassThru -WindowStyle Hidden\nWrite-Output \"child:$($p.Id)\"\nping -n 300 127.0.0.1 | Out-Null")
	var pid int
	for deadline := time.Now().Add(30 * time.Second); pid == 0 && time.Now().Before(deadline); time.Sleep(200 * time.Millisecond) {
		_, log = status(id)
		if m := regexp.MustCompile(`child:(\d+)`).FindStringSubmatch(log); m != nil {
			pid, _ = strconv.Atoi(m[1])
		}
	}
	if pid == 0 {
		t.Fatal("job did not start")
	}
	if _, err := s.handle("job.stop", json.RawMessage(mustJSON(map[string]any{"id": id}))); err != nil {
		t.Fatal(err)
	}
	for deadline := time.Now().Add(10 * time.Second); alive(pid); time.Sleep(100 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatalf("child %d survived the stop", pid)
		}
	}
	if view, _ = status(id); view.State != "stopped" {
		t.Fatalf("stopped job: %+v", view)
	}
}

// Failed echoes read as words: the common statuses as the result kinds the
// page knows, the rest as a short phrase, never a bare number.
func TestICMPStatusInWords(t *testing.T) {
	for status, want := range map[uint32]replyKind{1231: replyUnreachable, 1232: replyUnreachable, 11003: replyUnreachable,
		11010: replyTimeout, 121: replyTimeout, 11013: replyExceeded} {
		if kind, _ := icmpStatus(status); kind != want {
			t.Errorf("status %d: kind %d, want %d", status, kind, want)
		}
	}
	for _, status := range []uint32{11050, 11009, 87, 5} {
		kind, err := icmpStatus(status)
		if text := pingResult(echoReply{kind: kind, err: err}).Error; kind != replyFailed || strings.ContainsAny(text, "0123456789") {
			t.Errorf("status %d: %q", status, text)
		}
	}
	if r := pingResult(echoReply{kind: replyUnreachable}); r.Error != "unreachable" {
		t.Errorf("unreachable: %+v", r)
	}
}
