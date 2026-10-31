package browser

// CDP multiplexer over one Transport (Python transport.Connection).

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

type reply struct {
	result *Obj
	err    error
}

type conn struct {
	t       Transport
	timeout time.Duration

	mu      sync.Mutex
	seq     int64
	pending map[int64]chan reply
	large   map[int64]bool // pending calls whose reply may reach maxMessage
	done    chan struct{}  // closed when the reader stops
	onEvent func(method string, params *Obj, session string)
}

func newConn(t Transport, timeout time.Duration, onEvent func(string, *Obj, string)) *conn {
	c := &conn{t: t, timeout: timeout, pending: map[int64]chan reply{}, large: map[int64]bool{}, done: make(chan struct{}), onEvent: onEvent}
	if s, ok := t.(sized); ok {
		s.setSizer(c.ceiling)
	}
	go c.read()
	return c
}

type artifactKey struct{}

// withArtifact marks calls made with ctx as fetching an artifact (a
// screenshot, an evaluated value, a response body): only their replies may
// grow to maxMessage.
func withArtifact(ctx context.Context) context.Context {
	return context.WithValue(ctx, artifactKey{}, true)
}

// Chrome writes "id" first in every reply and never in an event.
var replyHead = regexp.MustCompile(`^\s*\{\s*"id"\s*:\s*(\d{1,15})\s*[,}]`)

func replyID(head []byte) (int64, bool) {
	m := replyHead.FindSubmatch(head)
	if m == nil {
		return 0, false
	}
	id, err := strconv.ParseInt(string(m[1]), 10, 64)
	return id, err == nil
}

// ceiling bounds an incoming message by what it is. Events come unasked and
// a page sets their size, so they get the small bound.
func (c *conn) ceiling(head []byte) int {
	id, ok := replyID(head)
	if !ok {
		return maxEvent
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.large[id] {
		return maxMessage
	}
	return maxReply
}

// dropped fails the call whose reply was discarded as too large.
func (c *conn) dropped(head []byte) {
	id, ok := replyID(head)
	if !ok {
		return
	}
	c.mu.Lock()
	ch := c.pending[id]
	delete(c.pending, id)
	c.mu.Unlock()
	if ch != nil {
		ch <- reply{err: protoErr("cdp_message_too_large")}
	}
}

func (c *conn) connected() bool {
	select {
	case <-c.done:
		return false
	default:
		return true
	}
}

func (c *conn) read() {
	defer func() {
		recover() // a failing handler stops the reader like Python's _read
		c.mu.Lock()
		close(c.done)
		pending := c.pending
		c.pending = map[int64]chan reply{}
		c.mu.Unlock()
		for _, ch := range pending {
			select {
			case ch <- reply{err: ErrDisconnected}:
			default:
			}
		}
	}()
	for {
		raw, err := c.t.Receive()
		var dropped *droppedMessage
		if errors.As(err, &dropped) {
			c.dropped(dropped.head)
			continue
		}
		if err != nil {
			return
		}
		value, err := parseJSON(raw)
		if err != nil {
			return
		}
		data, ok := value.(*Obj)
		if !ok {
			return
		}
		if id, ok := data.Get("id"); ok {
			f, isNum := num(id)
			if !isNum {
				continue
			}
			c.mu.Lock()
			ch := c.pending[int64(f)]
			delete(c.pending, int64(f))
			c.mu.Unlock()
			if ch == nil {
				continue
			}
			if e, ok := data.Get("error"); ok {
				msg := "cdp_error"
				if eo, ok := e.(*Obj); ok {
					if m, ok := eo.Get("message"); ok {
						msg = pyStr(m)
					}
				} else {
					panic(typeError("error"))
				}
				ch <- reply{err: protoErr(msg)}
			} else {
				result, _ := data.Def("result", newObj()).(*Obj)
				if result == nil {
					result = newObj()
				}
				ch <- reply{result: result}
			}
		} else if m, ok := data.Get("method"); ok {
			method, _ := m.(string)
			params, _ := data.Def("params", newObj()).(*Obj)
			session, _ := data.Get("sessionId")
			sid, _ := session.(string)
			// Handlers only update state and schedule work.
			c.onEvent(method, params, sid)
		}
	}
}

// call sends one command and waits up to timeout (and ctx) for its reply.
// The command is never resent.
func (c *conn) call(ctx context.Context, method string, params any, session string, timeout time.Duration) (*Obj, error) {
	if !c.connected() {
		return nil, ErrDisconnected
	}
	if params == nil {
		params = newObj()
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	ch := make(chan reply, 1)
	c.mu.Lock()
	c.seq++
	id := c.seq
	c.pending[id] = ch
	if ctx.Value(artifactKey{}) != nil {
		c.large[id] = true
	}
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		delete(c.pending, id)
		delete(c.large, id)
		c.mu.Unlock()
	}()
	payload := O("id", id, "method", method, "params", params)
	if session != "" {
		payload.Set("sessionId", session)
	}
	body, err := dumps(payload)
	if err != nil {
		return nil, err
	}
	if err := c.t.Send(ctx, body); err != nil {
		if ctx.Err() != nil {
			return nil, ctxErr(ctx)
		}
		return nil, ErrDisconnected
	}
	select {
	case r := <-ch:
		return r.result, r.err
	case <-ctx.Done():
		select {
		case r := <-ch:
			return r.result, r.err
		default:
		}
		return nil, ctxErr(ctx)
	case <-c.done:
		select {
		case r := <-ch:
			return r.result, r.err
		default:
		}
		return nil, ErrDisconnected
	}
}

func (c *conn) close() {
	c.t.Close()
	<-c.done
}

// resolveEndpoint follows Python Connection.connect: an http(s) endpoint is
// resolved through /json/version (at most 64KiB, no proxy).
func resolveEndpoint(ctx context.Context, endpoint string) (string, error) {
	if strings.HasPrefix(endpoint, "ws://") || strings.HasPrefix(endpoint, "wss://") {
		return endpoint, nil
	}
	u, err := url.Parse(endpoint)
	if err != nil {
		return "", err
	}
	path := strings.TrimRight(u.Path, "/")
	if !strings.HasSuffix(path, "/json/version") {
		path += "/json/version"
	}
	u.Path, u.RawPath, u.Fragment = path, "", ""
	client := &http.Client{Transport: &http.Transport{Proxy: nil}}
	defer client.CloseIdleConnections()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return "", err
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return "", errors.New("cdp version request failed")
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 65537))
	if err != nil {
		return "", err
	}
	if len(body) > 65536 {
		return "", errors.New("cdp_version_too_large")
	}
	var version struct {
		URL *string `json:"webSocketDebuggerUrl"`
	}
	if err := json.Unmarshal(body, &version); err != nil || version.URL == nil {
		return "", errors.New("invalid cdp version response")
	}
	return *version.URL, nil
}
