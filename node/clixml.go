package node

import (
	"bytes"
	"encoding/xml"
	"html"
	"io"
	"regexp"
	"strconv"
	"strings"
)

const clixmlHeader = "#< CLIXML"

var (
	cliXMLStream = regexp.MustCompile(`^<S S="(\w+)">([^<]*)</S>`)
	cliXMLEscape = regexp.MustCompile(`_x([0-9A-Fa-f]{4})_`)
)

func plainErrors(s string) string {
	var out strings.Builder
	w := clixmlWriter{out: &out}
	w.Write([]byte(s))
	w.flush()
	return out.String()
}

// clixmlText is one CLIXML stream record as a console shows it.
func clixmlText(stream, raw string) string {
	text := cliXMLEscape.ReplaceAllStringFunc(html.UnescapeString(raw), func(e string) string {
		n, _ := strconv.ParseUint(e[2:6], 16, 16)
		return string(rune(n))
	})
	if stream != "Error" {
		text = strings.ToUpper(stream) + ": " + text
	}
	return text
}

type clixmlWriter struct {
	out     io.Writer
	buf     []byte
	xml     bool
	records bool
}

// PowerShell's CLIXML and native programs' plain stderr share the same pipe.
// Only recognized serialization records are removed or decoded; other bytes stay.
func (w *clixmlWriter) Write(p []byte) (int, error) {
	w.buf = append(w.buf, p...)
	w.drain(false)
	return len(p), nil
}

func (w *clixmlWriter) drain(final bool) {
	for len(w.buf) > 0 {
		b := w.buf
		if bytes.HasPrefix(b, []byte(clixmlHeader)) {
			n := len(clixmlHeader)
			if len(b) == n && !final {
				return
			}
			if len(b) > n && b[n] == '\r' {
				n++
				if len(b) == n && !final {
					return
				}
			}
			if len(b) > n && b[n] == '\n' {
				n++
			}
			w.xml = true
			w.buf = b[n:]
			continue
		}
		if !final && bytes.HasPrefix([]byte(clixmlHeader), b) {
			return
		}
		if w.xml && b[0] == '<' {
			prefixes := []string{`<S S="`, "<Objs ", "<Objs>", "</Objs>", `<Obj S="progress"`}
			partial := false
			for _, prefix := range prefixes {
				if len(b) < len(prefix) && bytes.HasPrefix([]byte(prefix), b) {
					partial = true
					break
				}
			}
			if partial && !final {
				return
			}
			n := 0
			switch {
			case w.records && bytes.HasPrefix(b, []byte(`<S S="`)):
				end := bytes.Index(b, []byte("</S>"))
				if end < 0 && !final && len(b) <= 1<<20 {
					return
				}
				if end >= 0 {
					n = end + len("</S>")
					if m := cliXMLStream.FindSubmatch(b[:n]); m != nil {
						io.WriteString(w.out, clixmlText(string(m[1]), string(m[2])))
					} else {
						w.out.Write(b[:n])
					}
				}
			case bytes.HasPrefix(b, []byte("<Objs ")), bytes.HasPrefix(b, []byte("<Objs>")):
				end := bytes.IndexByte(b, '>')
				if end < 0 && !final && len(b) <= 1<<20 {
					return
				}
				if end >= 0 {
					n = end + 1
					w.records = true
				}
			case w.records && bytes.HasPrefix(b, []byte("</Objs>")):
				n = len("</Objs>")
				w.xml, w.records = false, false
			case w.records && bytes.HasPrefix(b, []byte(`<Obj S="progress"`)):
				decoder := xml.NewDecoder(bytes.NewReader(b))
				var value struct{}
				err := decoder.Decode(&value)
				if err == nil {
					n = int(decoder.InputOffset())
				} else if !final && len(b) <= 1<<20 {
					if err == io.EOF || strings.Contains(err.Error(), "unexpected EOF") {
						return
					}
				}
			}
			if n > 0 {
				w.buf = b[n:]
				continue
			}
		}
		n := len(b)
		for _, token := range []string{clixmlHeader, "<"} {
			if token == "<" && !w.xml {
				continue
			}
			if i := bytes.Index(b[1:], []byte(token)); i >= 0 {
				n = min(n, i+1)
			}
		}
		if !final {
			for size := min(n, len(clixmlHeader)-1); size > 0; size-- {
				if bytes.HasSuffix(b[:n], []byte(clixmlHeader[:size])) {
					n -= size
					break
				}
			}
		}
		w.out.Write(b[:n])
		w.buf = b[n:]
	}
	w.buf = nil
}

func (w *clixmlWriter) flush() { w.drain(true) }
