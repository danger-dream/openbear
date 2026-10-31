package browser

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

func heapInUse() uint64 {
	runtime.GC()
	var m runtime.MemStats
	runtime.ReadMemStats(&m)
	return m.HeapAlloc
}

// sendLargeRequest feeds the events of one request whose post data and
// headers take about 1.75MB, each string freshly allocated.
func sendLargeRequest(obs *Observations, sid string, i int) {
	rid := "r" + strconv.Itoa(i)
	obs.event("Network.requestWillBeSentExtraInfo", O("requestId", rid,
		"headers", O("X-Extra", strings.Repeat("e", 256<<10))), sid)
	obs.event("Network.requestWillBeSent", O("requestId", rid, "request", O(
		"method", "POST", "url", "https://test.invalid/"+rid, "postData", strings.Repeat("p", 1<<20),
		"headers", O("X-Request", "yes"))), sid)
	obs.event("Network.responseReceived", O("requestId", rid, "response", O(
		"url", "https://test.invalid/"+rid, "status", 200, "headers", O("X-Big", strings.Repeat("h", 512<<10)))), sid)
	obs.event("Network.loadingFinished", O("requestId", rid), sid)
}

// checkAccounting verifies the byte count matches what the records hold.
func checkAccounting(t *testing.T, obs *Observations) {
	t.Helper()
	sum := 0
	for _, record := range obs.records.Values() {
		sum += record.size
	}
	for _, list := range obs.pendingExtra.Values() {
		sum += pendingSize(list)
	}
	if sum != obs.bytes {
		t.Fatalf("accounted %d, records hold %d", obs.bytes, sum)
	}
	if len(obs.network.rows) != obs.records.Len() {
		t.Fatalf("%d rows for %d records", len(obs.network.rows), obs.records.Len())
	}
}

func TestPageNetworkRecordsStayWithinTheirByteLimit(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	e.config.Set("maxEventEntries", 100000)
	e.config.Set("maxBodyBytes", 2<<20)
	e.config.Set("maxArtifactBytes", 2<<20)
	page.obs.configure(100000)
	ctx := context.Background()
	before := heapInUse()
	const requests = 600 // about 1GB of events
	loop(e, func() {
		obs := page.obs
		for i := 1; i <= requests; i++ {
			sendLargeRequest(obs, page.session, i)
			if obs.bytes > maxPageNetworkBytes {
				t.Fatalf("request %d: %d bytes held", i, obs.bytes)
			}
		}
		checkAccounting(t, obs)
		if obs.records.Len() < 10 || obs.records.Len() > 30 {
			t.Fatalf("%d records kept", obs.records.Len())
		}
		if grown := int64(heapInUse()) - int64(before); grown > 3*maxPageNetworkBytes/2 {
			t.Fatalf("heap grew %dMB", grown>>20)
		}

		// The newest records keep their details and bodies.
		headers, err := obs.details(ctx, O("id", requests, "part", "response-headers"))
		if err != nil || len(asStr(asObj(asObj(headers).Must("headers")).Must("x-big"))) != 512<<10 {
			t.Fatalf("headers: %v", err)
		}
		headers, err = obs.details(ctx, O("id", requests, "part", "request-headers"))
		if err != nil || len(asStr(asObj(asObj(headers).Must("headers")).Must("x-extra"))) != 256<<10 {
			t.Fatalf("extra headers: %v", err)
		}
		result, err := obs.details(ctx, O("id", requests, "part", "request-body", "outputPath", filepath.Join(t.TempDir(), "body")))
		if err != nil {
			t.Fatal(err)
		}
		if data, _ := os.ReadFile(asStr(asObj(result).Must("artifact"))); len(data) != 1<<20 {
			t.Fatalf("body %d bytes", len(data))
		}
		// The oldest are reported as dropped, in the list and on request.
		if _, err := obs.details(ctx, O("id", 1, "part", "response-headers")); err == nil || err.(*Error).Code != "network_entry_evicted" {
			t.Fatalf("evicted record: %v", err)
		}
		if !truthy(obs.network.read(0).Must("dropped")) {
			t.Fatal("loss not reported")
		}
		if _, err := obs.details(ctx, O("id", requests+1, "part", "response-headers")); err == nil || err.(*Error).Code != "network_entry_expired" {
			t.Fatalf("unknown record: %v", err)
		}
	})
}

func TestUnmatchedExtraHeadersCountTowardsTheLimit(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	page.obs.configure(100000)
	loop(e, func() {
		obs := page.obs
		for i := 0; i < 200; i++ {
			obs.event("Network.responseReceivedExtraInfo", O("requestId", "orphan"+strconv.Itoa(i),
				"headers", O("X-Big", strings.Repeat("h", 1<<20))), page.session)
		}
		checkAccounting(t, obs)
		if obs.bytes > maxPageNetworkBytes || obs.pendingExtra.Len() > 32 {
			t.Fatalf("%d bytes in %d pending", obs.bytes, obs.pendingExtra.Len())
		}
	})
}

func TestAllPagesShareANetworkByteLimit(t *testing.T) {
	e, first, _ := testEngine(t, nil)
	e.config.Set("maxEventEntries", 100000)
	first.obs.configure(100000)
	pages := []*Page{first}
	for i := 1; i < 6; i++ {
		p := newPage(e, "target"+strconv.Itoa(i))
		p.session = "session" + strconv.Itoa(i)
		e.pages.Set(p.target, p)
		e.sessions[p.session] = p
		pages = append(pages, p)
	}
	n := 0
	loop(e, func() {
		// Each page stays just under its own limit; together they exceed the total.
		for _, p := range pages {
			for p.obs.bytes < maxPageNetworkBytes-2<<20 {
				n++
				sendLargeRequest(p.obs, p.session, n)
			}
		}
		total := 0
		for _, p := range pages {
			checkAccounting(t, p.obs)
			total += p.obs.bytes
		}
		if total > maxNetworkBytes {
			t.Fatalf("%d bytes over all pages", total)
		}
		// The oldest records went first: the first page's, then the second's.
		if first.obs.records.Len() != 0 || pages[5].obs.evicted != 0 || pages[4].obs.evicted != 0 {
			t.Fatalf("records kept per page: %d %d %d", first.obs.records.Len(), pages[4].obs.records.Len(), pages[5].obs.records.Len())
		}
		if _, err := first.obs.details(context.Background(), O("id", 1, "part", "response-headers")); err == nil || err.(*Error).Code != "network_entry_evicted" {
			t.Fatalf("evicted record: %v", err)
		}
	})
}

func TestBodyWaitEndsWhenItsRecordIsDropped(t *testing.T) {
	e, page, _ := testEngine(t, nil)
	page.obs.configure(100000)
	loop(e, func() {
		obs := page.obs
		obs.event("Network.requestWillBeSent", O("requestId", "slow", "request", O("method", "GET", "url", "https://test.invalid/slow")), page.session)
		obs.event("Network.responseReceived", O("requestId", "slow", "response", O("url", "https://test.invalid/slow", "status", 200, "headers", O())), page.session)
	})
	go func() {
		time.Sleep(100 * time.Millisecond)
		for i := 0; i < 40; i++ {
			e.onEvent("Network.requestWillBeSent", O("requestId", "big"+strconv.Itoa(i), "request", O(
				"method", "POST", "url", "https://test.invalid/", "postData", strings.Repeat("p", 1<<20))), page.session)
		}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	var err error
	loop(e, func() {
		_, err = page.obs.details(ctx, O("id", 1, "part", "response-body", "outputPath", filepath.Join(t.TempDir(), "body")))
	})
	if err == nil || err.(*Error).Code != "network_entry_evicted" {
		t.Fatalf("wait ended with %v", err)
	}
}
