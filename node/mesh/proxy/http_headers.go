package proxy

import (
	"bufio"
	"errors"
	"io"
	"net/http"
)

const (
	maxHTTPHeaderBytes = 32 << 10
	maxHTTPHeaderLine  = 8 << 10
	maxHTTPHeaderLines = 100
)

var errHTTPHeadersLarge = errors.New("proxy request headers too large")

// This reader sits below the one bufio.Reader used throughout the handshake.
// Count only the request line and headers: after the blank line, uploads and
// CONNECT bytes pass unbounded and remain in that same buffer. SOCKS bypasses
// the HTTP budget as soon as its first byte identifies it.
type headerBudget struct {
	r                  io.Reader
	total, line, lines int
	previous           byte
	done               bool
	failed             bool
}

type proxyReader struct {
	*bufio.Reader
	budget *headerBudget
}

func newProxyReader(r io.Reader) *proxyReader {
	budget := &headerBudget{r: r}
	return &proxyReader{Reader: bufio.NewReaderSize(budget, 4096), budget: budget}
}

func (r *proxyReader) readRequest() (*http.Request, error) {
	req, err := http.ReadRequest(r.Reader)
	// net/textproto may consume an underlying read error during lookahead,
	// or replace it with a MIME syntax error. The budget failure is final
	// regardless of how the parser interpreted the truncated header.
	if r.budget.failed {
		return nil, errHTTPHeadersLarge
	}
	return req, err
}

func (r *headerBudget) Read(p []byte) (int, error) {
	if r.failed {
		return 0, errHTTPHeadersLarge
	}
	n, err := r.r.Read(p)
	for i, b := range p[:n] {
		if r.done {
			break
		}
		if r.total == 0 && b == 5 {
			r.done = true
			break
		}
		r.total++
		r.line++
		if r.total > maxHTTPHeaderBytes || r.line > maxHTTPHeaderLine {
			r.failed = true
			return i, errHTTPHeadersLarge
		}
		if b == '\n' {
			r.lines++
			if r.lines > maxHTTPHeaderLines {
				r.failed = true
				return i, errHTTPHeadersLarge
			}
			r.done = r.line == 1 || r.line == 2 && r.previous == '\r'
			r.line = 0
		}
		r.previous = b
	}
	return n, err
}
