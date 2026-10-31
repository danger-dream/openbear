//go:build unix || windows

package node

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// A background job lives in its own directory under the jobs directory,
// independent of the connection and of this process: a runner (this binary
// as "job-run") starts the command in a new session, writes its output to
// capped log segments and its exit code to a file. The node only reads that
// directory, so a restart or update loses nothing.
//
//	job.json        what to run (written before the runner starts)
//	pid, child      "pid start-time" of the runner and of the command
//	stdin           optional input
//	out-<offset>.log  output segments; their names say where they start
//	exit            {"exitCode", "endedAt"}, written when the command ends
//	stop            present once a stop was asked for

var (
	// Output kept per job: the newest segment and the one before it.
	jobLogLimit   int64 = 20 << 20
	jobKeep             = 50
	jobKeepFor          = 7 * 24 * time.Hour
	jobRunningMax       = 20
	// A runner gets this long to write its pid before a missing one counts as dead.
	jobStartGrace = 30 * time.Second
	jobStopGrace  = 5 * time.Second
)

const (
	jobCommandMax = 64 << 10
	jobStdinMax   = 1 << 20
	jobReadMax    = 256 << 10
	jobReadTail   = 64 << 10
)

var jobID = regexp.MustCompile(`^[a-z0-9]{6,40}$`)

type jobSpec struct {
	ID        string `json:"id"`
	Command   string `json:"command"`
	Cwd       string `json:"cwd,omitempty"`
	Shell     string `json:"shell"`
	StartedAt int64  `json:"startedAt"`
	LogLimit  int64  `json:"logLimit"`
	Stdin     bool   `json:"stdin,omitempty"`
}

type jobExit struct {
	ExitCode int    `json:"exitCode"`
	Signal   string `json:"signal,omitempty"`
	EndedAt  int64  `json:"endedAt"`
}

type jobView struct {
	ID        string `json:"id"`
	Command   string `json:"command"`
	Cwd       string `json:"cwd,omitempty"`
	Shell     string `json:"shell"`
	State     string `json:"state"` // running, exited, stopped, interrupted
	ExitCode  *int   `json:"exitCode"`
	Signal    string `json:"signal,omitempty"`
	StartedAt int64  `json:"startedAt"`
	EndedAt   int64  `json:"endedAt,omitempty"`
	// DurationMs is measured by the node, whose clock set the times.
	DurationMs int64 `json:"durationMs"`
	Pid        int   `json:"pid,omitempty"`
}

type jobStore struct {
	mu     sync.Mutex // starts and cleanup
	dir    string
	runner func() (string, error)
}

func (a *Agent) jobs() *jobStore {
	a.jobsOnce.Do(func() {
		dir := a.jobDir
		if dir == "" && a.ConfigPath != "" {
			dir = jobsDir(a.ConfigPath)
		}
		if dir == "" {
			dir = filepath.Join(os.TempDir(), "openbear-node-jobs")
		}
		a.jobStore = &jobStore{dir: dir, runner: func() (string, error) {
			exe, err := a.executable()
			if err != nil {
				return "", err
			}
			return runnerPath(exe, dir, a.build())
		}}
	})
	return a.jobStore
}

func (s *jobStore) handle(method string, raw json.RawMessage) (any, error) {
	switch method {
	case "job.start":
		return s.start(raw)
	case "job.list":
		return s.list()
	}
	var p struct {
		ID     string `json:"id"`
		Offset *int64 `json:"offset"`
		Limit  int64  `json:"limit"`
	}
	if json.Unmarshal(raw, &p) != nil || !jobID.MatchString(p.ID) {
		return nil, errors.New("invalid_params")
	}
	dir := filepath.Join(s.dir, p.ID)
	view, err := readJob(dir)
	if err != nil {
		return nil, err
	}
	switch method {
	case "job.status":
		limit := p.Limit
		if limit <= 0 {
			limit = jobReadTail
		}
		offset := int64(-1)
		if p.Offset != nil {
			offset = max(0, *p.Offset)
		}
		return map[string]any{"job": view, "log": readJobLog(dir, offset, min(limit, jobReadMax))}, nil
	case "job.stop":
		return s.stop(dir, view)
	}
	return nil, errors.New("unsupported_method")
}

func (s *jobStore) start(raw json.RawMessage) (any, error) {
	var p struct {
		Command string `json:"command"`
		Cwd     string `json:"cwd"`
		Stdin   string `json:"stdin"`
	}
	if json.Unmarshal(raw, &p) != nil || strings.TrimSpace(p.Command) == "" || len(p.Command) > jobCommandMax || len(p.Stdin) > jobStdinMax {
		return nil, errors.New("invalid_params")
	}
	if p.Cwd != "" {
		if info, err := os.Stat(p.Cwd); err != nil || !info.IsDir() || !filepath.IsAbs(p.Cwd) {
			return nil, errors.New("invalid_cwd")
		}
	}
	runner, err := s.runner()
	if err != nil {
		return nil, errors.New("start_failed")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if err = os.MkdirAll(s.dir, 0o700); err != nil {
		return nil, fileError(err)
	}
	running := 0
	for _, v := range s.cleanup() {
		if v.State == "running" {
			running++
		}
	}
	if running >= jobRunningMax {
		return nil, errors.New("too_many_jobs")
	}
	now := time.Now()
	spec := jobSpec{ID: strconv.FormatInt(now.UnixMilli(), 36) + randomHex(4), Command: p.Command, Cwd: p.Cwd, Shell: jobShell(),
		StartedAt: now.UnixMilli(), LogLimit: jobLogLimit, Stdin: p.Stdin != ""}
	dir := filepath.Join(s.dir, spec.ID)
	if err = os.Mkdir(dir, 0o700); err != nil {
		return nil, fileError(err)
	}
	data, _ := json.Marshal(spec)
	if err = writeAtomic(dir, "job.json", data); err == nil && spec.Stdin {
		err = os.WriteFile(filepath.Join(dir, "stdin"), []byte(p.Stdin), 0o600)
	}
	if err != nil {
		os.RemoveAll(dir)
		return nil, fileError(err)
	}
	cmd := exec.Command(runner, "job-run", dir)
	if err = startRunner(cmd); err != nil {
		os.RemoveAll(dir)
		return nil, errors.New("start_failed")
	}
	// The runner records itself too; this closes the gap before it runs.
	if token, ok := processStart(cmd.Process.Pid); ok {
		writeAtomic(dir, "pid", []byte(fmt.Sprintf("%d %s\n", cmd.Process.Pid, token)))
	}
	go cmd.Wait()
	return readJob(dir)
}

func (s *jobStore) list() (any, error) {
	s.mu.Lock()
	views := s.cleanup()
	s.mu.Unlock()
	sort.Slice(views, func(i, j int) bool { return views[i].StartedAt > views[j].StartedAt })
	return map[string]any{"jobs": views}, nil
}

// cleanup removes finished jobs past jobKeepFor, then the oldest finished
// ones beyond jobKeep, and returns the jobs that remain.
func (s *jobStore) cleanup() []jobView {
	entries, _ := os.ReadDir(s.dir)
	views := []jobView{}
	now := time.Now()
	for _, e := range entries {
		if !e.IsDir() || !jobID.MatchString(e.Name()) {
			continue
		}
		dir := filepath.Join(s.dir, e.Name())
		v, err := readJob(dir)
		if err != nil {
			// A start that failed half way; give a starting one time to write job.json.
			if info, statErr := e.Info(); statErr == nil && now.Sub(info.ModTime()) > time.Hour {
				os.RemoveAll(dir)
			}
			continue
		}
		ended := time.UnixMilli(max(v.EndedAt, v.StartedAt))
		if v.State != "running" && now.Sub(ended) > jobKeepFor {
			os.RemoveAll(dir)
			continue
		}
		views = append(views, v)
	}
	sort.Slice(views, func(i, j int) bool { return views[i].StartedAt < views[j].StartedAt })
	for extra := len(views) - jobKeep; extra > 0; extra-- {
		i := slices.IndexFunc(views, func(v jobView) bool { return v.State != "running" })
		if i < 0 {
			break
		}
		os.RemoveAll(filepath.Join(s.dir, views[i].ID))
		views = slices.Delete(views, i, i+1)
	}
	return views
}

// stop asks the runner to end the command's whole process group or tree
// and waits for its exit code. When the runner is gone (or does not end it
// in time) the command is killed here, if it still runs.
func (s *jobStore) stop(dir string, view jobView) (any, error) {
	_, orphan := liveProcess(dir, "child")
	if view.State != "running" && !(view.ExitCode == nil && orphan) {
		return map[string]any{"job": view, "stopped": false}, nil
	}
	if err := writeAtomic(dir, "stop", []byte("1\n")); err != nil {
		return nil, fileError(err)
	}
	if pid, ok := liveProcess(dir, "pid"); ok {
		signalRunner(pid)
	}
	deadline := time.Now().Add(jobStopGrace + 3*time.Second)
	for time.Now().Before(deadline) {
		if v, err := readJob(dir); err == nil && v.State != "running" {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if view, _ = readJob(dir); view.ExitCode == nil {
		if pid, ok := liveProcess(dir, "child"); ok {
			killTree(pid)
		}
		if pid, ok := liveProcess(dir, "pid"); ok {
			killTree(pid)
		}
		// A kill lands when the process next runs, which on a loaded machine
		// is later; stopped means both are gone.
		for end := time.Now().Add(3 * time.Second); time.Now().Before(end); time.Sleep(20 * time.Millisecond) {
			_, child := liveProcess(dir, "child")
			_, runner := liveProcess(dir, "pid")
			if !child && !runner {
				break
			}
		}
		view, _ = readJob(dir)
	}
	return map[string]any{"job": view, "stopped": true}, nil
}

// readJob derives a job's state from its directory alone.
func readJob(dir string) (v jobView, err error) {
	defer func() {
		if v.EndedAt > 0 {
			v.DurationMs = v.EndedAt - v.StartedAt
		} else if err == nil {
			v.DurationMs = time.Now().UnixMilli() - v.StartedAt
		}
	}()
	var spec jobSpec
	data, err := os.ReadFile(filepath.Join(dir, "job.json"))
	if err != nil || json.Unmarshal(data, &spec) != nil {
		if errors.Is(err, os.ErrNotExist) {
			return jobView{}, errors.New("job_not_found")
		}
		return jobView{}, errors.New("job_unreadable")
	}
	v = jobView{ID: spec.ID, Command: spec.Command, Cwd: spec.Cwd, Shell: spec.Shell, StartedAt: spec.StartedAt}
	_, err = os.Stat(filepath.Join(dir, "stop"))
	stopped := err == nil
	finish := func() bool {
		var exit jobExit
		data, err := os.ReadFile(filepath.Join(dir, "exit"))
		if err != nil || json.Unmarshal(data, &exit) != nil {
			return false
		}
		v.State, v.ExitCode, v.Signal, v.EndedAt = "exited", &exit.ExitCode, exit.Signal, exit.EndedAt
		if stopped {
			v.State = "stopped"
		}
		return true
	}
	if finish() {
		return v, nil
	}
	pid, alive := liveProcess(dir, "pid")
	v.Pid = pid
	_, err = os.Stat(filepath.Join(dir, "pid"))
	if alive || err != nil && time.Since(time.UnixMilli(spec.StartedAt)) < jobStartGrace {
		v.State = "running"
		return v, nil
	}
	// The runner may have finished between the two reads.
	if finish() {
		return v, nil
	}
	v.State = "interrupted"
	if stopped {
		v.State = "stopped"
	}
	return v, nil
}

// liveProcess tells whether the process recorded in name still runs: same
// pid and same start time, so a reused pid is never taken for it.
func liveProcess(dir, name string) (int, bool) {
	data, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		return 0, false
	}
	fields := strings.Fields(string(data))
	if len(fields) != 2 {
		return 0, false
	}
	pid, err := strconv.Atoi(fields[0])
	if err != nil || pid <= 0 {
		return 0, false
	}
	token, alive := processStart(pid)
	return pid, alive && token == fields[1]
}

func writeAtomic(dir, name string, data []byte) error {
	temp := filepath.Join(dir, "."+name+"-"+randomHex(4))
	if err := os.WriteFile(temp, data, 0o600); err != nil {
		os.Remove(temp)
		return err
	}
	if err := os.Rename(temp, filepath.Join(dir, name)); err != nil {
		os.Remove(temp)
		return err
	}
	return nil
}

type logSegment struct {
	path        string
	start, size int64
}

func logSegments(dir string) []logSegment {
	names, _ := filepath.Glob(filepath.Join(dir, "out-*.log"))
	var segs []logSegment
	for _, name := range names {
		start, err := strconv.ParseInt(strings.TrimSuffix(strings.TrimPrefix(filepath.Base(name), "out-"), ".log"), 10, 64)
		info, statErr := os.Stat(name)
		if err == nil && statErr == nil {
			segs = append(segs, logSegment{name, start, info.Size()})
		}
	}
	sort.Slice(segs, func(i, j int) bool { return segs[i].start < segs[j].start })
	return segs
}

// readJobLog reads up to limit bytes of output from offset, or the tail
// when offset is negative. Offsets count every byte the command ever wrote;
// output older than the kept segments is gone, and first says where the
// kept part starts. A UTF-8 character is never split at either end.
func readJobLog(dir string, offset, limit int64) map[string]any {
	segs := logSegments(dir)
	var first, size int64
	if len(segs) > 0 {
		first, size = segs[0].start, segs[len(segs)-1].start+segs[len(segs)-1].size
	}
	tail := offset < 0
	if tail {
		offset = max(first, size-limit)
	}
	offset = min(max(offset, first), size)
	var data []byte
	for _, seg := range segs {
		if int64(len(data)) >= limit {
			break
		}
		at := offset + int64(len(data))
		if at >= seg.start+seg.size || at < seg.start {
			continue
		}
		f, err := os.Open(seg.path)
		if err != nil {
			break
		}
		buf := make([]byte, min(limit-int64(len(data)), seg.start+seg.size-at))
		n, _ := f.ReadAt(buf, at-seg.start)
		f.Close()
		data = append(data, buf[:n]...)
		if int64(n) < int64(len(buf)) {
			break
		}
	}
	if tail || offset > first {
		skip := 0
		for skip < len(data) && skip < 3 && !utf8.RuneStart(data[skip]) {
			skip++
		}
		data, offset = data[skip:], offset+int64(skip)
	}
	for i := len(data) - 1; i >= 0 && i >= len(data)-4; i-- {
		if utf8.RuneStart(data[i]) {
			if !utf8.FullRune(data[i:]) {
				data = data[:i]
			}
			break
		}
	}
	return map[string]any{"data": string(data), "offset": offset, "next": offset + int64(len(data)), "size": size, "first": first}
}

// jobLog is the runner's side: output goes to the newest segment, and a new
// one starts when it reaches half the limit, dropping all but the previous.
// No segment grows past half, so the two kept never exceed the limit.
type jobLog struct {
	mu          sync.Mutex
	dir         string
	half        int64
	f           *os.File
	start, size int64
}

func newJobLog(dir string, limit int64) (*jobLog, error) {
	l := &jobLog{dir: dir, half: max(limit/2, 1)}
	return l, l.open(0)
}

func (l *jobLog) open(start int64) error {
	f, err := os.OpenFile(filepath.Join(l.dir, fmt.Sprintf("out-%016d.log", start)), os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	l.f, l.start, l.size = f, start, 0
	return nil
}

// Write never fails the command: output that cannot be stored is dropped.
func (l *jobLog) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	// A write that fits in a fresh segment is kept whole; a longer one (the
	// pipe hands over what piled up, which can be far more than a line) is
	// split across segments.
	for rest := p; len(rest) > 0; {
		if l.size > 0 && l.size+int64(len(rest)) > l.half {
			previous := l.start
			l.f.Close()
			if l.open(l.start+l.size) != nil {
				break
			}
			for _, seg := range logSegments(l.dir) {
				if seg.start < previous {
					os.Remove(seg.path)
				}
			}
		}
		if l.f == nil {
			break
		}
		chunk := rest[:min(int64(len(rest)), l.half-l.size)]
		n, _ := l.f.Write(chunk)
		l.size += int64(n)
		if n < len(chunk) {
			break
		}
		rest = rest[n:]
	}
	return len(p), nil
}

func (l *jobLog) Close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.f.Close()
}

// RunJob is the runner ("openbear-node job-run DIR"): it runs the job in
// dir and records how it ended. ctx ends when the runner is told to stop.
func RunJob(ctx context.Context, dir string) error {
	var spec jobSpec
	data, err := os.ReadFile(filepath.Join(dir, "job.json"))
	if err != nil || json.Unmarshal(data, &spec) != nil {
		return errors.New("job.json is missing or invalid")
	}
	if token, ok := processStart(os.Getpid()); ok {
		writeAtomic(dir, "pid", []byte(fmt.Sprintf("%d %s\n", os.Getpid(), token)))
	}
	finish := func(code int, signal string) error {
		exit, _ := json.Marshal(jobExit{ExitCode: code, Signal: signal, EndedAt: time.Now().UnixMilli()})
		return writeAtomic(dir, "exit", exit)
	}
	out, err := newJobLog(dir, spec.LogLimit)
	if err != nil {
		finish(-1, "")
		return err
	}
	defer out.Close()
	cmd, cleanup, err := jobCommand(spec)
	if err != nil {
		fmt.Fprintf(out, "openbear-node: cannot start the job: %v\n", err)
		return finish(-1, "")
	}
	defer cleanup()
	cmd.Dir = spec.Cwd
	if spec.Stdin {
		in, err := os.Open(filepath.Join(dir, "stdin"))
		if err == nil {
			defer in.Close()
			cmd.Stdin = in
		}
	}
	errOut, flush := jobStderr(out)
	cmd.Stdout, cmd.Stderr = out, errOut
	// Output still held open by background children after the command ends
	// is cut off, so the exit code is not held back by them.
	cmd.WaitDelay = 2 * time.Second
	if err = cmd.Start(); err != nil {
		fmt.Fprintf(out, "openbear-node: cannot start the job: %v\n", err)
		return finish(-1, "")
	}
	terminate, release := watchJob(cmd)
	defer release()
	if token, ok := processStart(cmd.Process.Pid); ok {
		writeAtomic(dir, "child", []byte(fmt.Sprintf("%d %s\n", cmd.Process.Pid, token)))
	}
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(500 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-ctx.Done():
			case <-ticker.C:
				if _, err := os.Stat(filepath.Join(dir, "stop")); err != nil {
					continue
				}
			}
			terminate(false)
			select {
			case <-done:
			case <-time.After(jobStopGrace):
				terminate(true)
			}
			return
		}
	}()
	err = cmd.Wait()
	close(done)
	flush()
	if cmd.ProcessState == nil {
		return finish(-1, "")
	}
	code, signal := exitInfo(cmd.ProcessState)
	var exitErr *exec.ExitError
	if err != nil && !errors.As(err, &exitErr) && !errors.Is(err, exec.ErrWaitDelay) {
		fmt.Fprintf(out, "openbear-node: %v\n", err)
	}
	return finish(code, signal)
}
