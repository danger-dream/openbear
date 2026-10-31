package health

import (
	"slices"
	"sync"
)

// Bounds of the control message queues: each peer's own, all of them
// together, and how many the manager takes before its tick may run.
const (
	inboxPerPeer = 256
	inboxTotal   = 1024
	inboxBatch   = 64
)

// inbox queues control messages by the peer that sent them and hands them
// out a peer at a time in turn, so that a peer sending more than the
// manager takes delays its own messages, not the others'. A full peer
// queue drops the new message; with all of them full together, the longest
// queue loses its oldest.
type inbox struct {
	mu     sync.Mutex
	queues map[string][]Received
	order  []string // peers with a message waiting, next first
	total  int
	ready  chan struct{}
}

func newInbox() *inbox {
	return &inbox{queues: map[string][]Received{}, ready: make(chan struct{}, 1)}
}

func (b *inbox) push(r Received) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	q := b.queues[r.Peer]
	if len(q) >= inboxPerPeer {
		return false
	}
	if b.total >= inboxTotal {
		longest := ""
		for p, other := range b.queues {
			if len(other) > len(b.queues[longest]) {
				longest = p
			}
		}
		if len(b.queues[longest]) <= len(q) {
			return false
		}
		b.queues[longest][0] = Received{}
		if rest := b.queues[longest][1:]; len(rest) > 0 {
			b.queues[longest] = rest
		} else {
			delete(b.queues, longest)
			b.order = slices.DeleteFunc(b.order, func(p string) bool { return p == longest })
		}
		b.total--
	}
	if len(q) == 0 {
		b.order = append(b.order, r.Peer)
	}
	b.queues[r.Peer] = append(q, r)
	b.total++
	b.signal()
	return true
}

// pop takes the oldest message of the peer whose turn it is.
func (b *inbox) pop() (Received, bool) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if len(b.order) == 0 {
		return Received{}, false
	}
	peer := b.order[0]
	b.order = b.order[1:]
	q := b.queues[peer]
	r := q[0]
	q[0] = Received{}
	if q = q[1:]; len(q) == 0 {
		delete(b.queues, peer)
	} else {
		b.queues[peer] = q
		b.order = append(b.order, peer)
	}
	b.total--
	return r, true
}

// again signals once more if messages are left after a batch.
func (b *inbox) again() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.total > 0 {
		b.signal()
	}
}

func (b *inbox) signal() {
	select {
	case b.ready <- struct{}{}:
	default:
	}
}
