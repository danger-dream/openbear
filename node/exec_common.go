package node

import (
	"strconv"
	"time"
	"unicode/utf8"
)

// Output beyond execOutputLimit keeps its first execOutputHead bytes and its
// last execOutputLimit-execOutputHead, with a line between them saying how
// much was left out.
const (
	execOutputLimit = 256 << 10
	execOutputHead  = 64 << 10
)

type ExecParams struct {
	Command string `json:"command"`
	Timeout int    `json:"timeout"`
	Stdin   string `json:"stdin"`
	Cwd     string `json:"cwd"`
	Shell   string `json:"shell"`
}

type ExecResult struct {
	ExitCode   int    `json:"exitCode"`
	Stdout     string `json:"stdout"`
	Stderr     string `json:"stderr"`
	Truncated  bool   `json:"truncated"`
	TimedOut   bool   `json:"timedOut"`
	DurationMs int64  `json:"durationMs"`
	Shell      string `json:"shell,omitempty"`
}

// capped is a command's stdout or stderr: the start as written, then the
// rest in a ring buffer that keeps the end.
type capped struct {
	head  []byte
	tail  []byte
	start int // the oldest byte of tail once it has wrapped
	total int64
}

func (c *capped) Write(p []byte) (int, error) {
	n := len(p)
	c.total += int64(n)
	if room := execOutputHead - len(c.head); room > 0 {
		k := min(room, len(p))
		c.head = append(c.head, p[:k]...)
		p = p[k:]
	}
	const size = execOutputLimit - execOutputHead
	if len(p) >= size {
		c.tail = append(c.tail[:0], p[len(p)-size:]...)
		c.start = 0
		return n, nil
	}
	if room := size - len(c.tail); room > 0 {
		k := min(room, len(p))
		c.tail = append(c.tail, p[:k]...)
		p = p[k:]
	}
	for len(p) > 0 {
		k := copy(c.tail[c.start:], p)
		c.start = (c.start + k) % size
		p = p[k:]
	}
	return n, nil
}

func (c *capped) truncated() bool { return c.total > execOutputLimit }

// String is everything written, or for longer output its start, the
// omission marker and its end; neither side keeps half a UTF-8 character.
func (c *capped) String() string {
	if !c.truncated() {
		return string(c.head) + string(c.tail)
	}
	head := c.head
	for i := len(head) - 1; i >= 0 && i >= len(head)-utf8.UTFMax; i-- {
		if utf8.RuneStart(head[i]) {
			if !utf8.FullRune(head[i:]) {
				head = head[:i]
			}
			break
		}
	}
	tail := append(append(make([]byte, 0, len(c.tail)), c.tail[c.start:]...), c.tail[:c.start]...)
	for i := 0; i < utf8.UTFMax-1 && len(tail) > 0 && !utf8.RuneStart(tail[0]); i++ {
		tail = tail[1:]
	}
	omitted := c.total - int64(len(head)) - int64(len(tail))
	return string(head) + "\n…（省略 " + strconv.FormatInt(omitted, 10) + " 字节）…\n" + string(tail)
}

// execGrace is how long a finished command's output is still collected
// from background processes that keep its stdout or stderr open.
const execGrace = time.Second
