package node

import (
	"bytes"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"unicode/utf8"
)

var omission = regexp.MustCompile(`\n…（省略 (\d+) 字节）…\n`)

// splitCapped returns what was kept before and after the marker and the
// number of bytes the marker says were left out.
func splitCapped(t *testing.T, s string) (string, string, int) {
	t.Helper()
	m := omission.FindAllStringSubmatchIndex(s, -1)
	if len(m) != 1 {
		t.Fatalf("want one omission marker, found %d", len(m))
	}
	n, _ := strconv.Atoi(s[m[0][2]:m[0][3]])
	return s[:m[0][0]], s[m[0][1]:], n
}

func TestCappedKeepsShortOutputWhole(t *testing.T) {
	for _, size := range []int{0, 1, execOutputHead, execOutputHead + 1, execOutputLimit} {
		var c capped
		want := bytes.Repeat([]byte("x"), size)
		for chunk := want; len(chunk) > 0; {
			k := min(len(chunk), 4093)
			c.Write(chunk[:k])
			chunk = chunk[k:]
		}
		if c.truncated() || c.String() != string(want) {
			t.Fatalf("%d bytes: truncated %v, kept %d", size, c.truncated(), len(c.String()))
		}
	}
}

// Long output keeps its start and its end, like seq 1 300000 shows its
// first and last lines, and says how much of the middle is gone.
func TestCappedKeepsHeadAndTail(t *testing.T) {
	var all bytes.Buffer
	for i := 1; i <= 300000; i++ {
		fmt.Fprintf(&all, "%d\n", i)
	}
	for _, chunk := range []int{1, 7, 4096, 100000, all.Len()} {
		var c capped
		for b := all.Bytes(); len(b) > 0; {
			k := min(chunk, len(b))
			if n, err := c.Write(b[:k]); n != k || err != nil {
				t.Fatalf("write: %d %v", n, err)
			}
			b = b[k:]
		}
		if !c.truncated() {
			t.Fatalf("chunk %d: not truncated", chunk)
		}
		head, tail, omitted := splitCapped(t, c.String())
		if len(head) != execOutputHead || len(tail) != execOutputLimit-execOutputHead {
			t.Fatalf("chunk %d: kept %d + %d", chunk, len(head), len(tail))
		}
		if !strings.HasPrefix(all.String(), head) || !strings.HasSuffix(all.String(), tail) || !strings.HasSuffix(tail, "299999\n300000\n") {
			t.Fatalf("chunk %d: head or tail is not the output's own", chunk)
		}
		if omitted != all.Len()-len(head)-len(tail) {
			t.Fatalf("chunk %d: omitted %d of %d", chunk, omitted, all.Len())
		}
	}
}

func TestCappedCutsBetweenCharacters(t *testing.T) {
	text := strings.Repeat("中文输出，", 40000) // 3-byte characters, not aligned to the limits
	var c capped
	c.Write([]byte("a"))
	c.Write([]byte(text))
	head, tail, omitted := splitCapped(t, c.String())
	if !utf8.ValidString(head) || !utf8.ValidString(tail) {
		t.Fatal("a kept side ends in half a character")
	}
	if !strings.HasPrefix("a"+text, head) || !strings.HasSuffix(text, tail) || omitted != 1+len(text)-len(head)-len(tail) {
		t.Fatalf("kept %d + %d, omitted %d", len(head), len(tail), omitted)
	}
}
