package mesh

import (
	"net/netip"
	"testing"
	"time"
)

type clock struct{ t time.Time }

func (c *clock) now() time.Time          { return c.t }
func (c *clock) advance(d time.Duration) { c.t = c.t.Add(d) }

func testScheduler(maxBytes int) (*scheduler, *clock, *int) {
	c := &clock{t: time.Unix(1000, 0)}
	s := newScheduler(1024, maxBytes)
	s.now = c.now
	drops := 0
	s.dropped = func(n int) { drops += n }
	return s, c, &drops
}

var (
	srcA = netip.MustParseAddr("100.96.0.11")
	dstB = netip.MustParseAddr("100.96.0.12")
)

func flowPacket(sport uint16, size int, id uint16) []byte {
	return packet(srcA, dstB, protoTCP, 64, id, 0, false, ports(sport, 443, size-20))
}

func sportOf(b []byte) uint16 { return uint16(b[20])<<8 | uint16(b[21]) }

func TestSchedulerSharesBetweenFlows(t *testing.T) {
	s, _, _ := testScheduler(8 << 20)
	for i := 0; i < 40; i++ {
		s.enqueue(flowPacket(1000, 1400, uint16(i)))
	}
	for i := 0; i < 40; i++ {
		s.enqueue(flowPacket(2000, 1400, uint16(i)))
	}
	count := map[uint16]int{}
	last := map[uint16]uint16{}
	for i := 0; i < 40; i++ {
		b := s.tryDequeue()
		p := sportOf(b)
		count[p]++
		id := uint16(b[4])<<8 | uint16(b[5])
		if id < last[p] {
			t.Fatal("a flow's packets were reordered")
		}
		last[p] = id
	}
	if count[1000] < 18 || count[2000] < 18 {
		t.Fatalf("first 40 packets: %v; the flow queued first took more than its share", count)
	}
}

func TestSchedulerSmallPacketsFirstWithinTheirShare(t *testing.T) {
	s, _, _ := testScheduler(8 << 20)
	s.enqueue(flowPacket(1000, 1400, 1))
	s.enqueue(flowPacket(3000, 100, 1))
	if sportOf(s.tryDequeue()) != 3000 {
		t.Fatal("a new small flow did not go first")
	}
	// Bulk keeps a backlog; new small flows keep arriving faster than their
	// share. Small flows that collide in a bucket queue as bulk, fairly, so
	// only what the priority path sends counts against the share.
	ahead, total := 0, 0
	for i := 0; i < 2000; i++ {
		for s.buckets[s.classify(flowPacket(1000, 1400, 0))].bytes < 30000 {
			s.enqueue(flowPacket(1000, 1400, uint16(i)))
		}
		s.enqueue(flowPacket(uint16(4000+i), 100, 1))
		_, first, _ := s.pick()
		b := s.tryDequeue()
		total += len(b)
		if first {
			ahead += len(b)
		}
	}
	share := float64(ahead) / float64(total)
	if limit := smallShare + smallBurst/float64(total); share > limit+0.005 || share < 0.08 {
		t.Fatalf("small packets went ahead with %.3f of the bytes sent while bulk waited", share)
	}
	// A flow that has sent small packets keeps its order when it sends a big one.
	s2, _, _ := testScheduler(8 << 20)
	s2.enqueue(flowPacket(1000, 1400, 1))
	s2.enqueue(flowPacket(5000, 100, 1))
	s2.enqueue(flowPacket(5000, 1400, 2))
	s2.enqueue(flowPacket(5000, 100, 3))
	var ids []byte
	for b := s2.tryDequeue(); b != nil; b = s2.tryDequeue() {
		if sportOf(b) == 5000 {
			ids = append(ids, b[5])
		}
	}
	if string(ids) != "\x01\x02\x03" {
		t.Fatalf("flow order %v", ids)
	}
}

func TestSchedulerRateLimit(t *testing.T) {
	s, c, _ := testScheduler(8 << 20)
	mbps := 8.0 // 1 MB/s
	s.setRate(&mbps)
	for i := 0; i < 100; i++ {
		s.enqueue(flowPacket(1000, 1000, uint16(i)))
	}
	sent := 0
	for step := 0; step < 1000; step++ { // one second in 1 ms steps
		for {
			b, wait, _ := s.next()
			if b == nil {
				if wait <= 0 {
					t.Fatal("nothing to send and no wait")
				}
				break
			}
			sent += len(b) + wireOverhead
		}
		c.advance(time.Millisecond)
		if s.queuedBytes() < 50000 {
			for i := 0; i < 10; i++ {
				s.enqueue(flowPacket(1000, 1000, 0))
			}
		}
	}
	if sent < 950_000 || sent > 1_000_000+int(s.burst())+2000 {
		t.Fatalf("sent %d bytes in one second at 1 MB/s", sent)
	}
}

func TestSchedulerByteBudgetDropsFromTheFattestFlow(t *testing.T) {
	s, _, drops := testScheduler(20000)
	for i := 0; i < 12; i++ {
		s.enqueue(flowPacket(1000, 1400, uint16(i)))
	}
	for i := 0; i < 3; i++ {
		s.enqueue(flowPacket(2000, 1400, uint16(i)))
	}
	if s.queuedBytes() > 20000 || *drops == 0 {
		t.Fatalf("queued %d, dropped %d", s.queuedBytes(), *drops)
	}
	thin := s.buckets[s.classify(flowPacket(2000, 1400, 0))]
	if thin.len() != 3 {
		t.Fatalf("the thin flow lost packets: %d left", thin.len())
	}
	fat := s.buckets[s.classify(flowPacket(1000, 1400, 0))]
	if id := fat.peek().b[5]; id == 0 {
		t.Fatal("the fat flow kept its oldest packet")
	}
	// One flow alone is held to its own budget.
	s2, _, _ := testScheduler(8 << 20)
	for i := 0; i < 2000; i++ {
		s2.enqueue(flowPacket(1000, 1400, uint16(i)))
	}
	if q := s2.queuedBytes(); q > s2.flowBytes || q < s2.flowBytes-1400 {
		t.Fatalf("one flow queued %d bytes, its limit is %d", q, s2.flowBytes)
	}
}

func TestSchedulerFlowLimitFollowsTheRate(t *testing.T) {
	s, _, drops := testScheduler(8 << 20)
	mbps := func(v float64) *float64 { return &v }
	for _, c := range []struct {
		rate *float64
		want int
	}{
		{nil, 1 << 20},          // no rate: an eighth of the budget
		{mbps(45), 1_125_000},   // 200 ms at 45 Mbit/s
		{mbps(1), flowQueueMin}, // 25 kB, raised to the floor
		{mbps(10000), 2 << 20},  // a quarter of the budget at most
		{nil, 1 << 20},
	} {
		s.setRate(c.rate)
		if s.flowBytes != c.want {
			t.Fatalf("rate %v: flow limit %d, want %d", c.rate, s.flowBytes, c.want)
		}
	}
	// A TCP window's burst at 45 Mbit/s and 60 ms (338 kB) fits one flow.
	s.setRate(mbps(45))
	for i := 0; i < 338_000/1400; i++ {
		s.enqueue(flowPacket(1000, 1400, uint16(i)))
	}
	if *drops != 0 {
		t.Fatalf("one flow's window burst lost %d packets", *drops)
	}
}

// TestSchedulerSingleFlowKeepsTheRate feeds one flow as fast as the rate
// drains it, the way a TCP sender in congestion avoidance does, and checks
// the queue neither starves the rate nor stands above target for long.
func TestSchedulerSingleFlowKeepsTheRate(t *testing.T) {
	s, c, drops := testScheduler(8 << 20)
	rate := 45.0
	s.setRate(&rate)
	sent, id := 0, uint16(0)
	var worst time.Duration
	for step := 0; step < 2000; step++ { // two seconds in 1 ms steps
		// The sender keeps 15 ms of queue, more than 64 KiB but within
		// target: it should neither be dropped nor starve the rate.
		for n := 0; n < 100 && s.queuedBytes() < 84_000; n++ {
			id++
			s.enqueue(flowPacket(1000, 1400, id))
		}
		for {
			b, _, n := s.next()
			*drops += n
			if b == nil {
				break
			}
			sent += len(b) + wireOverhead
		}
		if f := s.buckets[s.classify(flowPacket(1000, 1400, 0))]; f.len() > 0 {
			worst = max(worst, c.now().Sub(f.peek().at))
		}
		c.advance(time.Millisecond)
	}
	if got := float64(sent) * 8 / 2 / 1e6; got < 0.98*rate || *drops != 0 || worst > queueTarget {
		t.Fatalf("one flow: %.1f Mbit/s of %g, %d drops, oldest packet %v", got, rate, *drops, worst)
	}
}

// TestSchedulerCoDelPerFlow: a sparse flow served between a bulk flow's
// packets does not reset the bulk flow's congestion, and is not dropped
// for it.
func TestSchedulerCoDelPerFlow(t *testing.T) {
	s, c, drops := testScheduler(8 << 20)
	rate := 8.0 // 1 MB/s
	s.setRate(&rate)
	id := uint16(0)
	smallSent := 0
	for step := 0; step < 1000; step++ {
		for n := 0; n < 100 && s.buckets[s.classify(flowPacket(1000, 1400, 0))].bytes < 100_000; n++ { // 100 ms standing
			id++
			s.enqueue(flowPacket(1000, 1400, id))
		}
		if step%5 == 0 {
			s.enqueue(flowPacket(uint16(3000+step), 100, 1))
		}
		for {
			b, _, n := s.next()
			*drops += n
			if b == nil {
				break
			}
			if sportOf(b) != 1000 {
				smallSent++
			}
		}
		c.advance(time.Millisecond)
	}
	if *drops < 5 {
		t.Fatalf("a bulk flow standing at 100 ms was dropped %d times in a second", *drops)
	}
	if smallSent != 200 {
		t.Fatalf("sparse flows sent %d of 200 packets", smallSent)
	}
}

func TestSchedulerSojournLimits(t *testing.T) {
	s, c, drops := testScheduler(8 << 20)
	s.enqueue(flowPacket(1000, 1400, 1))
	c.advance(queueMaxAge + time.Millisecond)
	if b := s.tryDequeue(); b != nil || *drops != 1 {
		t.Fatalf("a packet older than %v was sent (drops %d)", queueMaxAge, *drops)
	}

	// A standing queue above target: drops start after an interval.
	s, c, drops = testScheduler(8 << 20)
	for i := 0; i < 40; i++ {
		s.enqueue(flowPacket(1000, 1400, uint16(i)))
	}
	c.advance(50 * time.Millisecond)
	for i := 0; i < 3; i++ {
		s.tryDequeue()
		s.enqueue(flowPacket(1000, 1400, 0))
	}
	if *drops != 0 {
		t.Fatal("dropped before the queue stayed above target for an interval")
	}
	c.advance(queueInterval)
	s.tryDequeue()
	if *drops == 0 {
		t.Fatal("no drop after the sojourn time stayed above target")
	}
}
