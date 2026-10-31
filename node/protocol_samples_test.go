//go:build unix

package node

// The real node against the protocol samples in testdata/protocol.md, which the
// controller's tests (tests/fake_node.py) check the controller against: what
// the node sends matches them, and what the controller sends decodes into the
// node's types and gets the replies they show.

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"
)

var (
	samplesOnce sync.Once
	samplesData map[string]any
	samplesErr  error
)

// loadSamples reads each "### name" heading and the ```json block after it.
func loadSamples(t *testing.T) map[string]any {
	t.Helper()
	samplesOnce.Do(func() {
		f, err := os.Open(filepath.Join("testdata", "protocol.md"))
		if err != nil {
			samplesErr = err
			return
		}
		defer f.Close()
		samplesData = map[string]any{}
		name, inBlock := "", false
		var block []string
		scan := bufio.NewScanner(f)
		for scan.Scan() {
			line := scan.Text()
			switch {
			case inBlock && line == "```":
				var v any
				if err := json.Unmarshal([]byte(strings.Join(block, "\n")), &v); err != nil {
					samplesErr = fmt.Errorf("sample %s: %v", name, err)
					return
				}
				if _, dup := samplesData[name]; dup {
					samplesErr = fmt.Errorf("sample %s appears twice", name)
					return
				}
				samplesData[name], name, inBlock, block = v, "", false, nil
			case inBlock:
				block = append(block, line)
			case strings.HasPrefix(line, "### "):
				if name != "" {
					samplesErr = fmt.Errorf("sample %s has no json block", name)
					return
				}
				name = strings.Fields(line[4:])[0]
			case line == "```json" && name != "":
				inBlock = true
			}
		}
		if name != "" || inBlock {
			samplesErr = fmt.Errorf("sample %s is not finished", name)
		}
	})
	if samplesErr != nil {
		t.Fatalf("testdata/protocol.md: %v", samplesErr)
	}
	return samplesData
}

// sample is a copy of one sample, its tokens (like <dir>) replaced.
func sample(t *testing.T, name string, subs map[string]string) any {
	t.Helper()
	v, ok := loadSamples(t)[name]
	if !ok {
		t.Fatalf("no sample %s", name)
	}
	return replaceTokens(generic(t, v), subs)
}

func generic(t *testing.T, v any) any {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	var out any
	json.Unmarshal(data, &out)
	return out
}

func replaceTokens(v any, subs map[string]string) any {
	switch x := v.(type) {
	case string:
		for token, real := range subs {
			x = strings.ReplaceAll(x, token, real)
		}
		return x
	case []any:
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = replaceTokens(e, subs)
		}
		return out
	case map[string]any:
		out := map[string]any{}
		for k, e := range x {
			out[replaceTokens(k, subs).(string)] = replaceTokens(e, subs)
		}
		return out
	}
	return v
}

var refPattern = regexp.MustCompile(`^<ref:(.+)>$`)

// fill makes a value of a pattern: placeholders become zero values, optional
// keys are kept without their "?".
func fill(t *testing.T, v any) any {
	switch x := v.(type) {
	case string:
		if m := refPattern.FindStringSubmatch(x); m != nil {
			return fill(t, sample(t, m[1], nil))
		}
		switch x {
		case "<string>", "<base64>":
			return ""
		case "<int>", "<number>":
			return 0.0
		case "<int?>", "<any>":
			return nil
		case "<bool>":
			return false
		case "<hex64>":
			return strings.Repeat("0", 64)
		}
		return x
	case []any:
		if len(x) > 0 && x[0] == "<each>" {
			return []any{}
		}
		out := make([]any, len(x))
		for i, e := range x {
			out[i] = fill(t, e)
		}
		return out
	case map[string]any:
		out := map[string]any{}
		for k, e := range x {
			out[strings.TrimSuffix(k, "?")] = fill(t, e)
		}
		return out
	}
	return v
}

func isInt(v any) bool {
	f, ok := v.(float64)
	return ok && f == math.Trunc(f)
}

// mismatches says where got differs from the pattern want (see testdata/protocol.md).
func mismatches(t *testing.T, got, want any, where string) []string {
	if s, ok := want.(string); ok {
		if m := refPattern.FindStringSubmatch(s); m != nil {
			return mismatches(t, got, sample(t, m[1], nil), where)
		}
		ok := true
		switch s {
		case "<any>":
		case "<string>":
			_, ok = got.(string)
		case "<int>":
			ok = isInt(got)
		case "<int?>":
			ok = got == nil || isInt(got)
		case "<number>":
			_, ok = got.(float64)
		case "<bool>":
			_, ok = got.(bool)
		case "<base64>":
			str, isStr := got.(string)
			_, err := base64.StdEncoding.DecodeString(str)
			ok = isStr && err == nil
		case "<hex64>":
			str, _ := got.(string)
			ok = regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(str)
		default:
			ok = got == s
		}
		if !ok {
			return []string{fmt.Sprintf("%s: %#v is not %q", where, got, s)}
		}
		return nil
	}
	switch w := want.(type) {
	case map[string]any:
		g, ok := got.(map[string]any)
		if !ok {
			return []string{fmt.Sprintf("%s: %#v is not an object", where, got)}
		}
		var out []string
		keys := map[string]string{}
		for raw := range w {
			keys[strings.TrimSuffix(raw, "?")] = raw
		}
		for k := range g {
			if _, known := keys[k]; !known {
				out = append(out, fmt.Sprintf("%s: unexpected key %q", where, k))
			}
		}
		for k, raw := range keys {
			v, present := g[k]
			if !present {
				if !strings.HasSuffix(raw, "?") {
					out = append(out, fmt.Sprintf("%s: missing key %q", where, k))
				}
				continue
			}
			out = append(out, mismatches(t, v, w[raw], where+"."+k)...)
		}
		sort.Strings(out)
		return out
	case []any:
		g, ok := got.([]any)
		if !ok {
			return []string{fmt.Sprintf("%s: %#v is not a list", where, got)}
		}
		var out []string
		if len(w) == 2 && w[0] == "<each>" {
			for i, v := range g {
				out = append(out, mismatches(t, v, w[1], fmt.Sprintf("%s[%d]", where, i))...)
			}
			return out
		}
		if len(g) != len(w) {
			return []string{fmt.Sprintf("%s: %d items, the sample has %d: %v", where, len(g), len(w), got)}
		}
		for i := range w {
			out = append(out, mismatches(t, g[i], w[i], fmt.Sprintf("%s[%d]", where, i))...)
		}
		return out
	}
	if !reflect.DeepEqual(got, want) {
		return []string{fmt.Sprintf("%s: %#v is not %#v", where, got, want)}
	}
	return nil
}

func mustMatch(t *testing.T, label string, got, want any) {
	t.Helper()
	if found := mismatches(t, generic(t, got), want, "$"); len(found) > 0 {
		t.Errorf("%s does not match its sample:\n  %s\n  got %v", label, strings.Join(found, "\n  "), got)
	}
}

// decodeStrictly decodes v into target and fails on any key the target lacks.
func decodeStrictly(t *testing.T, label string, v any, target any) {
	t.Helper()
	data, _ := json.Marshal(v)
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(target); err != nil {
		t.Errorf("%s: the node does not take the sample %s: %v", label, data, err)
	}
}

// paramTypes are the node's own types for call parameters, where it has one.
var paramTypes = map[string]func() any{
	"exec": func() any { return &ExecParams{} }, "fs.list": func() any { return &fileParams{} }, "fs.stat": func() any { return &fileParams{} },
	"fs.read": func() any { return &fileParams{} }, "fs.write": func() any { return &fileParams{} }, "fs.edit": func() any { return &editParams{} },
	"update": func() any { return &updateParams{} },
}

// callSample sends one call:METHOD[/variant] sample and checks the reply.
func callSample(t *testing.T, ch *channel, name string, subs map[string]string) map[string]any {
	t.Helper()
	s := sample(t, name, subs).(map[string]any)
	method := strings.SplitN(strings.TrimPrefix(name, "call:"), "/", 2)[0]
	params := fill(t, s["params"])
	if newType, ok := paramTypes[method]; ok {
		decodeStrictly(t, name+" params", params, newType())
	}
	reply := call(t, ch, method, params)
	if code, ok := s["error"]; ok {
		mustMatch(t, name+" reply", reply, sample(t, "msg:reply/error", nil))
		if reply["error"] != code {
			t.Errorf("%s: error %v, the sample says %v", name, reply["error"], code)
		}
		return reply
	}
	mustMatch(t, name+" reply", reply, sample(t, "msg:reply", nil))
	mustMatch(t, name+" result", reply["result"], s["result"])
	result, _ := reply["result"].(map[string]any)
	return result
}

// sampledFakeController answers the handshake with a welcome and session config of the samples' shape.
func sampledFakeController(t *testing.T) *fakeController {
	f := newFake()
	f.sessionConfig = map[string]any{"protocol": ProtocolVersion, "protocolMin": ProtocolMin, "signedParams": true}
	f.welcome = func(base []byte, auth map[string]any) map[string]any {
		bound := paramsBase(base, []byte(auth["params"].(string)))
		return map[string]any{"paramsSig": b64.EncodeToString(ed25519.Sign(f.key, signed("server-params", bound)))}
	}
	return f
}

func TestProtocolSamplesCoverEveryOneOnce(t *testing.T) {
	var names []string
	for name := range loadSamples(t) {
		names = append(names, name)
	}
	sort.Strings(names)
	// Each one is checked below; a new sample needs a check of its own here too.
	want := []string{"browser", "call:browser.artifact", "call:browser.artifact/outside", "call:browser.call", "call:browser.call/not_started",
		"call:browser.cdp", "call:browser.cdp/not_forwarded", "call:browser.config", "call:browser.restart", "call:browser.start",
		"call:browser.status", "call:browser.upload", "enroll:fingerprint", "call:config.url", "call:config.url/unchanged", "call:exec", "call:exec/timeout", "call:fs.edit", "call:fs.edit/crlf",
		"call:fs.edit/failures", "call:fs.edit/not_text", "call:fs.list", "call:fs.read", "call:fs.read/missing", "call:fs.stat", "call:fs.write",
		"call:fs.write/partial", "call:job.list", "call:job.start", "call:job.status", "call:job.stop", "call:no.such.method", "call:ping",
		"call:uninstall", "call:update", "enroll:error", "enroll:request", "enroll:response", "entry", "info", "job", "msg:auth", "msg:auth.params",
		"msg:call", "msg:challenge", "msg:close", "msg:config", "msg:hello", "msg:reply", "msg:reply/error", "msg:report", "msg:term.ack",
		"msg:term.close", "msg:term.closed", "msg:term.open", "msg:term.opened", "msg:term.resize", "msg:welcome", "refusal:capability_disabled"}
	sort.Strings(want)
	if !reflect.DeepEqual(names, want) {
		t.Fatalf("samples changed: have %v\nchecked %v", names, want)
	}
}

func TestProtocolSamplesHandshakeHelloAndReport(t *testing.T) {
	f := sampledFakeController(t)
	f.welcome = func(base []byte, auth map[string]any) map[string]any {
		bound := paramsBase(base, []byte(auth["params"].(string)))
		w := map[string]any{"paramsSig": b64.EncodeToString(ed25519.Sign(f.key, signed("server-params", bound)))}
		mustMatch(t, "the test's welcome", map[string]any{"t": "welcome", "sig": b64.EncodeToString([]byte("x")), "paramsSig": w["paramsSig"]},
			sample(t, "msg:welcome", nil))
		return w
	}
	probed := make(chan map[string]any, 1)
	done := make(chan struct{})
	s := f.serve(t, func(ch *channel, auth map[string]any) {
		var params any
		json.Unmarshal([]byte(auth["params"].(string)), &params)
		if auth["probe"] == true {
			mustMatch(t, "probe auth.params", params, sample(t, "msg:auth.params", nil))
			probed <- auth
			return
		}
		defer close(done)
		mustMatch(t, "auth", auth, sample(t, "msg:auth", nil))
		mustMatch(t, "auth.params", params, sample(t, "msg:auth.params", nil))
		mustMatch(t, "hello", next(t, ch, "hello"), sample(t, "msg:hello", nil))
		mustMatch(t, "report", next(t, ch, "report"), sample(t, "msg:report", nil))
		revoke(ch)
	})
	defer s.Close()
	a := f.agent(s.URL, Capabilities{Exec: true, Terminal: true})
	if err := runSession(t, a); err == nil {
		t.Fatal("the session must end with the revocation")
	}
	<-done
	// A probe of an address says so in its signed parameters.
	conn, _, first, err := a.dial(context.Background(), a.config(), handshakeParams(true))
	if err != nil {
		t.Fatalf("probe: %v", err)
	}
	conn.Close()
	mustMatch(t, "probe auth", <-probed, sample(t, "msg:auth", nil))
	if first.T != "config" {
		t.Fatalf("probe got %v", first)
	}
}

// The controller's messages, filled in from the samples, are all things the node reads.
func TestProtocolSamplesDecodeIntoTheNodesTypes(t *testing.T) {
	// The node does not read these keys; it needs nothing from them.
	ignored := map[string][]string{"msg:config": {"probe"}}
	for _, name := range []string{"msg:challenge", "msg:welcome", "msg:config", "msg:call", "msg:close", "msg:term.open", "msg:term.resize", "msg:term.ack", "msg:term.close"} {
		m := fill(t, sample(t, name, nil)).(map[string]any)
		for _, k := range ignored[name] {
			delete(m, k)
		}
		decodeStrictly(t, name, m, &envelope{})
	}
}

func TestProtocolSamplesCalls(t *testing.T) {
	dir := t.TempDir()
	f := sampledFakeController(t)
	results := map[string]map[string]any{}
	var s *httptest.Server
	done := make(chan struct{})
	var subs map[string]string
	var a *Agent
	s = f.serve(t, func(ch *channel, auth map[string]any) {
		defer close(done)
		next(t, ch, "hello")
		for _, name := range []string{"call:fs.write", "call:fs.write/partial", "call:fs.stat", "call:fs.list", "call:fs.read", "call:fs.read/missing"} {
			results[name] = callSample(t, ch, name, subs)
		}
		// The unfinished upload made its folder; folders come first.
		if entries := results["call:fs.list"]["entries"].([]any); len(entries) != 2 || entries[0].(map[string]any)["name"] != "sub" ||
			entries[1].(map[string]any)["name"] != "upload.txt" {
			t.Errorf("fs.list: %v", entries)
		}
		for _, name := range []string{"call:fs.edit", "call:fs.edit/failures", "call:fs.edit/crlf", "call:fs.edit/not_text"} {
			edit := sample(t, name, subs).(map[string]any)
			path := fill(t, edit["params"]).(map[string]any)["path"].(string)
			text, _ := edit["file"].(string)
			before := []byte(text)
			if raw, ok := edit["fileBase64"].(string); ok {
				before, _ = base64.StdEncoding.DecodeString(raw)
			}
			if err := os.WriteFile(path, before, 0o640); err != nil {
				t.Fatal(err)
			}
			callSample(t, ch, name, subs)
			after := before
			if text, ok := edit["after"].(string); ok {
				after = []byte(text)
			}
			if got, _ := os.ReadFile(path); !bytes.Equal(got, after) {
				t.Errorf("%s left %q, the sample says %q", name, got, after)
			}
			if info, _ := os.Stat(path); info.Mode().Perm() != 0o640 {
				t.Errorf("%s changed the mode to %v", name, info.Mode())
			}
		}
		callSample(t, ch, "call:exec", subs)
		callSample(t, ch, "call:exec/timeout", subs)
		job := callSample(t, ch, "call:job.start", subs)
		subs["<job>"] = job["id"].(string)
		callSample(t, ch, "call:job.list", subs)
		status := callSample(t, ch, "call:job.status", subs)
		if status["log"].(map[string]any)["offset"] != 0.0 {
			t.Errorf("job.status from offset 0: %v", status)
		}
		stopped := callSample(t, ch, "call:job.stop", subs)
		if stopped["stopped"] != true || stopped["job"].(map[string]any)["state"] != "stopped" {
			t.Errorf("job.stop: %v", stopped)
		}
		callSample(t, ch, "call:update", subs)
		callSample(t, ch, "call:config.url/unchanged", subs)
		callSample(t, ch, "call:ping", subs)
		callSample(t, ch, "call:no.such.method", subs)
		callSample(t, ch, "call:uninstall", subs)
		ch.conn.Read() // the node ends the session after its reply
	})
	defer s.Close()
	a = f.agent(s.URL, Capabilities{Exec: true, Terminal: true})
	exe := filepath.Join(t.TempDir(), "openbear-node")
	os.WriteFile(exe, []byte("a node build"), 0o755)
	a.exe = exe
	a.jobsOnce.Do(func() { a.jobStore = testStore(t) })
	base, _ := ControllerURL(s.URL)
	subs = map[string]string{"<dir>": dir, "<binary>": BinaryName(), "<build>": a.build(), "<url>": base.String()}
	runSession(t, a)
	<-done
	a.mu.Lock()
	then := a.next
	a.mu.Unlock()
	if then != ErrUninstalled {
		t.Fatalf("uninstall must end the run, got %v", then)
	}
}

func TestProtocolSamplesConfigURLChange(t *testing.T) {
	f := sampledFakeController(t)
	done := make(chan struct{})
	var subs map[string]string
	s := f.serve(t, func(ch *channel, auth map[string]any) {
		if auth["probe"] == true {
			return // a probe of the new address or the backup
		}
		defer close(done)
		next(t, ch, "hello")
		callSample(t, ch, "call:config.url", subs)
	})
	defer s.Close()
	base, _ := ControllerURL(s.URL)
	moved, _ := ControllerURL(strings.Replace(s.URL, "127.0.0.1", "localhost", 1))
	subs = map[string]string{"<url2>": moved.String(), "<url>": base.String()}
	a := f.agent(s.URL, Capabilities{Exec: true})
	runSession(t, a)
	<-done
	if c := a.config(); c.URL != moved.String() || c.Backup != base.String() {
		t.Fatalf("config after the change: %+v", c)
	}
}

func TestProtocolSamplesRefusals(t *testing.T) {
	f := sampledFakeController(t)
	done := make(chan struct{})
	s := f.serve(t, func(ch *channel, _ map[string]any) {
		defer close(done)
		if caps := next(t, ch, "hello")["caps"].(map[string]any); caps["exec"] != false {
			t.Errorf("caps: %v", caps)
		}
		for _, method := range sample(t, "refusal:capability_disabled", nil).([]any) {
			reply := call(t, ch, method.(string), map[string]any{})
			mustMatch(t, method.(string), reply, sample(t, "msg:reply/error", nil))
			if reply["error"] != "capability_disabled" {
				t.Errorf("%s with commands off: %v", method, reply)
			}
		}
		revoke(ch)
	})
	defer s.Close()
	runSession(t, f.agent(s.URL, Capabilities{Exec: false, Terminal: true}))
	<-done
}

func TestProtocolSamplesTerminal(t *testing.T) {
	message := func(name string, values map[string]any) map[string]any {
		m := fill(t, sample(t, name, nil)).(map[string]any)
		for k, v := range values {
			if _, ok := m[k]; !ok {
				t.Fatalf("%s has no %s", name, k)
			}
			m[k] = v
		}
		decodeStrictly(t, name, m, &envelope{})
		return m
	}
	terminalSession(t, Capabilities{Exec: true, Terminal: true}, func(ch *channel) {
		control := map[string]map[string]any{}
		ch.send(message("msg:term.open", map[string]any{"sid": 7, "cols": 100, "rows": 30}))
		output(t, ch, 7, "term.opened", control)
		mustMatch(t, "term.opened", control["term.opened"], sample(t, "msg:term.opened", nil))
		ch.send(message("msg:term.resize", map[string]any{"sid": 7, "cols": 120, "rows": 40}))
		time.Sleep(100 * time.Millisecond)
		termInput(ch, 7, "stty size; echo done-$((6*7))\n")
		if out := output(t, ch, 7, "done-42", nil); !strings.Contains(out, "40 120") {
			t.Errorf("resize did not reach the shell: %q", out)
		}
		ch.send(message("msg:term.ack", map[string]any{"sid": 7, "n": 1024}))
		termInput(ch, 7, "exit\n")
		output(t, ch, 7, "term.closed", control)
		mustMatch(t, "term.closed (exit)", control["term.closed"], sample(t, "msg:term.closed", nil))
		if control["term.closed"]["reason"] != "exited" {
			t.Errorf("term.closed: %v", control["term.closed"])
		}
		ch.send(message("msg:term.open", map[string]any{"sid": 8, "cols": 80, "rows": 24}))
		output(t, ch, 8, "term.opened", control)
		ch.send(message("msg:term.close", map[string]any{"sid": 8}))
		output(t, ch, 8, "term.closed", control)
		if control["term.closed"]["reason"] != "closed" || control["term.closed"]["sid"] != 8.0 {
			t.Errorf("term.closed after term.close: %v", control["term.closed"])
		}
	})
}

func TestProtocolSamplesEnroll(t *testing.T) {
	_, controller, _ := ed25519.GenerateKey(rand.Reader)
	var answer any
	got := make(chan any, 2)
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body any
		data, _ := io.ReadAll(r.Body)
		json.Unmarshal(data, &body)
		got <- body
		json.NewEncoder(w).Encode(answer)
	}))
	defer s.Close()
	response := fill(t, sample(t, "enroll:response", nil)).(map[string]any)
	response["nodeId"], response["controllerKey"] = "srv-9", b64.EncodeToString(controller.Public().(ed25519.PublicKey))
	answer = response
	c, err := Enroll(context.Background(), s.URL, "", "c0de", "", Capabilities{Exec: true})
	if err != nil || c.NodeID != "srv-9" {
		t.Fatalf("enroll: %v %+v", err, c)
	}
	mustMatch(t, "enroll request", <-got, sample(t, "enroll:request", nil))
	refusal := fill(t, sample(t, "enroll:error", nil)).(map[string]any)
	refusal["error"] = "invalid_enrollment_code"
	answer = refusal
	if _, err = Enroll(context.Background(), s.URL, "", "c0de", "", Capabilities{Exec: true}); err == nil || !strings.Contains(err.Error(), "invalid_enrollment_code") {
		t.Fatalf("refused enroll: %v", err)
	}
	<-got
}

// browserSample runs one call:browser.* sample through the node's browser methods.
func browserSample(t *testing.T, a *Agent, name string, subs map[string]string) map[string]any {
	t.Helper()
	s := sample(t, name, subs).(map[string]any)
	method := strings.SplitN(strings.TrimPrefix(name, "call:"), "/", 2)[0]
	raw, _ := json.Marshal(fill(t, s["params"]))
	result, err := a.browserCall(context.Background(), envelope{T: "call", Method: method, Params: raw})
	if code, ok := s["error"]; ok {
		if err == nil || err.Error() != code {
			t.Errorf("%s: got %v %v, the sample says error %v", name, result, err, code)
		}
		return nil
	}
	if err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	mustMatch(t, name+" result", result, s["result"])
	out, _ := generic(t, result).(map[string]any)
	return out
}

func TestProtocolSamplesBrowser(t *testing.T) {
	a, h := browserAgent(t)
	a.Config.Capabilities.Browser = false
	if _, err := a.browserCall(context.Background(), envelope{Method: "browser.start", Params: json.RawMessage(`{}`)}); err == nil || err.Error() != "capability_disabled" {
		t.Fatalf("browser.start with the browser off: %v", err)
	}
	if status := browserSample(t, a, "call:browser.config", nil); status["enabled"] != true || status["endpoint"] != "ws://127.0.0.1:1/devtools/browser/none" {
		t.Fatalf("browser.config: %v", status)
	}
	browserSample(t, a, "call:browser.status", nil)
	if status := browserSample(t, a, "call:browser.start", nil); status["state"] != "failed" || status["error"] == nil {
		t.Fatalf("browser.start on an endpoint nothing answers: %v", status)
	}
	browserSample(t, a, "call:browser.restart", nil)
	browserSample(t, a, "call:browser.call/not_started", nil)
	browserSample(t, a, "call:browser.cdp/not_forwarded", nil)
	engine, _ := pipeBrowser(t, nil)
	attachEngine(h, engine)
	if r := browserSample(t, a, "call:browser.call", nil); !reflect.DeepEqual(r["result"], map[string]any{"configured": true}) {
		t.Fatalf("browser.call configure: %v", r)
	}
	if r := browserSample(t, a, "call:browser.cdp", nil); r["epoch"] != "epoch-1" || r["result"] == nil {
		t.Fatalf("browser.cdp: %v", r)
	}
	uploaded := browserSample(t, a, "call:browser.upload", nil)
	if data, err := os.ReadFile(fmt.Sprint(uploaded["path"])); err != nil || string(data) != "hi" {
		t.Fatalf("browser.upload: %v %q %v", uploaded, data, err)
	}
	artifacts := filepath.Join(h.dir, "artifacts")
	os.MkdirAll(artifacts, 0o700)
	artifact := filepath.Join(artifacts, "0123456789abcdef01234567")
	os.WriteFile(artifact, []byte("png"), 0o600)
	s := sample(t, "call:browser.artifact", nil).(map[string]any)
	params := fill(t, s["params"]).(map[string]any)
	params["path"] = artifact
	raw, _ := json.Marshal(params)
	result, err := a.browserCall(context.Background(), envelope{Method: "browser.artifact", Params: raw})
	if err != nil {
		t.Fatal(err)
	}
	mustMatch(t, "browser.artifact", result, s["result"])
	if _, err := os.Stat(artifact); !os.IsNotExist(err) {
		t.Fatal("the last read must remove the artifact")
	}
	browserSample(t, a, "call:browser.artifact/outside", nil)
}

// The fingerprint in an install command is what the node compares with the key it enrolls with.
func TestProtocolSamplesEnrollFingerprint(t *testing.T) {
	s := sample(t, "enroll:fingerprint", nil).(map[string]any)
	key, _ := base64.StdEncoding.DecodeString(s["controllerKey"].(string))
	if sum := sha256.Sum256(key); hex.EncodeToString(sum[:]) != s["fingerprint"] {
		t.Fatalf("the sample's fingerprint is not the sha256 of its key")
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"ok": true, "nodeId": "srv-1", "controllerKey": s["controllerKey"]})
	}))
	defer srv.Close()
	fingerprint := s["fingerprint"].(string)
	for _, given := range []string{fingerprint, strings.ToUpper(fingerprint)} {
		if _, err := Enroll(context.Background(), srv.URL, "", "c0de", given, Capabilities{Exec: true}); err != nil {
			t.Fatalf("enroll with %s: %v", given, err)
		}
	}
	_, err := Enroll(context.Background(), srv.URL, "", "c0de", strings.Repeat("0", 64), Capabilities{Exec: true})
	if err == nil || !strings.Contains(err.Error(), "enrollment stopped") || !strings.Contains(err.Error(), fingerprint) {
		t.Fatalf("enroll with another fingerprint: %v", err)
	}
}
