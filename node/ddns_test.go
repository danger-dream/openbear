package node

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"testing"
	"time"
)

func TestDDNSParseFindsTheFirstUsableAddress(t *testing.T) {
	trace := "fl=29f1\nh=www.cloudflare.com\nip=203.0.113.24\nts=1793250000.123\nvisit_scheme=https\nuag=curl/8.4.0\ncolo=SJC\n"
	for _, c := range []struct {
		text string
		v6   bool
		want string
	}{
		{"203.0.113.24\n", false, "203.0.113.24"},
		{trace, false, "203.0.113.24"},
		{"fl=1\nip=2001:db8::24\nts=1\n", true, "2001:db8::24"},
		{`{"ip":"198.51.100.7","country":"SG"}`, false, "198.51.100.7"},
		{`<html><body>Your IP: 198.51.100.8</body></html>`, false, "198.51.100.8"},
		{"ip:198.51.100.9", false, "198.51.100.9"},
		{"  2001:DB8::1  ", true, "2001:db8::1"},
		{"127.0.0.1 then 0.0.0.0 then 198.51.100.10", false, "198.51.100.10"},
		{"fe80::1 2001:db8::9", true, "2001:db8::9"},
		{"::ffff:198.51.100.11", false, ""},
		{"::ffff:198.51.100.11", true, ""},
		{"198.51.100.12", true, ""},
		{"2001:db8::12", false, ""},
		{"Date: Thu, 29 Oct 2026 06:18:30 GMT", false, ""},
		{"", false, ""},
	} {
		got, ok := ddnsParse(c.text, c.v6)
		if c.want == "" && ok || c.want != "" && (!ok || got.String() != c.want) {
			t.Errorf("ddnsParse(%q, v6=%v) = %v %v, want %q", c.text, c.v6, got, ok, c.want)
		}
	}
}

// Answers of the services in China the controller asks, as they came on 2026-10-29.
func TestDDNSParseReadsTheCallerFromJSONAnswers(t *testing.T) {
	for _, c := range []struct{ name, text string }{
		{"ipuu", `{"code":200,"data":"192.0.2.34","msg":"获取用户端IP成功"}`},
		{"upyun", `{"addr":"36.150.185.19","server":"marco\/3.2.5","server_time":"2026-10-29 07:50:41","remote_addr":"192.0.2.34",` +
			`"addr_location":{"country":"中国","isp":"移动"},"hostname":"cmn-js-czx4-019","remote_addr_location":{"country":"中国","isp":"电信"}}`},
		{"domob", `{"ip":"192.0.2.34","country_code":"CN","region":"Yunnan","lat":25.0389,"lon":102.7183,"processing_time":"21.974µs"}`},
		{"iqiyi", `{"t":"CT|YunNan_KunMing-192.0.2.34","mrc":"3","z":"baiducdn_ct","time":"1793260241","v":"2026-09-17 v26.37.1",` +
			`"l":"https://bdcdnct.inter.71edge.com/v.f4v?uuid=70739022-1a186a3c446-0a861337&src=iqiyi.com&z=87605104&cphc=arta"}`},
		{"cz88", `{"code":200,"success":true,"message":"操作成功","data":{"ip":"192.0.2.34","geocode":"156053001002","country":"中国",` +
			`"geoCenter":{"latitude":"25.258602","longitude":"102.641539"}},"time":"2026-10-29 15:57:31"}`},
		{"ipip", "当前 IP：192.0.2.34  来自于：中国 云南 昆明  电信\n"},
		{"oray", "Current IP Address: 192.0.2.34"},
		{"yinghualuo", `{"is_ipv6":false,"ip":"192.0.2.34","location":"云南省昆明市 电信","area":"云南省昆明市","isp":"电信"}`},
	} {
		if got, ok := ddnsParse(c.text, false); !ok || got.String() != "192.0.2.34" {
			t.Errorf("%s: got %v %v", c.name, got, ok)
		}
	}
}

func TestPickPublicV4KeepsTheLineAndTheProxyExit(t *testing.T) {
	for _, c := range []struct{ china, abroad, v4, proxy string }{
		{"192.0.2.34", "192.0.2.92", "192.0.2.34", "192.0.2.92"},
		{"203.0.113.5", "203.0.113.5", "203.0.113.5", ""},
		{"", "203.0.113.5", "203.0.113.5", ""},
		{"192.0.2.34", "", "192.0.2.34", ""},
		{"", "", "", ""},
	} {
		if v4, proxy := pickPublicV4(c.china, c.abroad); v4 != c.v4 || proxy != c.proxy {
			t.Errorf("pickPublicV4(%q, %q) = %q, %q", c.china, c.abroad, v4, proxy)
		}
	}
}

func unhex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(strings.Join(strings.Fields(s), ""))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// The sample responses of RFC 5769, sections 2.2 and 2.3.
const (
	stunSampleTx   = "b7e7a701 bc34d686 fa87dfae"
	stunSampleIPv4 = `0101003c 2112a442 b7e7a701 bc34d686 fa87dfae 8022000b 74657374 20766563 746f7220
		00200008 0001a147 e112a643 00080014 2b91f599 fd9e90c3 8c7489f9 2af9ba53 f06be7d7 80280004 c07d4c96`
	stunSampleIPv6 = `01010048 2112a442 b7e7a701 bc34d686 fa87dfae 8022000b 74657374 20766563 746f7220
		00200014 0002a147 0113a9fa a5d3f179 bc25f4b5 bed2b9d9 00080014 a382954e 4be67bf1 1784c97c 8292c275 bfe3ed41
		80280004 c8fb0b4c`
)

func TestSTUNCodec(t *testing.T) {
	var tx [12]byte
	copy(tx[:], unhex(t, stunSampleTx))
	if got := hex.EncodeToString(stunRequest(tx)); got != "000100002112a442"+strings.ReplaceAll(stunSampleTx, " ", "") {
		t.Fatalf("binding request %s", got)
	}
	for _, c := range []struct{ msg, want string }{
		{stunSampleIPv4, "192.0.2.1:32853"},
		{stunSampleIPv6, "[2001:db8:1234:5678:11:2233:4455:6677]:32853"},
		// MAPPED-ADDRESS alone: 198.51.100.5:3478, unxored.
		{"0101000c 2112a442 " + stunSampleTx + " 00010008 00010d96 c6336405", "198.51.100.5:3478"},
		// Both: XOR-MAPPED-ADDRESS wins.
		{"01010018 2112a442 " + stunSampleTx + " 00010008 00010d96 c6336405 00200008 0001a147 e112a643", "192.0.2.1:32853"},
	} {
		got, err := stunParse(unhex(t, c.msg), tx)
		if err != nil || got.String() != c.want {
			t.Errorf("stunParse = %v %v, want %s", got, err, c.want)
		}
	}
	other := tx
	other[0] ^= 1
	if _, err := stunParse(unhex(t, stunSampleIPv4), other); !errors.Is(err, errSTUNForeign) {
		t.Fatalf("another transaction's answer must be skipped: %v", err)
	}
	// Binding error response with ERROR-CODE 401.
	failed := unhex(t, "01110008 2112a442 "+stunSampleTx+" 00090004 00000401")
	if _, err := stunParse(failed, tx); err == nil || err.Error() != "http_error" {
		t.Fatalf("error response: %v", err)
	}
	if _, err := stunParse(unhex(t, "01010004 2112a442 "+stunSampleTx+" 8022"), tx); err == nil || err.Error() != "parse_error" {
		t.Fatalf("truncated message: %v", err)
	}
}

var errFlaky = ddnsFail("timeout", "")

func TestDDNSTrackerConfirmsChangesAndCountsFailures(t *testing.T) {
	type step struct {
		value string
		err   error
		want  string // "" none, "=v" value v, "!code" error code
	}
	cases := map[string]struct {
		confirm int
		steps   []step
	}{
		"first value at once, a change after confirm reads": {2, []step{
			{"a", nil, "=a"}, {"a", nil, ""}, {"b", nil, ""}, {"b", nil, "=b"}, {"b", nil, ""},
		}},
		"a flap back resets the count": {2, []step{
			{"a", nil, "=a"}, {"b", nil, ""}, {"a", nil, ""}, {"b", nil, ""}, {"c", nil, ""}, {"c", nil, "=c"},
		}},
		"confirm 1 reports every change": {1, []step{{"a", nil, "=a"}, {"b", nil, "=b"}, {"b", nil, ""}, {"a", nil, "=a"}}},
		"a failure breaks the run": {3, []step{
			{"a", nil, "=a"}, {"b", nil, ""}, {"b", nil, ""}, {"", errFlaky, ""}, {"b", nil, ""}, {"b", nil, ""}, {"b", nil, "=b"},
		}},
		"the third failure is reported once, then the recovery": {2, []step{
			{"a", nil, "=a"}, {"", errFlaky, ""}, {"", errFlaky, ""}, {"", errFlaky, "!timeout"}, {"", errFlaky, ""},
			{"a", nil, "=a"}, {"a", nil, ""},
		}},
		"recovering with a new value reports it": {3, []step{
			{"a", nil, "=a"}, {"", errFlaky, ""}, {"", errFlaky, ""}, {"", errFlaky, "!timeout"}, {"b", nil, "=b"},
		}},
		"failures before any value": {2, []step{
			{"", errFlaky, ""}, {"", errFlaky, ""}, {"", errFlaky, "!timeout"}, {"a", nil, "=a"},
		}},
		"two failures are not reported": {2, []step{{"a", nil, "=a"}, {"", errFlaky, ""}, {"", errFlaky, ""}, {"a", nil, ""}}},
		"no consent is reported at once": {2, []step{
			{"", ddnsFail("capability_disabled", ""), "!capability_disabled"}, {"", ddnsFail("capability_disabled", ""), ""},
		}},
	}
	for name, c := range cases {
		tr := ddnsTracker{confirm: c.confirm}
		for i, s := range c.steps {
			o, ok := tr.observe(s.value, "", s.err)
			got := ""
			switch {
			case ok && o.Error != "":
				got = "!" + o.Error
			case ok:
				got = "=" + o.Value
			}
			if got != s.want {
				t.Fatalf("%s: step %d got %q, want %q", name, i, got, s.want)
			}
			if ok && o.Error != "" && o.Fails != ddnsFailLimit && o.Error == "timeout" {
				t.Fatalf("%s: error report counts %d failures", name, o.Fails)
			}
		}
	}
}

type fakeWait struct {
	d time.Duration
	c chan time.Time
}

type fakeClock struct{ waits chan fakeWait }

func newFakeClock() *fakeClock { return &fakeClock{waits: make(chan fakeWait, 64)} }

func (f *fakeClock) after(d time.Duration) <-chan time.Time {
	c := make(chan time.Time, 1)
	f.waits <- fakeWait{d, c}
	return c
}

type fakeRead struct {
	value string
	err   error
}

func TestDDNSRunReadsOnItsOwnJitteredClock(t *testing.T) {
	clock := newFakeClock()
	w := &ddnsWatcher{after: clock.after}
	reads := make(chan fakeRead)
	out := make(chan ddnsObs)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	read := func(ctx context.Context, s ddnsSpec) (string, string, error) {
		select {
		case r := <-reads:
			return r.value, "probe.example", r.err
		case <-ctx.Done():
			return "", "", ctx.Err()
		}
	}
	spec := ddnsSpec{key: "p_1", family: "v4", method: "url", interval: time.Minute, confirm: 2}
	done := make(chan struct{})
	go func() { w.run(ctx, spec, read, out); close(done) }()
	// One round: feed a reading, then collect what it reported until the
	// probe asks for its next wait.
	round := func(r fakeRead) []ddnsObs {
		t.Helper()
		reads <- r
		var got []ddnsObs
		for {
			select {
			case o := <-out:
				got = append(got, o)
			case wait := <-clock.waits:
				if wait.d < 54*time.Second || wait.d > 66*time.Second {
					t.Fatalf("wait %s is not a minute ±10%%", wait.d)
				}
				wait.c <- time.Time{}
				return got
			case <-time.After(5 * time.Second):
				t.Fatal("the probe stalled")
			}
		}
	}
	got := round(fakeRead{"203.0.113.1", nil})
	if len(got) != 1 || got[0].Value != "203.0.113.1" || got[0].Key != "p_1" || got[0].Family != "v4" || got[0].Method != "url" ||
		got[0].Detail != "probe.example" || got[0].At == 0 || got[0].Fails != 0 {
		t.Fatalf("first reading: %+v", got)
	}
	if got = round(fakeRead{"203.0.113.2", nil}); len(got) != 0 {
		t.Fatalf("an unconfirmed change was reported: %+v", got)
	}
	if got = round(fakeRead{"203.0.113.2", nil}); len(got) != 1 || got[0].Value != "203.0.113.2" {
		t.Fatalf("the confirmed change: %+v", got)
	}
	for i := 1; i < ddnsFailLimit; i++ {
		if got = round(fakeRead{"", errFlaky}); len(got) != 0 {
			t.Fatalf("failure %d was reported: %+v", i, got)
		}
	}
	if got = round(fakeRead{"", errFlaky}); len(got) != 1 || got[0].Error != "timeout" || got[0].Value != "" || got[0].Fails != ddnsFailLimit {
		t.Fatalf("the failure report: %+v", got)
	}
	if got = round(fakeRead{"", errFlaky}); len(got) != 0 {
		t.Fatalf("a failure was reported twice: %+v", got)
	}
	if got = round(fakeRead{"203.0.113.2", nil}); len(got) != 1 || got[0].Error != "" || got[0].Value != "203.0.113.2" || got[0].Fails != 0 {
		t.Fatalf("the recovery: %+v", got)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("the probe outlived its context")
	}
}

func TestDDNSWatchReplacesAndStopsProbes(t *testing.T) {
	clock := newFakeClock()
	w := &ddnsWatcher{after: clock.after}
	sent := make(chan []ddnsObs, 16)
	send := func(obs []ddnsObs) error { sent <- obs; return nil }
	reads := make(chan string, 64)
	read := func(_ context.Context, s ddnsSpec) (string, string, error) {
		reads <- s.key
		return "198.51.100.1", "", nil
	}
	collect := func(n int) map[string]ddnsObs {
		t.Helper()
		got := map[string]ddnsObs{}
		for len(got) < n {
			select {
			case batch := <-sent:
				for _, o := range batch {
					got[o.Key] = o
				}
			case <-time.After(5 * time.Second):
				t.Fatalf("got %d of %d reports", len(got), n)
			}
		}
		return got
	}
	drain := func() []string {
		var keys []string
		for {
			select {
			case k := <-reads:
				keys = append(keys, k)
			case <-time.After(100 * time.Millisecond):
				return keys
			}
		}
	}
	session, end := context.WithCancel(context.Background())
	specs := []ddnsSpec{
		{key: "a", family: "v4", method: "url", interval: time.Minute, confirm: 2},
		{key: "b", family: "v6", method: "nic", interval: 15 * time.Second, confirm: 1},
	}
	w.watch(session, specs, read, send)
	if got := collect(2); got["a"].Value != "198.51.100.1" || got["b"].Family != "v6" || got["b"].Method != "nic" {
		t.Fatalf("first round: %+v", got)
	}
	first := []fakeWait{<-clock.waits, <-clock.waits}
	drain()
	w.watch(session, specs[1:], read, send)
	if got := collect(1); got["b"].Key != "b" {
		t.Fatalf("a new watch starts with a fresh round: %+v", got)
	}
	if keys := drain(); len(keys) != 1 || keys[0] != "b" {
		t.Fatalf("the new watch read %v", keys)
	}
	last := <-clock.waits
	for _, wait := range first {
		wait.c <- time.Time{}
	}
	if keys := drain(); len(keys) != 0 {
		t.Fatalf("replaced probes still read: %v", keys)
	}
	end()
	last.c <- time.Time{}
	if keys := drain(); len(keys) != 0 {
		t.Fatalf("a probe read after its session ended: %v", keys)
	}
	select {
	case wait := <-clock.waits:
		t.Fatalf("a probe waited again after its session ended: %v", wait.d)
	default:
	}
}

func TestDDNSJitterStaysWithinTenPercent(t *testing.T) {
	for range 1000 {
		if d := ddnsJitter(30 * time.Second); d < 27*time.Second || d > 33*time.Second {
			t.Fatalf("jitter %s", d)
		}
	}
}

func TestDDNSPickV6(t *testing.T) {
	c := func(s string, temporary, deprecated bool) ddnsCandidate {
		return ddnsCandidate{netip.MustParseAddr(s), temporary, deprecated}
	}
	list := []ddnsCandidate{
		c("fe80::1", false, false),
		c("2001:db8:1::aaaa", true, false),
		c("2001:db8:1::1", false, true),
		c("fd00::1", false, false),
		c("2001:db8:1::2", false, false),
		c("240e:390::2", false, false),
		c("::ffff:198.51.100.1", false, false),
	}
	sel := func(mode, value string, temporary bool) ddnsSelect {
		s, err := ddnsParseSelect(&ddnsSelectIn{Mode: mode, Value: value, AllowTemporary: temporary})
		if err != nil {
			t.Fatalf("%s %q: %v", mode, value, err)
		}
		return s
	}
	for _, x := range []struct {
		sel          ddnsSelect
		allowPrivate bool
		want         string
	}{
		{sel("first", "", false), false, "2001:db8:1::2"},
		{sel("", "", true), false, "2001:db8:1::aaaa"},
		{sel("first", "", false), true, "fd00::1"},
		{sel("index", "1", false), false, "2001:db8:1::2"},
		{sel("index", "2", false), false, "240e:390::2"},
		{sel("index", "3", false), false, ""},
		{sel("index", "3", false), true, "240e:390::2"},
		{sel("prefix", "240e:", false), false, "240e:390::2"},
		{sel("prefix", "240E:390::/32", false), false, "240e:390::2"},
		{sel("prefix", "2001:db8:2::/48", false), false, ""},
		{sel("regex", `::2$`, false), false, "2001:db8:1::2"},
		{sel("regex", `^240e`, false), false, "240e:390::2"},
		{sel("regex", `aaaa`, false), false, ""},
		{sel("regex", `aaaa`, true), false, "2001:db8:1::aaaa"},
	} {
		got, ok := ddnsPickV6(list, x.sel, x.allowPrivate)
		if x.want == "" && ok || x.want != "" && got.String() != x.want {
			t.Errorf("%+v private=%v: got %v %v, want %q", x.sel, x.allowPrivate, got, ok, x.want)
		}
	}
	for _, bad := range []ddnsSelectIn{{Mode: "last"}, {Mode: "index", Value: "0"}, {Mode: "index", Value: "x"},
		{Mode: "prefix"}, {Mode: "regex"}, {Mode: "regex", Value: "("}} {
		if _, err := ddnsParseSelect(&bad); err == nil {
			t.Errorf("%+v must be refused", bad)
		}
	}
}

func TestDDNSWatchValidatesItsProbes(t *testing.T) {
	good := `{"key":"p_1","family":"v4","method":"url","intervalS":60,"confirm":2,"urls":["https://api.ipify.org"],
		"v6Select":{"mode":"first","value":"","allowTemporary":false}}`
	specs, err := parseDDNSWatch(json.RawMessage(`{"probes":[` + good + `,
		{"key":"p_2","family":"v6","method":"nic","intervalS":10,"confirm":5,"iface":"eth0","allowPrivate":true},
		{"key":"p_3","family":"v4","method":"stun","intervalS":3600,"confirm":1,"stun":["stun.cloudflare.com","stun:stun.l.google.com:19302","[2001:db8::1]:3478"]},
		{"key":"p_4","family":"v4","method":"cmd","intervalS":30,"confirm":1,"cmd":"curl -s ifconfig.me"},
		{"key":"p_5","family":"v4","method":"url"}]}`))
	if err != nil || len(specs) != 5 {
		t.Fatalf("valid probes refused: %v", err)
	}
	if specs[0].interval != time.Minute || specs[1].interval != 10*time.Second || specs[1].confirm != 5 || !specs[1].allowPrivate {
		t.Fatalf("parsed %+v %+v", specs[0], specs[1])
	}
	if got := strings.Join(specs[2].stun, ","); got != "stun.cloudflare.com:3478,stun.l.google.com:19302,[2001:db8::1]:3478" {
		t.Fatalf("stun servers %s", got)
	}
	if specs[4].interval != time.Minute || specs[4].confirm != 2 || len(specs[4].urls) == 0 {
		t.Fatalf("defaults %+v", specs[4])
	}
	if specs, err = parseDDNSWatch(json.RawMessage(`{"probes":[]}`)); err != nil || len(specs) != 0 {
		t.Fatalf("an empty list stops everything: %v", err)
	}
	urls := make([]string, 9)
	for i := range urls {
		urls[i] = fmt.Sprintf("%q", "https://example.com/"+fmt.Sprint(i))
	}
	for _, bad := range []string{
		`{"key":"","family":"v4","method":"url"}`,
		`{"key":"p","family":"v5","method":"url"}`,
		`{"key":"p","family":"v4","method":"ping"}`,
		`{"key":"p","family":"v4","method":"nic","intervalS":9}`,
		`{"key":"p","family":"v4","method":"url","intervalS":29}`,
		`{"key":"p","family":"v4","method":"stun","intervalS":3601}`,
		`{"key":"p","family":"v4","method":"url","confirm":0}`,
		`{"key":"p","family":"v4","method":"url","confirm":6}`,
		`{"key":"p","family":"v4","method":"url","intervalS":"60"}`,
		`{"key":"p","family":"v4","method":"url","urls":["ftp://example.com"]}`,
		`{"key":"p","family":"v4","method":"url","urls":["https://"]}`,
		`{"key":"p","family":"v4","method":"url","urls":[` + strings.Join(urls, ",") + `]}`,
		`{"key":"p","family":"v4","method":"stun","stun":["a:1","b:2","c:3","d:4","e:5"]}`,
		`{"key":"p","family":"v4","method":"stun","stun":["stun.example.com:0"]}`,
		`{"key":"p","family":"v4","method":"stun","stun":["bad host:3478"]}`,
		`{"key":"p","family":"v4","method":"cmd","cmd":" "}`,
		`{"key":"p","family":"v4","method":"cmd","cmd":"` + strings.Repeat("x", 1001) + `"}`,
		`{"key":"p","family":"v6","method":"nic","v6Select":{"mode":"regex","value":"("}}`,
		good + `,` + good,
	} {
		if _, err := parseDDNSWatch(json.RawMessage(`{"probes":[` + bad + `]}`)); !errors.Is(err, errDDNSParams) {
			t.Errorf("%s: %v", bad, err)
		}
	}
	if _, err := parseDDNSWatch(json.RawMessage(`{"key":"p"}`)); err != nil {
		t.Fatalf("no probes key is an empty list: %v", err)
	}
	if _, err := parseDDNSWatch(json.RawMessage(`[1]`)); !errors.Is(err, errDDNSParams) {
		t.Fatalf("not an object: %v", err)
	}
	many := make([]string, ddnsMaxProbes+1)
	for i := range many {
		many[i] = fmt.Sprintf(`{"key":"p%d","family":"v4","method":"nic"}`, i)
	}
	if _, err := parseDDNSWatch(json.RawMessage(`{"probes":[` + strings.Join(many, ",") + `]}`)); !errors.Is(err, errDDNSParams) {
		t.Fatalf("too many probes: %v", err)
	}
}

func echoServer(t *testing.T, network, address string, handler http.HandlerFunc) *httptest.Server {
	t.Helper()
	l, err := net.Listen(network, address)
	if err != nil {
		t.Skipf("no %s loopback: %v", network, err)
	}
	s := httptest.NewUnstartedServer(handler)
	s.Listener.Close()
	s.Listener = l
	s.Start()
	t.Cleanup(s.Close)
	return s
}

func TestDDNSURLReadsTheAddressOverOneFamily(t *testing.T) {
	ctx := context.Background()
	s := echoServer(t, "tcp4", "127.0.0.1:0", func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/plain":
			fmt.Fprint(w, "203.0.113.24\n")
		case "/trace":
			fmt.Fprint(w, "fl=1\nh=example\nip=203.0.113.25\nts=1\n")
		case "/down":
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprint(w, "203.0.113.26")
		case "/junk":
			fmt.Fprint(w, "no address here")
		case "/big":
			fmt.Fprint(w, strings.Repeat("x", ddnsBodyLimit)+" 203.0.113.27")
		case "/slow":
			time.Sleep(time.Second)
			fmt.Fprint(w, "203.0.113.28")
		}
	})
	for path, want := range map[string]string{"/plain": "203.0.113.24", "/trace": "203.0.113.25"} {
		addr, detail, err := ddnsURL(ctx, []string{s.URL + path}, false, "")
		if err != nil || addr.String() != want || detail != s.URL+path {
			t.Fatalf("%s: %v %q %v", path, addr, detail, err)
		}
	}
	addr, detail, err := ddnsURL(ctx, []string{s.URL + "/down", s.URL + "/junk", s.URL + "/trace"}, false, "")
	if err != nil || addr.String() != "203.0.113.25" || detail != s.URL+"/trace" {
		t.Fatalf("falling through to the third URL: %v %q %v", addr, detail, err)
	}
	for path, want := range map[string]string{"/down": "http_error", "/junk": "parse_error", "/big": "parse_error"} {
		if _, _, err := ddnsURL(ctx, []string{s.URL + path}, false, ""); err == nil || err.Error() != want {
			t.Fatalf("%s: %v, want %s", path, err, want)
		}
	}
	short, cancel := context.WithTimeout(ctx, 200*time.Millisecond)
	defer cancel()
	if _, _, err := ddnsURL(short, []string{s.URL + "/slow"}, false, ""); err == nil || err.Error() != "timeout" {
		t.Fatalf("slow: %v", err)
	}
	// An IPv4-only server cannot answer over tcp6.
	if _, _, err := ddnsURL(ctx, []string{s.URL + "/plain"}, true, ""); err == nil {
		t.Fatal("tcp6 reached an IPv4 listener")
	}
	if _, _, err := ddnsURL(ctx, []string{s.URL + "/plain"}, false, "no-such-iface0"); err == nil || err.Error() != "iface_not_found" {
		t.Fatalf("unknown bind interface: %v", err)
	}
	if transport := ddnsHTTPClient(&net.Dialer{}, false).Transport.(*http.Transport); transport.Proxy != nil {
		t.Fatal("echo lookups must not go through a proxy")
	}
}

func TestDDNSURLOverIPv6(t *testing.T) {
	s := echoServer(t, "tcp6", "[::1]:0", func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "ip=2001:db8::24\n") })
	if addr, _, err := ddnsURL(context.Background(), []string{s.URL}, true, ""); err != nil || addr.String() != "2001:db8::24" {
		t.Fatalf("tcp6: %v %v", addr, err)
	}
	if _, _, err := ddnsURL(context.Background(), []string{s.URL}, false, ""); err == nil {
		t.Fatal("tcp4 reached an IPv6 listener")
	}
}

// fakeSTUN answers Binding requests with mapped, or not at all when mapped is invalid.
func fakeSTUN(t *testing.T, network, address string, mapped netip.AddrPort) string {
	t.Helper()
	c, err := net.ListenPacket(network, address)
	if err != nil {
		t.Skipf("no %s loopback: %v", network, err)
	}
	t.Cleanup(func() { c.Close() })
	go func() {
		buf := make([]byte, 1500)
		for {
			n, from, err := c.ReadFrom(buf)
			if err != nil {
				return
			}
			if n < 20 || !mapped.IsValid() {
				continue
			}
			var tx [12]byte
			copy(tx[:], buf[8:20])
			ip := mapped.Addr().AsSlice()
			value := []byte{0, 1, 0, 0}
			if len(ip) == 16 {
				value[1] = 2
			}
			var key [16]byte
			copy(key[:], []byte{0x21, 0x12, 0xa4, 0x42})
			copy(key[4:], tx[:])
			port := mapped.Port() ^ 0x2112
			value[2], value[3] = byte(port>>8), byte(port)
			for i := range ip {
				value = append(value, ip[i]^key[i])
			}
			reply := []byte{0x01, 0x01, 0, byte(4 + len(value)), 0x21, 0x12, 0xa4, 0x42}
			reply = append(reply, tx[:]...)
			reply = append(reply, 0x00, 0x20, 0, byte(len(value)))
			reply = append(reply, value...)
			c.WriteTo(reply, from)
		}
	}()
	return c.LocalAddr().String()
}

func shortSTUNRetries(t *testing.T) {
	saved := stunRetries
	stunRetries = []time.Duration{100 * time.Millisecond, 200 * time.Millisecond}
	t.Cleanup(func() { stunRetries = saved })
}

func TestDDNSSTUNReadsTheMappedAddress(t *testing.T) {
	shortSTUNRetries(t)
	ctx := context.Background()
	silent := fakeSTUN(t, "udp4", "127.0.0.1:0", netip.AddrPort{})
	server := fakeSTUN(t, "udp4", "127.0.0.1:0", netip.MustParseAddrPort("203.0.113.40:51000"))
	addr, detail, err := ddnsSTUN(ctx, []string{silent, server}, false, "")
	if err != nil || addr.String() != "203.0.113.40" || detail != server {
		t.Fatalf("udp4: %v %q %v", addr, detail, err)
	}
	if _, _, err := ddnsSTUN(ctx, []string{silent}, false, ""); err == nil || err.Error() != "timeout" {
		t.Fatalf("no answer: %v", err)
	}
	loop := fakeSTUN(t, "udp4", "127.0.0.1:0", netip.MustParseAddrPort("127.0.0.1:1"))
	if _, _, err := ddnsSTUN(ctx, []string{loop}, false, ""); err == nil || err.Error() != "parse_error" {
		t.Fatalf("a loopback mapping is no address: %v", err)
	}
}

func TestDDNSSTUNOverIPv6(t *testing.T) {
	shortSTUNRetries(t)
	server := fakeSTUN(t, "udp6", "[::1]:0", netip.MustParseAddrPort("[2001:db8::40]:51000"))
	if addr, _, err := ddnsSTUN(context.Background(), []string{server}, true, ""); err != nil || addr.String() != "2001:db8::40" {
		t.Fatalf("udp6: %v %v", addr, err)
	}
	mapped4 := fakeSTUN(t, "udp6", "[::1]:0", netip.MustParseAddrPort("203.0.113.40:51000"))
	if _, _, err := ddnsSTUN(context.Background(), []string{mapped4}, true, ""); err == nil || err.Error() != "parse_error" {
		t.Fatalf("an IPv4 mapping over udp6: %v", err)
	}
}

func TestDDNSCmdNeedsCommandExecution(t *testing.T) {
	spec := ddnsSpec{key: "c", family: "v4", method: "cmd", cmd: "echo 198.51.100.77"}
	off := NewAgent(Config{}, nil)
	if _, _, err := off.ddnsRead(context.Background(), spec); err == nil || err.Error() != "capability_disabled" {
		t.Fatalf("without consent: %v", err)
	}
	on := NewAgent(Config{Capabilities: Capabilities{Exec: true}}, nil)
	if value, _, err := on.ddnsRead(context.Background(), spec); err != nil || value != "198.51.100.77" {
		t.Fatalf("with consent: %q %v", value, err)
	}
	spec.cmd = "echo nothing; exit 3"
	if _, _, err := on.ddnsRead(context.Background(), spec); err == nil || err.Error() != "parse_error" {
		t.Fatalf("a failing command: %v", err)
	}
}

func TestDDNSNICReadsAnInterface(t *testing.T) {
	if _, _, err := ddnsNIC(ddnsSpec{family: "v4", method: "nic", iface: "no-such-iface0"}); err == nil || err.Error() != "iface_not_found" {
		t.Fatalf("unknown interface: %v", err)
	}
	if addr, iface, err := ddnsNIC(ddnsSpec{family: "v4", method: "nic"}); err != nil {
		t.Logf("no default IPv4 interface here: %v", err)
	} else if !addr.Is4() || iface == "" {
		t.Fatalf("default interface: %v %q", addr, iface)
	}
	for _, i := range ddnsInterfaces() {
		for _, a := range i.V6 {
			if a.Scope == "" || a.Addr == "" {
				t.Fatalf("interface %s: %+v", i.Name, a)
			}
		}
	}
}

// exchange sends one call and reads until its reply, keeping the ddns
// reports that arrive meanwhile.
func exchange(t *testing.T, ch *channel, method string, params any, reports *[]map[string]any) map[string]any {
	t.Helper()
	id := method + "-" + fmt.Sprint(time.Now().UnixNano())
	ch.send(map[string]any{"t": "call", "id": id, "method": method, "params": params})
	for {
		m := nextOf(t, ch, "reply", "ddns")
		if m["t"] == "ddns" {
			*reports = append(*reports, m)
		} else if m["id"] == id {
			return m
		}
	}
}

func nextOf(t *testing.T, ch *channel, kinds ...string) map[string]any {
	t.Helper()
	ch.conn.SetIdleTimeout(15 * time.Second)
	for {
		k, data, err := ch.read()
		if err != nil {
			t.Fatalf("waiting for %v: %v", kinds, err)
		}
		var m map[string]any
		if k != kindJSON || json.Unmarshal(data, &m) != nil {
			continue
		}
		for _, kind := range kinds {
			if m["t"] == kind {
				return m
			}
		}
	}
}

func TestDDNSOverASession(t *testing.T) {
	echo := echoServer(t, "tcp4", "127.0.0.1:0", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, "fl=1\nip=203.0.113.24\n")
	})
	f := newFake()
	type outcome struct {
		watch, bad, stop, probe map[string]any
		reports                 []map[string]any
	}
	result := make(chan outcome, 1)
	s := f.serve(t, func(ch *channel, _ map[string]any) {
		var o outcome
		if hello := next(t, ch, "hello"); !strings.Contains(fmt.Sprint(hello["features"]), "ddns") {
			t.Errorf("hello lacks the ddns feature: %v", hello["features"])
		}
		o.bad = exchange(t, ch, "ddns.watch", map[string]any{"probes": []any{map[string]any{"key": "x", "family": "v4", "method": "url", "intervalS": 5}}}, &o.reports)
		o.watch = exchange(t, ch, "ddns.watch", map[string]any{"probes": []any{
			map[string]any{"key": "p_url", "family": "v4", "method": "url", "intervalS": 60, "confirm": 2, "urls": []string{echo.URL}},
			map[string]any{"key": "p_cmd", "family": "v4", "method": "cmd", "intervalS": 60, "confirm": 2, "cmd": "echo 198.51.100.1"},
		}}, &o.reports)
		seen := map[string]bool{}
		for _, r := range o.reports {
			for _, x := range r["obs"].([]any) {
				seen[x.(map[string]any)["key"].(string)] = true
			}
		}
		for len(seen) < 2 {
			r := nextOf(t, ch, "ddns")
			o.reports = append(o.reports, r)
			for _, x := range r["obs"].([]any) {
				seen[x.(map[string]any)["key"].(string)] = true
			}
		}
		o.probe = exchange(t, ch, "ddns.probe", map[string]any{"family": "v4", "methods": []string{"url", "nic", "cmd"}, "urls": []string{echo.URL}, "cmd": "echo 198.51.100.1"}, &o.reports)
		o.stop = exchange(t, ch, "ddns.watch", map[string]any{"probes": []any{}}, &o.reports)
		result <- o
		ch.conn.CloseWith(closeRevoked, "")
	})
	defer s.Close()
	runSession(t, f.agent(s.URL, Capabilities{}))
	o := <-result
	if o.bad["ok"] != false || o.bad["error"] != "invalid_params" {
		t.Fatalf("an interval under the minimum: %v", o.bad)
	}
	if o.watch["ok"] != true || o.watch["result"].(map[string]any)["accepted"].(float64) != 2 {
		t.Fatalf("watch reply: %v", o.watch)
	}
	if o.stop["ok"] != true || o.stop["result"].(map[string]any)["accepted"].(float64) != 0 {
		t.Fatalf("stop reply: %v", o.stop)
	}
	obs := map[string]map[string]any{}
	for _, r := range o.reports {
		if len(r) != 2 {
			t.Fatalf("report has extra fields: %v", r)
		}
		for _, x := range r["obs"].([]any) {
			m := x.(map[string]any)
			for _, field := range []string{"key", "family", "method", "value", "error", "detail", "at", "fails"} {
				if _, ok := m[field]; !ok {
					t.Fatalf("observation lacks %s: %v", field, m)
				}
			}
			obs[m["key"].(string)] = m
		}
	}
	if u := obs["p_url"]; u["value"] != "203.0.113.24" || u["error"] != "" || u["family"] != "v4" || u["method"] != "url" ||
		u["fails"].(float64) != 0 || u["at"].(float64) < float64(time.Now().Add(-time.Minute).UnixMilli()) {
		t.Fatalf("url observation: %v", u)
	}
	if c := obs["p_cmd"]; c["error"] != "capability_disabled" || c["value"] != "" {
		t.Fatalf("a command probe without consent: %v", c)
	}
	probe := o.probe["result"].(map[string]any)
	results := probe["results"].([]any)
	if len(results) != 3 {
		t.Fatalf("probe results: %v", probe)
	}
	if r := results[0].(map[string]any); r["method"] != "url" || r["value"] != "203.0.113.24" || r["error"] != "" {
		t.Fatalf("probe url: %v", r)
	}
	if r := results[1].(map[string]any); r["method"] != "nic" {
		t.Fatalf("probe nic: %v", r)
	}
	if r := results[2].(map[string]any); r["method"] != "cmd" || r["error"] != "capability_disabled" {
		t.Fatalf("probe cmd: %v", r)
	}
	if _, ok := probe["interfaces"].([]any); !ok {
		t.Fatalf("probe interfaces: %v", probe)
	}
}
