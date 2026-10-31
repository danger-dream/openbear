package browser

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"strconv"
	"strings"
)

// ring is common.Ring: a bounded event log with a monotonic cursor.
type ring struct {
	limit int
	seq   int
	rows  []*Obj
}

func newRing(limit int) *ring { return &ring{limit: limit} }

func (r *ring) add(data *Obj) *Obj {
	r.seq++
	row := O("id", r.seq)
	row.Update(data)
	r.rows = append(r.rows, row)
	r.trim(r.limit)
	return row
}

// pyTail is the start index of rows[-limit:].
func pyTail(n, limit int) int {
	start := n - limit
	if limit == 0 {
		start = 0 // rows[-0:] keeps everything
	}
	if limit < 0 {
		start = -limit
	}
	return min(max(start, 0), n)
}

func (r *ring) trim(limit int) {
	r.limit = limit
	r.rows = append([]*Obj(nil), r.rows[pyTail(len(r.rows), limit):]...)
}

func (r *ring) read(after float64) *Obj {
	entries := []any{}
	for _, row := range r.rows {
		if mustNum(row.Must("id")) > after {
			entries = append(entries, row.Copy())
		}
	}
	dropped := len(r.rows) > 0 && after < mustNum(r.rows[0].Must("id"))-1
	return O("entries", entries, "cursor", r.seq, "dropped", dropped)
}

type netKey struct{ sid, rid string }

type extraKey struct {
	key  netKey
	kind string
}

// Request records keep what CDP sent about each request (headers, inline
// post data, the response; bodies stay in the browser). Every event fits
// in maxEvent, but maxEventEntries of them could still fill the
// node's memory, so records are also bounded in bytes, per page and over
// all pages, dropping the oldest first.
const (
	maxPageNetworkBytes = 32 << 20
	maxNetworkBytes     = 128 << 20
)

// jsonSize approximates the memory a parsed CDP value holds.
func jsonSize(v any) int {
	switch v := v.(type) {
	case string:
		return 16 + len(v)
	case json.Number:
		return 16 + len(v)
	case *Obj:
		n := 64
		for _, k := range v.keys {
			n += 48 + len(k) + jsonSize(v.vals[k])
		}
		return n
	case []any:
		n := 24
		for _, x := range v {
			n += 16 + jsonSize(x)
		}
		return n
	}
	return 16
}

type netRecord struct {
	key             netKey
	size            int // bytes accounted for it
	stamp           int // creation order over all pages
	request         *Obj
	row             *Obj
	response        *Obj
	done            bool
	requestHeaders  any
	responseHeaders any
	requestExtra    bool
	responseExtra   bool
	failed          bool
	redirected      bool
}

func (r *netRecord) extra(kind string) bool {
	if kind == "request" {
		return r.requestExtra
	}
	return r.responseExtra
}

func (r *netRecord) setExtra(kind string, headers any) {
	if kind == "request" {
		r.requestHeaders, r.requestExtra = headers, true
	} else {
		r.responseHeaders, r.responseExtra = headers, true
	}
}

// Observations is observations.Observations: console/network rings,
// request records, the pending dialog and the open file chooser.
type Observations struct {
	page         *Page
	console      *ring
	network      *ring
	records      *omap[int, *netRecord]
	current      map[netKey]int
	pendingExtra *omap[extraKey, []any]
	dialog       *Obj
	dialogOpened *event
	chooser      *Obj
	consoleReady map[string]bool
	// bytes is what records and pendingExtra hold; evicted is the newest
	// record id dropped for memory; pendingSince orders pendingExtra with
	// the records' stamps.
	bytes        int
	evicted      int
	pendingSince map[extraKey]int
}

func newObservations(p *Page) *Observations {
	limit := int(mustNum(p.e.config.Must("maxEventEntries")))
	return &Observations{
		page: p, console: newRing(limit), network: newRing(limit),
		records: newOmap[int, *netRecord](), current: map[netKey]int{},
		pendingExtra: newOmap[extraKey, []any](), pendingSince: map[extraKey]int{}, dialogOpened: newEvent(),
		consoleReady: map[string]bool{},
	}
}

func (o *Observations) configure(limit int) {
	o.console.trim(limit)
	o.network.trim(limit)
	for o.records.Len() > limit {
		_, record := o.records.PopFirst()
		o.bytes -= record.size
	}
	for key, rid := range o.current {
		if !o.records.Has(rid) {
			delete(o.current, key)
		}
	}
	for o.pendingExtra.Len() > limit {
		o.popPending()
	}
}

func (o *Observations) popPending() {
	ek, list := o.pendingExtra.PopFirst()
	delete(o.pendingSince, ek)
	o.bytes -= pendingSize(list)
}

func pendingSize(list []any) int {
	n := 0
	for _, headers := range list {
		n += 16 + jsonSize(headers)
	}
	return n
}

// account updates the bytes record holds after it changed.
func (o *Observations) account(record *netRecord) {
	size := 96 + jsonSize(record.request) + jsonSize(record.row)
	if record.response != nil {
		size += jsonSize(record.response)
	}
	// Headers not from ExtraInfo are those of request or response.
	if record.requestExtra {
		size += jsonSize(record.requestHeaders)
	}
	if record.responseExtra {
		size += jsonSize(record.responseHeaders)
	}
	if current, ok := o.records.Get(int(mustNum(record.row.Must("id")))); ok && current == record {
		o.bytes += size - record.size
	}
	record.size = size
}

// fit drops the oldest records and unmatched ExtraInfo headers until this
// page and all pages are within their byte limits.
func (o *Observations) fit() {
	for o.bytes > maxPageNetworkBytes && o.dropOldest() {
	}
	for {
		total, oldestStamp := 0, 0
		var oldest *Observations
		for _, page := range o.page.e.pages.Values() {
			total += page.obs.bytes
			if stamp, ok := page.obs.oldest(); ok && (oldest == nil || stamp < oldestStamp) {
				oldest, oldestStamp = page.obs, stamp
			}
		}
		if total <= maxNetworkBytes || oldest == nil || !oldest.dropOldest() {
			return
		}
	}
}

// oldest is the stamp of the page's oldest record or unmatched headers.
func (o *Observations) oldest() (int, bool) {
	stamp, ok := 0, false
	if o.records.Len() > 0 {
		stamp, ok = o.records.First().stamp, true
	}
	if o.pendingExtra.Len() > 0 {
		if since := o.pendingSince[o.pendingExtra.FirstKey()]; !ok || since < stamp {
			stamp, ok = since, true
		}
	}
	return stamp, ok
}

func (o *Observations) dropOldest() bool {
	if o.pendingExtra.Len() > 0 && (o.records.Len() == 0 || o.pendingSince[o.pendingExtra.FirstKey()] < o.records.First().stamp) {
		o.popPending()
		return true
	}
	if o.records.Len() == 0 {
		return false
	}
	id, record := o.records.PopFirst()
	o.bytes -= record.size
	o.evicted = id
	if o.current[record.key] == id {
		delete(o.current, record.key)
	}
	rows := o.network.rows
	for len(rows) > 0 && int(mustNum(rows[0].Must("id"))) <= id {
		rows = rows[1:]
	}
	o.network.rows = rows
	return true
}

func (o *Observations) event(method string, data *Obj, sid string) {
	switch {
	case method == "Console.messageAdded" && o.consoleReady[sid]:
		message := asObj(data.Must("message"))
		if pyEqual(message.Def("source", nil), "console-api") {
			o.console.add(O("level", message.Def("level", "log"), "text", pyPrefix(asStr(message.Def("text", "")), 8000)))
		}
	case method == "Runtime.bindingCalled" && pyEqual(data.Def("name", nil), o.page.errorBinding):
		payload := asStr(data.Must("payload"))
		value, err := parseJSON([]byte(pyPrefix(payload, 16384)))
		if err != nil {
			return
		}
		if v, ok := value.(*Obj); ok {
			if text, ok := v.Def("text", nil).(string); ok {
				o.console.add(O("level", "error", "text", pyPrefix(text, 8000)))
			}
		}
	case method == "Page.javascriptDialogOpening":
		o.dialog = O("type", data.Must("type"), "message", data.Must("message"),
			"defaultValue", data.Def("defaultPrompt", ""), "session", sid)
		o.dialogOpened.Set()
	case method == "Page.javascriptDialogClosed":
		o.dialog = nil
		o.dialogOpened.Clear()
	case method == "Page.fileChooserOpened":
		o.chooser = data.Copy()
		o.chooser.Set("session", sid)
	case strings.HasPrefix(method, "Network."):
		o.network_(method, data, sid)
	}
}

func (o *Observations) extra_(key netKey, kind string, headers any) {
	for _, record := range o.records.Values() {
		if record.key == key && !record.extra(kind) {
			record.setExtra(kind, headers)
			o.account(record)
			o.fit()
			return
		}
	}
	ek := extraKey{key, kind}
	list, ok := o.pendingExtra.Get(ek)
	if !ok {
		o.pendingSince[ek] = o.page.e.nextNetworkStamp()
	}
	o.pendingExtra.Set(ek, append(list, headers))
	o.bytes += pendingSize([]any{headers})
	o.configure(o.network.limit)
	o.fit()
}

func (o *Observations) network_(method string, data *Obj, sid string) {
	ridv := data.Def("requestId", nil)
	if !truthy(ridv) {
		return
	}
	rid := pyStr(ridv)
	key := netKey{sid, rid}
	page := o.page
	nav := page.navigation
	if method == "Network.loadingFailed" && nav != nil && pyEqual(nav.Def("requestId", nil), rid) &&
		sid == page.session && navActive(nav) {
		page.failNavigation(pyStr(data.Def("errorText", "network_failure")))
	}
	switch method {
	case "Network.requestWillBeSent":
		if previous, ok := o.lookup(key); ok && truthy(data.Def("redirectResponse", nil)) {
			response := asObj(data.Must("redirectResponse"))
			previous.response, previous.done, previous.redirected = response, true, true
			previous.row.Set("status", response.Must("status"))
			if !previous.responseExtra {
				previous.responseHeaders = response.Def("headers", newObj())
			}
			o.account(previous)
		}
		request := asObj(data.Must("request"))
		nav = page.navigation
		if nav != nil && navActive(nav) && sid == page.session &&
			pyEqual(data.Def("type", nil), "Document") && pyEqual(data.Def("frameId", nil), page.mainID) {
			nav.Set("requestId", rid)
			nav.Set("loaderId", data.Def("loaderId", nil))
		}
		row := o.network.add(O(
			"method", request.Must("method"),
			"url", pyPrefix(asStr(request.Must("url")), 4000),
			"resourceType", strings.ToLower(asStr(data.Def("type", "Other"))),
		))
		id := int(mustNum(row.Must("id")))
		record := &netRecord{key: key, request: request, row: row, stamp: page.e.nextNetworkStamp(),
			requestHeaders: request.Def("headers", newObj()), responseHeaders: newObj()}
		o.records.Set(id, record)
		o.current[key] = id
		o.account(record)
		for _, kind := range []string{"request", "response"} {
			ek := extraKey{key, kind}
			if waiting, ok := o.pendingExtra.Get(ek); ok && len(waiting) > 0 {
				first := waiting[0]
				o.pendingExtra.Set(ek, waiting[1:])
				o.bytes -= pendingSize([]any{first})
				o.extra_(key, kind, first)
				if rest, _ := o.pendingExtra.Get(ek); len(rest) == 0 {
					o.pendingExtra.Del(ek)
					delete(o.pendingSince, ek)
				}
			}
		}
		o.configure(o.network.limit)
		o.fit()
	case "Network.requestWillBeSentExtraInfo", "Network.responseReceivedExtraInfo":
		kind := "response"
		if strings.Contains(method, "requestWill") {
			kind = "request"
		}
		o.extra_(key, kind, data.Def("headers", newObj()))
	default:
		record, ok := o.lookup(key)
		if !ok {
			return
		}
		switch method {
		case "Network.responseReceived":
			response := asObj(data.Must("response"))
			record.response = response
			record.row.Set("status", response.Must("status"))
			if !record.responseExtra {
				record.responseHeaders = response.Def("headers", newObj())
			}
			if frame := page.frames[pyStr(data.Def("frameId", nil))]; frame != nil && data.Has("frameId") &&
				pyEqual(data.Def("type", nil), "Document") {
				page.navigationStatus.Set(lifeKey{frame.id, loaderOf(data.Def("loaderId", nil))}, response.Must("status"))
			}
			o.account(record)
			o.fit()
		case "Network.loadingFinished":
			record.done = true
		case "Network.loadingFailed":
			record.done, record.failed = true, true
			if nav != nil && pyEqual(nav.Def("requestId", nil), rid) && truthy(nav.Def("networkError", nil)) {
				record.row.Set("error", nav.Must("networkError"))
			}
		}
	}
}

func (o *Observations) lookup(key netKey) (*netRecord, bool) {
	id, ok := o.current[key]
	if !ok {
		return nil, false
	}
	return o.records.Get(id)
}

func navActive(nav *Obj) bool {
	state := nav.Must("state")
	return pyEqual(state, "requested") || pyEqual(state, "committed")
}

// loaderOf keys Python's (frameId, loaderId) tuples; None differs from "".
func loaderOf(v any) string {
	if v == nil {
		return "\x00none"
	}
	return pyStr(v)
}

// details is Observations.details: headers inline, bodies as artifacts.
func (o *Observations) details(ctx context.Context, params *Obj) (any, error) {
	e := o.page.e
	var record *netRecord
	id := 0
	if f, ok := num(params.Must("id")); ok && f == float64(int(f)) {
		id = int(f)
		record, _ = o.records.Get(id)
	}
	if record == nil && id > 0 && id <= o.evicted {
		return nil, o.evictedError(ctx)
	}
	if record == nil || record.response == nil {
		return nil, browserError(ctx, "network_entry_expired", "", nil)
	}
	sid, requestID := record.key.sid, record.key.rid
	part := asStr(params.Must("part"))
	response := record.response
	if strings.HasSuffix(part, "headers") {
		headers := record.responseHeaders
		if part == "request-headers" {
			headers = record.requestHeaders
		}
		out := newObj()
		h := asObj(headers)
		for _, k := range h.Keys() {
			v, _ := h.Get(k)
			out.Set(strings.ToLower(k), pyStr(v))
		}
		return O("url", response.Must("url"), "statusCode", response.Must("status"), "headers", out), nil
	}
	limit := int64(mustNum(e.config.Must("maxBodyBytes")))
	var body []byte
	if part == "request-body" {
		entries := record.request.Def("postDataEntries", nil)
		allBytes := truthy(entries)
		if allBytes {
			for _, x := range asList(entries) {
				if !asObj(x).Has("bytes") {
					allBytes = false
					break
				}
			}
		}
		switch {
		case allBytes:
			for _, x := range asList(entries) {
				chunk, err := b64decode(asStr(asObj(x).Must("bytes")))
				if err != nil {
					return nil, err
				}
				body = append(body, chunk...)
			}
		case record.request.Has("postData"):
			body = []byte(asStr(record.request.Must("postData")))
		case truthy(record.request.Def("hasPostData", nil)):
			if record.redirected {
				return nil, browserError(ctx, "network_body_unavailable", "", nil)
			}
			result, err := e.call(withArtifact(ctx), "Network.getRequestPostData", O("requestId", requestID), sid)
			if err != nil {
				return nil, err
			}
			body = []byte(asStr(result.Must("postData")))
		default:
			body = []byte(asStr(record.request.Def("postData", "")))
		}
	} else {
		// CDP reuses a requestId for redirects; an earlier hop never borrows
		// the final response's bytes.
		if record.redirected {
			return nil, browserError(ctx, "network_body_unavailable", "", nil)
		}
		h := asObj(record.responseHeaders)
		for _, k := range h.Keys() {
			if strings.ToLower(k) == "content-length" {
				v, _ := h.Get(k)
				if s := pyStr(v); isDigits(s) && digitsGreater(s, limit) {
					return nil, browserError(ctx, "network_body_too_large", "", nil)
				}
				break
			}
		}
		for !record.done {
			if err := e.retryPause(ctx); err != nil {
				return nil, err
			}
			// Dropped records see no more events.
			if current, _ := o.records.Get(id); current != record {
				return nil, o.evictedError(ctx)
			}
		}
		if record.failed {
			return nil, browserError(ctx, "network_body_unavailable", "", nil)
		}
		result, err := e.call(withArtifact(ctx), "Network.getResponseBody", O("requestId", requestID), sid)
		if err != nil {
			return nil, err
		}
		if truthy(result.Def("base64Encoded", nil)) {
			body, err = b64decode(asStr(result.Must("body")))
			if err != nil {
				return nil, err
			}
		} else {
			body = []byte(asStr(result.Must("body")))
		}
	}
	if int64(len(body)) > limit {
		return nil, browserError(ctx, "network_body_too_large", "", nil)
	}
	return e.artifact(ctx, asStr(params.Must("outputPath")), body)
}

func (o *Observations) evictedError(ctx context.Context) error {
	return browserError(ctx, "network_entry_evicted", "", map[string]any{
		"pageLimitBytes": maxPageNetworkBytes, "totalLimitBytes": maxNetworkBytes})
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

func digitsGreater(s string, limit int64) bool {
	s = strings.TrimLeft(s, "0")
	if len(s) > 18 {
		return true
	}
	if s == "" {
		return 0 > limit
	}
	n, _ := strconv.ParseInt(s, 10, 64)
	return n > limit
}

// b64decode is base64.b64decode (non-alphabet characters are discarded).
func b64decode(s string) ([]byte, error) {
	if out, err := base64.StdEncoding.DecodeString(s); err == nil {
		return out, nil
	}
	clean := strings.Map(func(r rune) rune {
		if r >= 'A' && r <= 'Z' || r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '+' || r == '/' || r == '=' {
			return r
		}
		return -1
	}, s)
	return base64.StdEncoding.DecodeString(clean)
}

// omap is an insertion-ordered map (Python dict / OrderedDict).
type omap[K comparable, V any] struct {
	keys []K
	m    map[K]V
}

func newOmap[K comparable, V any]() *omap[K, V] { return &omap[K, V]{m: map[K]V{}} }

func (o *omap[K, V]) Get(k K) (V, bool) { v, ok := o.m[k]; return v, ok }
func (o *omap[K, V]) Has(k K) bool      { _, ok := o.m[k]; return ok }
func (o *omap[K, V]) Len() int          { return len(o.m) }

func (o *omap[K, V]) Set(k K, v V) {
	if _, ok := o.m[k]; !ok {
		o.keys = append(o.keys, k)
	}
	o.m[k] = v
}

func (o *omap[K, V]) Del(k K) {
	if _, ok := o.m[k]; !ok {
		return
	}
	delete(o.m, k)
	for i, key := range o.keys {
		if key == k {
			o.keys = append(o.keys[:i:i], o.keys[i+1:]...)
			return
		}
	}
}

func (o *omap[K, V]) PopFirst() (K, V) {
	k := o.keys[0]
	v := o.m[k]
	o.keys = o.keys[1:]
	delete(o.m, k)
	return k, v
}

func (o *omap[K, V]) First() V    { return o.m[o.keys[0]] }
func (o *omap[K, V]) FirstKey() K { return o.keys[0] }

func (o *omap[K, V]) Keys() []K { return append([]K(nil), o.keys...) }

func (o *omap[K, V]) Values() []V {
	out := make([]V, 0, len(o.keys))
	for _, k := range o.keys {
		out = append(out, o.m[k])
	}
	return out
}
