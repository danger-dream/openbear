//go:build unix

package node

import (
	"encoding/binary"
	"encoding/json"
	"regexp"
	"strconv"
	"syscall"
	"testing"
	"time"
)

var pidLine = regexp.MustCompile(`pid-(\d+)\r?\n`)

// shellPid waits for the shell's echo of $$ (the typed line shows "$$", not digits).
func shellPid(t *testing.T, ch *channel, sid uint32) int {
	t.Helper()
	out := output(t, ch, sid, "\n", nil)
	for pidLine.FindStringSubmatch(out) == nil {
		out += output(t, ch, sid, "\n", nil)
	}
	pid, _ := strconv.Atoi(pidLine.FindStringSubmatch(out)[1])
	return pid
}

func gone(t *testing.T, pid int) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for pid > 0 && syscall.Kill(pid, 0) == nil {
		if time.Now().After(deadline) {
			t.Fatalf("shell %d still running", pid)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func TestTerminalRunsResizesAndClosesTheShell(t *testing.T) {
	var pid int
	terminalSession(t, Capabilities{Terminal: true}, func(ch *channel) {
		control := map[string]map[string]any{}
		open(t, ch, 7)
		termInput(ch, 7, "echo ready-$((40+2)); echo pid-$$\r")
		pid = shellPid(t, ch, 7)
		ch.send(map[string]any{"t": "term.resize", "sid": 7, "cols": 132, "rows": 40})
		time.Sleep(100 * time.Millisecond)
		termInput(ch, 7, "stty size\r")
		output(t, ch, 7, "40 132", nil)
		ch.send(map[string]any{"t": "term.close", "sid": 7})
		output(t, ch, 7, "term.closed", control)
		if control["term.closed"]["sid"].(float64) != 7 || control["term.closed"]["reason"] != "closed" {
			t.Fatalf("closed the wrong session: %v", control["term.closed"])
		}
	})
	if pid == 0 {
		t.Fatal("shell pid not captured")
	}
	gone(t, pid)
}

func TestTerminalRefusedWhenDisabledLocally(t *testing.T) {
	terminalSession(t, Capabilities{Terminal: false}, func(ch *channel) {
		control := map[string]map[string]any{}
		ch.send(map[string]any{"t": "term.open", "sid": 1, "cols": 80, "rows": 24})
		output(t, ch, 1, "term.closed", control)
		if control["term.closed"]["reason"] != "capability_disabled" {
			t.Fatalf("unexpected: %v", control["term.closed"])
		}
	})
}

func TestLostConnectionEndsOpenShells(t *testing.T) {
	var pid int
	terminalSession(t, Capabilities{Terminal: true}, func(ch *channel) {
		open(t, ch, 3)
		termInput(ch, 3, "echo pid-$$\r")
		pid = shellPid(t, ch, 3)
	})
	gone(t, pid)
}

// A terminal that floods output stops at its window until acknowledged, and
// the connection keeps carrying reports and replies meanwhile.
func TestTerminalOutputWaitsForAcknowledgement(t *testing.T) {
	terminalSession(t, Capabilities{Terminal: true, Exec: true}, func(ch *channel) {
		open(t, ch, 5)
		termInput(ch, 5, "stty -echo; head -c 3000000 /dev/zero | tr '\\0' x; echo; echo flood-done\r")
		ch.conn.SetIdleTimeout(15 * time.Second)
		received, reports := 0, 0
		deadline := time.Now().Add(1500 * time.Millisecond)
		for time.Now().Before(deadline) || reports == 0 {
			kind, data, err := ch.read()
			if err != nil {
				t.Fatal(err)
			}
			if kind == kindTerminal && binary.BigEndian.Uint32(data[:4]) == 5 {
				received += len(data) - 4
				continue
			}
			var m map[string]any
			json.Unmarshal(data, &m)
			if m["t"] == "report" {
				reports++
			}
		}
		if received > outputWindow || received < outputWindow/2 {
			t.Fatalf("expected about one window without acks, got %d bytes", received)
		}
		if reply := call(t, ch, "ping", nil); reply["ok"] != true {
			t.Fatalf("calls must still be answered while a terminal is blocked: %v", reply)
		}
		// Acknowledging everything lets the rest through.
		var tail string
		for len(tail) < 20 || !containsDone(tail) {
			ch.send(map[string]any{"t": "term.ack", "sid": 5, "n": outputWindow})
			kind, data, err := ch.read()
			if err != nil {
				t.Fatal(err)
			}
			if kind == kindTerminal {
				received += len(data) - 4
				tail = (tail + string(data[4:]))
				if len(tail) > 64 {
					tail = tail[len(tail)-64:]
				}
			}
		}
		if received < 3000000 {
			t.Fatalf("output lost: %d", received)
		}
	})
}

func containsDone(s string) bool { return regexp.MustCompile(`flood-done`).MatchString(s) }
