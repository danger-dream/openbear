//go:build unix

package node

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

func testStore(t *testing.T) *jobStore {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	return &jobStore{dir: filepath.Join(t.TempDir(), "jobs"), runner: func() (string, error) { return exe, nil }}
}

func jobCall(t *testing.T, s *jobStore, method string, p map[string]any) map[string]any {
	t.Helper()
	raw, _ := json.Marshal(p)
	v, err := s.handle(method, raw)
	if err != nil {
		t.Fatalf("%s: %v", method, err)
	}
	out, _ := json.Marshal(v)
	var m map[string]any
	json.Unmarshal(out, &m)
	return m
}

// waitJob polls until the job leaves running and returns its status.
func waitJob(t *testing.T, s *jobStore, id string) map[string]any {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		r := jobCall(t, s, "job.status", map[string]any{"id": id})
		if r["job"].(map[string]any)["state"] != "running" {
			return r
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatalf("job %s still running", id)
	return nil
}

func TestJobRunsApartAndRecordsItsExit(t *testing.T) {
	s := testStore(t)
	start := time.Now()
	job := jobCall(t, s, "job.start", map[string]any{"command": "read line; echo \"got $line\"; sleep 1; echo err >&2; exit 3", "stdin": "hello\n"})
	if time.Since(start) > 2*time.Second || job["state"] != "running" || job["id"] == "" {
		t.Fatalf("start must return at once: %v", job)
	}
	id := job["id"].(string)
	r := waitJob(t, s, id)
	got := r["job"].(map[string]any)
	if got["state"] != "exited" || got["exitCode"].(float64) != 3 || got["endedAt"].(float64) < got["startedAt"].(float64) {
		t.Fatalf("exit not recorded: %v", got)
	}
	if log := r["log"].(map[string]any); log["data"] != "got hello\nerr\n" || log["next"].(float64) != 14 {
		t.Fatalf("log: %v", log)
	}
	// A new store, as after a node restart, reads the same state from disk.
	again := &jobStore{dir: s.dir, runner: s.runner}
	list := jobCall(t, again, "job.list", nil)["jobs"].([]any)
	if len(list) != 1 || list[0].(map[string]any)["exitCode"].(float64) != 3 {
		t.Fatalf("list after restart: %v", list)
	}
	part := jobCall(t, again, "job.status", map[string]any{"id": id, "offset": 4, "limit": 5})["log"].(map[string]any)
	if part["data"] != "hello" || part["next"].(float64) != 9 {
		t.Fatalf("offset read: %v", part)
	}
}

func TestJobStopEndsTheWholeProcessGroup(t *testing.T) {
	s := testStore(t)
	dir := t.TempDir()
	job := jobCall(t, s, "job.start", map[string]any{"command": "sleep 300 & echo $! > child; echo started; wait", "cwd": dir})
	id := job["id"].(string)
	var child int
	for i := 0; i < 100 && child == 0; i++ {
		b, _ := os.ReadFile(filepath.Join(dir, "child"))
		child, _ = strconv.Atoi(strings.TrimSpace(string(b)))
		time.Sleep(50 * time.Millisecond)
	}
	if child == 0 {
		t.Fatal("job did not start")
	}
	r := jobCall(t, s, "job.stop", map[string]any{"id": id})
	got := r["job"].(map[string]any)
	if r["stopped"] != true || got["state"] != "stopped" || got["exitCode"] == nil {
		t.Fatalf("stop: %v", r)
	}
	for i := 0; i < 50 && syscall.Kill(child, 0) == nil; i++ {
		time.Sleep(50 * time.Millisecond)
	}
	if syscall.Kill(child, 0) == nil {
		t.Fatal("the job's child survived the stop")
	}
	if again := jobCall(t, s, "job.stop", map[string]any{"id": id}); again["stopped"] != false {
		t.Fatalf("stopping a finished job: %v", again)
	}
}

func TestJobWhoseRunnerDiedIsInterrupted(t *testing.T) {
	s := testStore(t)
	job := jobCall(t, s, "job.start", map[string]any{"command": "sleep 30"})
	id := job["id"].(string)
	dir := filepath.Join(s.dir, id)
	var runner int
	for i := 0; i < 100; i++ {
		if _, ok := liveProcess(dir, "child"); ok {
			runner, _ = liveProcess(dir, "pid")
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if runner == 0 {
		t.Fatal("runner did not record itself")
	}
	syscall.Kill(runner, syscall.SIGKILL)
	// The kill lands when the runner next runs, later on a loaded machine.
	var got map[string]any
	for end := time.Now().Add(10 * time.Second); time.Now().Before(end); time.Sleep(50 * time.Millisecond) {
		if got = jobCall(t, s, "job.status", map[string]any{"id": id})["job"].(map[string]any); got["state"] != "running" {
			break
		}
	}
	if got["state"] != "interrupted" || got["exitCode"] != nil {
		t.Fatalf("dead runner: %v", got)
	}
	// Its command still runs; a stop ends it.
	if r := jobCall(t, s, "job.stop", map[string]any{"id": id}); r["stopped"] != true {
		t.Fatalf("stop orphan: %v", r)
	}
	if _, ok := liveProcess(dir, "child"); ok {
		t.Fatal("orphaned command survived the stop")
	}
}

func TestJobLogKeepsOnlyTheNewestOutput(t *testing.T) {
	defer func(limit int64) { jobLogLimit = limit }(jobLogLimit)
	jobLogLimit = 4000
	s := testStore(t)
	job := jobCall(t, s, "job.start", map[string]any{"command": "i=0; while [ $i -lt 500 ]; do echo \"line $i\"; i=$((i+1)); done"})
	r := waitJob(t, s, job["id"].(string))
	log := r["log"].(map[string]any)
	var kept int64
	for _, seg := range logSegments(filepath.Join(s.dir, job["id"].(string))) {
		kept += seg.size
	}
	if kept > 4000 || log["first"].(float64) == 0 || !strings.HasSuffix(log["data"].(string), "line 499\n") {
		t.Fatalf("log not capped: kept %d, %v", kept, log["first"])
	}
	head := jobCall(t, s, "job.status", map[string]any{"id": job["id"], "offset": 0})["log"].(map[string]any)
	if head["offset"] != log["first"] {
		t.Fatalf("reading dropped output starts at the kept part: %v", head)
	}
}

// Output handed over in one write longer than a segment, as a pipe does once
// it has piled up, is split so the kept segments stay within the limit.
func TestJobLogSplitsALongWrite(t *testing.T) {
	dir := t.TempDir()
	l, err := newJobLog(dir, 4000)
	if err != nil {
		t.Fatal(err)
	}
	l.Write([]byte(strings.Repeat("a", 300)))
	l.Write([]byte(strings.Repeat("b", 4500)))
	l.Write([]byte("line\n"))
	l.Close()
	var kept int64
	segs := logSegments(dir)
	for _, seg := range segs {
		if seg.size > 2000 {
			t.Fatalf("segment of %d bytes", seg.size)
		}
		kept += seg.size
	}
	log := readJobLog(dir, -1, 1<<20)
	if kept > 4000 || segs[0].start == 0 || log["size"] != int64(4805) || !strings.HasSuffix(log["data"].(string), "bbline\n") {
		t.Fatalf("kept %d from %d: %v", kept, segs[0].start, log["size"])
	}
}

func TestFinishedJobsAreCleanedUp(t *testing.T) {
	defer func(keep int, keepFor time.Duration) { jobKeep, jobKeepFor = keep, keepFor }(jobKeep, jobKeepFor)
	jobKeep = 3
	s := testStore(t)
	var ids []string
	for i := 0; i < 5; i++ {
		ids = append(ids, jobCall(t, s, "job.start", map[string]any{"command": "exit " + strconv.Itoa(i)})["id"].(string))
		waitJob(t, s, ids[i])
		time.Sleep(5 * time.Millisecond)
	}
	list := jobCall(t, s, "job.list", nil)["jobs"].([]any)
	if len(list) != 3 || list[0].(map[string]any)["id"] != ids[4] {
		t.Fatalf("keep the newest three: %v", list)
	}
	jobKeepFor = time.Millisecond
	time.Sleep(10 * time.Millisecond)
	if list = jobCall(t, s, "job.list", nil)["jobs"].([]any); len(list) != 0 {
		t.Fatalf("old jobs must go: %v", list)
	}
	if _, err := s.handle("job.status", json.RawMessage(`{"id":"`+ids[4]+`"}`)); err == nil || err.Error() != "job_not_found" {
		t.Fatalf("removed job: %v", err)
	}
}

func TestShellIsBashWhenPresent(t *testing.T) {
	if _, err := os.Stat("/bin/bash"); err != nil {
		t.Skip("no bash here")
	}
	home := t.TempDir()
	os.WriteFile(filepath.Join(home, ".profile"), []byte("PATH=\"$PATH:/opt/from-profile/bin\"\n"), 0o644)
	t.Setenv("HOME", home)
	r, err := Exec(t.Context(), json.RawMessage(`{"command":"[[ 1 == 1 ]] && echo \"$PATH\""}`))
	if err != nil || r.ExitCode != 0 || r.Shell != "/bin/bash" || !strings.Contains(r.Stdout, "/opt/from-profile/bin") {
		t.Fatalf("bash login shell: %+v %v", r, err)
	}
	// The controller's own commands ask for plain sh.
	r, _ = Exec(t.Context(), json.RawMessage(`{"command":"echo \"$PATH\"","shell":"sh"}`))
	if r.Shell != "/bin/sh" || strings.Contains(r.Stdout, "/opt/from-profile/bin") {
		t.Fatalf("plain sh: %+v", r)
	}
	defer func(path string) { bashPath = path }(bashPath)
	bashPath = filepath.Join(home, "no-bash")
	r, _ = Exec(t.Context(), json.RawMessage(`{"command":"echo ok"}`))
	if r.Shell != "/bin/sh" || r.Stdout != "ok\n" {
		t.Fatalf("fallback to sh: %+v", r)
	}
}

func TestShellFillsInAnEmptyHome(t *testing.T) {
	t.Setenv("HOME", "")
	t.Setenv("USER", "")
	t.Setenv("LOGNAME", "")
	r, err := Exec(t.Context(), json.RawMessage(`{"command":"echo \"$HOME|$USER|$LOGNAME\"","shell":"sh"}`))
	if err != nil || strings.HasPrefix(r.Stdout, "|") || strings.Contains(r.Stdout, "||") {
		t.Fatalf("environment not filled in: %q %v", r.Stdout, err)
	}
}
