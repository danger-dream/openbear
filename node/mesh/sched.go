package mesh

import (
	"encoding/binary"
	"errors"
	"hash/maphash"
	"math"
	"sync"
	"time"
)

// Scheduler parameters (transport appendix 5.1 and 7.2).
const (
	queueTarget   = 20 * time.Millisecond
	queueInterval = 100 * time.Millisecond // sojourn above target this long counts as congestion
	queueMaxAge   = 200 * time.Millisecond
	// A flow may queue what the upload limit sends in queueMaxAge, so one
	// TCP flow can keep a window's worth of burst while CoDel holds its
	// standing queue near queueTarget. Without a limit the drain rate is
	// unknown and a share of the whole budget bounds it instead.
	flowQueueMin    = 64 << 10
	flowShareRated  = 4    // with a limit, at most maxBytes/4 per flow
	flowShareFree   = 8    // without one, maxBytes/8 per flow
	smallPacket     = 256  // a flow arriving idle with a packet this small is served first
	smallShare      = 0.10 // of everything sent while bulk flows wait
	smallBurst      = 16 << 10
	drrQuantum      = 1514
	wireOverhead    = 60 // outer IPv4, UDP and WireGuard bytes the upload limit also pays
	fragmentEntries = 1024
)

var errQueueClosed = errors.New("queue closed")

type queued struct {
	b  []byte
	at time.Time
}

type flowQueue struct {
	pkts    []queued
	head    int
	bytes   int
	deficit int
	list    int8 // 0 idle, 1 small, 2 bulk

	// CoDel state, per flow: a sparse flow served between a bulk flow's
	// packets must not reset the bulk flow's congestion.
	firstAbove time.Time
	nextDrop   time.Time
	drops      int
}

func (f *flowQueue) len() int { return len(f.pkts) - f.head }

func (f *flowQueue) peek() *queued { return &f.pkts[f.head] }

func (f *flowQueue) pop() queued {
	q := f.pkts[f.head]
	f.pkts[f.head] = queued{}
	f.head++
	f.bytes -= len(q.b)
	if f.head == len(f.pkts) {
		f.pkts, f.head = f.pkts[:0], 0
	} else if f.head > 64 && f.head*2 > len(f.pkts) {
		f.pkts = append(f.pkts[:0], f.pkts[f.head:]...)
		f.head = 0
	}
	return q
}

// scheduler queues packets before WireGuard encrypts them: deficit round
// robin over inner flows hashed into buckets, new sparse flows first within
// a 10 % share, a token bucket for the upload limit, drops by byte budget
// from the most backlogged flow's oldest packet, and drops by sojourn time
// (CoDel per flow, then the hard age limit) from the flow's own oldest
// packet. One bucket holds a whole flow, so a flow's packets keep their order.
type scheduler struct {
	mu       sync.Mutex
	buckets  []flowQueue
	small    []int // bucket indexes
	bulk     []int
	bytes    int
	smallB   int // bytes in buckets on the small list
	maxBytes int
	seed     maphash.Seed
	frags    map[fragID]int
	closed   bool
	wake     chan struct{}
	done     chan struct{}
	now      func() time.Time

	smallCredit float64

	rate      float64 // bytes per second; 0 is unlimited
	tokens    float64
	filled    time.Time
	flowBytes int // per-flow byte limit, follows the rate

	dropped func(n int)

	// bySource queues by source address alone, which on a relay is the
	// node a forwarded end-to-end packet is from.
	bySource bool
}

type fragID struct {
	src, dst [4]byte
	id       uint16
	proto    byte
}

func newScheduler(buckets, maxBytes int) *scheduler {
	s := &scheduler{buckets: make([]flowQueue, buckets), maxBytes: maxBytes, seed: maphash.MakeSeed(),
		frags: map[fragID]int{}, wake: make(chan struct{}, 1), done: make(chan struct{}), now: time.Now,
		smallCredit: smallBurst, dropped: func(int) {}}
	s.flowBytes = s.flowLimit()
	return s
}

// flowLimit is the most one flow may queue: what the rate sends in
// queueMaxAge (older packets would be dropped anyway), within
// [flowQueueMin, maxBytes/flowShareRated]; maxBytes/flowShareFree without
// a rate.
func (s *scheduler) flowLimit() int {
	if s.rate == 0 {
		return max(flowQueueMin, s.maxBytes/flowShareFree)
	}
	n := int(math.Min(s.rate*queueMaxAge.Seconds(), float64(s.maxBytes/flowShareRated)))
	return max(flowQueueMin, n)
}

// setRate takes megabits per second; nil removes the limit.
func (s *scheduler) setRate(mbps *float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	defer func() { s.flowBytes = s.flowLimit() }()
	if mbps == nil {
		s.rate = 0
		return
	}
	s.rate = *mbps * 1e6 / 8
	s.filled = s.now()
	s.tokens = math.Min(s.tokens, s.burst())
}

func (s *scheduler) burst() float64 { return math.Max(16<<10, s.rate*0.005) }

func (s *scheduler) queuedBytes() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.bytes
}

func (s *scheduler) close() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.closed {
		s.closed = true
		close(s.done)
	}
}

// classify hashes the inner IPv4 flow; later fragments follow their first
// fragment, anything unparsable shares bucket 0.
func (s *scheduler) classify(b []byte) int {
	p, ok := parseIPv4(b)
	if !ok {
		return 0
	}
	var h maphash.Hash
	h.SetSeed(s.seed)
	if s.bySource {
		h.Write(b[12:16])
		return int(h.Sum64() % uint64(len(s.buckets)))
	}
	h.Write(b[12:20])
	h.WriteByte(p.proto)
	fid := fragID{[4]byte(b[12:16]), [4]byte(b[16:20]), p.id, p.proto}
	if p.frag != 0 {
		if i, ok := s.frags[fid]; ok {
			if !p.more {
				delete(s.frags, fid)
			}
			return i
		}
	} else if a, c, ok := p.ports(); ok {
		var ports [4]byte
		binary.BigEndian.PutUint16(ports[:], a)
		binary.BigEndian.PutUint16(ports[2:], c)
		h.Write(ports[:])
	}
	i := int(h.Sum64() % uint64(len(s.buckets)))
	if p.frag == 0 && p.more {
		if len(s.frags) >= fragmentEntries {
			clear(s.frags)
		}
		s.frags[fid] = i
	}
	return i
}

// bucketOf is the bucket packet b would be queued in; an end-to-end packet
// made of b is queued there too, keeping the flows apart.
func (s *scheduler) bucketOf(b []byte) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.classify(b)
}

// enqueue takes ownership of b.
func (s *scheduler) enqueue(b []byte) { s.enqueueAt(b, -1) }

// enqueueAt queues b in bucket i, or where classify puts it for an i out
// of range.
func (s *scheduler) enqueueAt(b []byte, i int) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	if i < 0 || i >= len(s.buckets) {
		i = s.classify(b)
	}
	f := &s.buckets[i]
	dropped := 0
	for f.len() > 0 && f.bytes+len(b) > s.flowBytes {
		s.drop(i)
		dropped++
	}
	f.pkts = append(f.pkts, queued{b, s.now()})
	f.bytes += len(b)
	s.bytes += len(b)
	if f.list == 1 {
		s.smallB += len(b)
	}
	if f.list == 0 {
		if len(b) <= smallPacket {
			f.list = 1
			s.smallB += len(b)
			s.small = append(s.small, i)
		} else {
			f.list = 2
			f.deficit = drrQuantum
			s.bulk = append(s.bulk, i)
		}
	}
	for s.bytes > s.maxBytes {
		s.drop(s.fattest())
		dropped++
	}
	s.mu.Unlock()
	if dropped > 0 {
		s.dropped(dropped)
	}
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

func (s *scheduler) fattest() int {
	best, most := -1, -1
	for _, list := range [][]int{s.small, s.bulk} {
		for _, i := range list {
			if b := s.buckets[i].bytes; b > most {
				best, most = i, b
			}
		}
	}
	return best
}

// drop discards the oldest packet of bucket i; an emptied bucket stays on
// its list until the dequeue loop passes it.
func (s *scheduler) drop(i int) {
	q := s.buckets[i].pop()
	s.bytes -= len(q.b)
	if s.buckets[i].list == 1 {
		s.smallB -= len(q.b)
	}
}

// dequeue blocks until a packet may be sent, or the scheduler closes.
func (s *scheduler) dequeue() ([]byte, error) {
	t := time.NewTimer(time.Hour)
	defer t.Stop()
	for {
		b, wait, dropped := s.next()
		if dropped > 0 {
			s.dropped(dropped)
		}
		if b != nil {
			return b, nil
		}
		if wait <= 0 {
			wait = time.Hour
		}
		t.Reset(wait)
		select {
		case <-s.done:
			return nil, errQueueClosed
		case <-s.wake:
		case <-t.C:
		}
	}
}

// tryDequeue returns nil when nothing may be sent right now.
func (s *scheduler) tryDequeue() []byte {
	b, _, dropped := s.next()
	if dropped > 0 {
		s.dropped(dropped)
	}
	return b
}

// next picks the packet to send, or how long the rate limit makes it wait.
func (s *scheduler) next() (pkt []byte, wait time.Duration, dropped int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		return nil, 0, 0
	}
	now := s.now()
	for {
		i, small, charged := s.pick()
		if i < 0 {
			return nil, 0, dropped
		}
		f := &s.buckets[i]
		head := f.peek()
		age := now.Sub(head.at)
		if age > queueMaxAge {
			s.drop(i)
			dropped++
			continue
		}
		if f.congested(age, now) {
			s.drop(i)
			dropped++
			continue
		}
		size := len(head.b)
		if s.rate > 0 {
			s.tokens = math.Min(s.burst(), s.tokens+now.Sub(s.filled).Seconds()*s.rate)
			s.filled = now
			if need := float64(size + wireOverhead); s.tokens < need {
				return nil, time.Duration((need - s.tokens) / s.rate * float64(time.Second)), dropped
			}
			s.tokens -= float64(size + wireOverhead)
		}
		q := f.pop()
		s.bytes -= size
		if small {
			if charged {
				s.smallCredit -= float64(size)
			}
			s.smallB -= size
			s.small = s.small[1:]
			if f.len() > 0 {
				// A flow that keeps sending is bulk from now on.
				s.smallB -= f.bytes
				f.list, f.deficit = 2, drrQuantum
				s.bulk = append(s.bulk, i)
			} else {
				f.list = 0
			}
		} else {
			f.deficit -= size
			s.smallCredit = math.Min(smallBurst, s.smallCredit+float64(size)*smallShare/(1-smallShare))
		}
		return q.b, 0, dropped
	}
}

// pick returns the bucket to serve next, rotating DRR deficits and
// dropping emptied buckets from the lists. Small flows spend credit only
// while bulk flows wait.
func (s *scheduler) pick() (i int, small, charged bool) {
	for len(s.small) > 0 && s.buckets[s.small[0]].len() == 0 {
		s.buckets[s.small[0]].list = 0
		s.small = s.small[1:]
	}
	if len(s.small) > 0 {
		if s.bytes == s.smallB {
			return s.small[0], true, false
		}
		if s.smallCredit >= float64(len(s.buckets[s.small[0]].peek().b)) {
			return s.small[0], true, true
		}
	}
	for len(s.bulk) > 0 {
		i := s.bulk[0]
		f := &s.buckets[i]
		if f.len() == 0 {
			f.list, f.deficit = 0, 0
			s.bulk = s.bulk[1:]
			continue
		}
		if f.deficit < len(f.peek().b) {
			f.deficit += drrQuantum
			s.bulk = append(s.bulk[1:], i)
			continue
		}
		return i, false, false
	}
	if len(s.small) > 0 {
		return s.small[0], true, false
	}
	return -1, false, false
}

// congested follows CoDel for one flow: once the sojourn time of its head
// has stayed above target for an interval, drop one packet, then more often
// while it stays there.
func (f *flowQueue) congested(age time.Duration, now time.Time) bool {
	if age < queueTarget || f.bytes <= drrQuantum {
		f.firstAbove, f.drops = time.Time{}, 0
		return false
	}
	if f.firstAbove.IsZero() {
		f.firstAbove = now.Add(queueInterval)
		return false
	}
	if now.Before(f.firstAbove) || now.Before(f.nextDrop) {
		return false
	}
	f.drops++
	f.nextDrop = now.Add(time.Duration(float64(queueInterval) / math.Sqrt(float64(f.drops))))
	return true
}
