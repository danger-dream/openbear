package proxy

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func loggedForward(t *testing.T, proto string, dest int) (*Forwarder, *ForwardConfig, string) {
	t.Helper()
	dir := t.TempDir()
	f := Forward{ID: "fw1", Proto: proto, Host: "127.0.0.1", Port: dest, Listen: bothFree(t), Node: loopback}
	c := &ForwardConfig{Port: int(freePort(t).Port()), Forwards: []Forward{f}, Targets: []ForwardTarget{{From: loopback, Host: f.Host, Port: f.Port, Proto: f.Proto}}}
	s := &Forwarder{}
	s.SetFailureLog(dir)
	s.Apply(c, loopback)
	t.Cleanup(s.Close)
	return s, c, filepath.Join(dir, "forward-failures.jsonl")
}

func failureRows(t *testing.T, path string) []forwardFailure {
	t.Helper()
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(data, []byte("127.0.0.1")) || bytes.Contains(data, []byte("secret-payload")) {
		t.Fatalf("private material logged: %s", data)
	}
	var rows []forwardFailure
	for _, line := range bytes.Split(bytes.TrimSpace(data), []byte{'\n'}) {
		var f forwardFailure
		if err := json.Unmarshal(line, &f); err != nil {
			t.Fatal(err)
		}
		rows = append(rows, f)
	}
	return rows
}

func TestForwardFailureBackendAndReply(t *testing.T) {
	s, c, path := loggedForward(t, "tcp", int(freePort(t).Port()))
	for range 3 {
		conn := dialForward(t, c)
		conn.SetReadDeadline(time.Now().Add(3 * time.Second))
		if _, err := conn.Read(make([]byte, 1)); err == nil {
			t.Fatal("accepted a refused backend")
		}
		conn.Close()
	}
	rows := failureRows(t, path)
	if len(rows) != 6 {
		t.Fatalf("want both ends on each attempt, got %+v", rows)
	}
	for i := 0; i < len(rows); i += 2 {
		target, entry := rows[i], rows[i+1]
		if target.Side != "target" || target.Stage != "dial_backend" || target.Reason != "connection_refused" || entry.Rule != "fw1" || entry.Stage != "target_reply" || !strings.HasPrefix(entry.Reason, "target_refused_") || entry.Match == "" || entry.Match != target.Match || entry.Port != uint16(c.Forwards[0].Port) || target.Proto != "tcp" || target.Time.IsZero() {
			t.Fatalf("bad causes: %+v %+v", target, entry)
		}
	}
	st := s.Status()
	if st.Forwards[0].Failed != 3 || st.Target.Failed != 3 || st.Forwards[0].LastError != "target_refused" {
		t.Fatalf("changed failure counters: %+v", st)
	}
}

// A valid tunnel whose far end closes before the one-byte reply must not look like a negative reply.
func TestForwardFailureBeforeAck(t *testing.T) {
	for _, proto := range []string{"tcp", "udp"} {
		for _, reset := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/reset=%t", proto, reset), func(t *testing.T) {
				ln, err := net.ListenTCP("tcp4", &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)})
				if err != nil {
					t.Fatal(err)
				}
				defer ln.Close()
				go func() {
					for {
						c, err := ln.AcceptTCP()
						if err != nil {
							return
						}
						go func() {
							defer c.Close()
							if cmd, _, err := readHeader(c); err == nil {
								if cmd == cmdUDP {
									_, _, _ = readFrame(c, make([]byte, 65536))
								}
								if reset {
									c.SetLinger(0)
								}
							}
						}()
					}
				}()
				dir := t.TempDir()
				s := &Forwarder{}
				s.SetFailureLog(dir)
				f := Forward{ID: "fw1", Listen: bothFree(t), Host: "127.0.0.1", Port: 48888, Proto: proto, Node: loopback}
				cfg := &ForwardConfig{Port: ln.Addr().(*net.TCPAddr).Port, Forwards: []Forward{f}}
				s.Apply(cfg, loopback)
				defer s.Close()
				if proto == "tcp" {
					c := dialForward(t, cfg)
					c.SetReadDeadline(time.Now().Add(3 * time.Second))
					c.Read(make([]byte, 1))
					c.Close()
				} else {
					c, err := net.DialUDP("udp", nil, &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: f.Listen})
					if err != nil {
						t.Fatal(err)
					}
					defer c.Close()
					if _, err = c.Write([]byte("secret-payload")); err != nil {
						t.Fatal(err)
					}
				}
				path := filepath.Join(dir, "forward-failures.jsonl")
				until := time.Now().Add(3 * time.Second)
				for {
					s.journal.mu.Lock()
					rows := failureRows(t, path)
					s.journal.mu.Unlock()
					if len(rows) > 0 {
						r := rows[0]
						if len(rows) != 1 || r.Proto != proto || r.Stage != "read_target_ack" || (r.Reason != "eof" && r.Reason != "connection_reset") {
							t.Fatalf("wrong failure: %+v", rows)
						}
						if proto == "tcp" && ((r.Reason == "connection_reset") != reset) {
							t.Fatalf("wrong TCP cause: %+v", r)
						}
						break
					}
					if time.Now().After(until) {
						t.Fatal("no per-attempt record")
					}
					time.Sleep(5 * time.Millisecond)
				}
			})
		}
	}
}

func TestForwardFailureNoNormalClose(t *testing.T) {
	e := echo(t)
	s, c, path := loggedForward(t, "tcp", int(e.Port()))
	conn := dialForward(t, c)
	roundTrip(t, conn, "secret-payload")
	conn.(*net.TCPConn).CloseWrite()
	conn.SetReadDeadline(time.Now().Add(3 * time.Second))
	if _, err := io.ReadAll(conn); err != nil {
		t.Fatal(err)
	}
	conn.Close()
	s.Close()
	if rows := failureRows(t, path); len(rows) != 0 {
		t.Fatalf("normal FIN/warm pool cleanup reported: %+v", rows)
	}
}

func TestForwardFailureRotation(t *testing.T) {
	path := filepath.Join(t.TempDir(), "forward-failures.jsonl")
	j := &forwardLog{path: path, limit: 300}
	var wg sync.WaitGroup
	for worker := 0; worker < 4; worker++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 20; i++ {
				j.record(forwardFailure{Side: "entry", Rule: "fw1", Proto: "tcp"}, "read_target_ack", "eof")
			}
		}()
	}
	wg.Wait()
	j = &forwardLog{path: path, limit: 300}
	j.record(forwardFailure{Side: "target"}, "dial_backend", "connection_refused")
	files, _ := filepath.Glob(path + "*")
	if len(files) != 4 {
		t.Fatalf("unbounded or missing generations: %v", files)
	}
	for _, p := range files {
		st, err := os.Stat(p)
		if err != nil || st.Size() > 300 || st.Mode().Perm()&0077 != 0 {
			t.Fatalf("bad file %s: %v, %v", p, st, err)
		}
		if len(failureRows(t, p)) == 0 {
			t.Fatal("empty generation", p)
		}
	}
}

func TestForwardFailureSocketReasonsRedacted(t *testing.T) {
	var d net.Dialer
	ctx, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()
	_, err := d.DialContext(ctx, "tcp", "127.0.0.1:48888")
	if err == nil || socketReason(err) != "timeout" {
		t.Fatal("deadline not preserved", err)
	}
	raw := errors.New("user=private password=private 127.0.0.1")
	if socketReason(raw) != "io_error" {
		t.Fatal("arbitrary error content leaked")
	}
}
