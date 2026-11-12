//go:build linux

package transport

import (
	"bytes"
	"encoding/binary"
	"errors"
	"net"
	"net/netip"
	"testing"
	"time"

	"golang.org/x/net/ipv6"
	"golang.org/x/sys/unix"
)

func TestSegmentationTakesEqualSegmentsAndAShorterLast(t *testing.T) {
	b := func(n ...int) [][]byte {
		var out [][]byte
		for _, x := range n {
			out = append(out, make([]byte, x))
		}
		return out
	}
	for _, c := range []struct {
		bufs [][]byte
		want bool
	}{
		{b(1200, 1200, 1200), true},
		{b(1200, 1200, 300), true},
		{b(1200, 300, 1200), false},
		{b(1200, 1300), false},
		{b(1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400, 1400), false},
	} {
		if got := gsoFits(c.bufs); got != c.want {
			t.Errorf("%d buffers: %v, want %v", len(c.bufs), got, c.want)
		}
	}
}

// Segmentation the socket refuses before sending anything falls back to
// sendmmsg, which delivers the batch, and is not tried on the path for a
// while; a failure that may have sent part of it is not sent again.
func TestSegmentationFallsBack(t *testing.T) {
	uri, _, receive := batchSink(t, "udp")
	b := newBind(t, Options{UDP: true})
	openBind(t, b)
	ep, err := b.ParseEndpoint(uri)
	if err != nil {
		t.Fatal(err)
	}
	batch := [][]byte{bytes.Repeat([]byte{1}, 500), bytes.Repeat([]byte{2}, 500), bytes.Repeat([]byte{3}, 200)}
	calls := 0
	refuse := func([]byte, []byte, netip.AddrPort) (int, int, error) { calls++; return 0, 0, unix.EIO }
	if handled, err := b.sendUDPBatchWith(batch, ep, false, nil, farDeadline(), refuse); !handled || err != nil || calls != 1 {
		t.Fatalf("handled %v, calls %d, %v", handled, calls, err)
	}
	for i, want := range batch {
		if _, got := receive(); !bytes.Equal(got, want) {
			t.Fatalf("datagram %d: %d bytes", i, len(got))
		}
	}
	if handled, err := b.sendUDPBatchWith(batch, ep, false, nil, farDeadline(), refuse); !handled || err != nil || calls != 1 {
		t.Fatalf("segmentation tried again at once: calls %d, %v", calls, err)
	}
	for range batch {
		receive()
	}

	sink, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sink.Close()
	b2 := newBind(t, Options{UDP: true})
	openBind(t, b2)
	ep2, err := b2.ParseEndpoint("udp://" + sink.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	partial := func(data, _ []byte, _ netip.AddrPort) (int, int, error) { return len(data) / 2, 0, nil }
	if handled, err := b2.sendUDPBatchWith(batch, ep2, false, nil, farDeadline(), partial); !handled || err == nil {
		t.Fatalf("a partial segmented send: handled %v, %v", handled, err)
	}
	sink.SetReadDeadline(time.Now().Add(200 * time.Millisecond))
	if _, _, err := sink.ReadFromUDP(make([]byte, 2048)); err == nil {
		t.Fatal("what may have gone out in part was sent again")
	}
}

// The kernel cuts a segmented send into the datagrams it was made of.
func TestSegmentedSendArrivesAsDatagrams(t *testing.T) {
	sink, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sink.Close()
	sender, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sender.Close()
	var bufs [][]byte
	for i := 0; i < 8; i++ {
		p := bytes.Repeat([]byte{byte(i)}, 1000)
		if i == 7 {
			p = p[:333]
		}
		binary.LittleEndian.PutUint32(p, 4)
		bufs = append(bufs, p)
	}
	fallback, err := gsoSend(bufs, preambleFor(2), sink.LocalAddr().(*net.UDPAddr).AddrPort(), sender.WriteMsgUDPAddrPort)
	if fallback {
		t.Skipf("no UDP segmentation here: %v", err)
	}
	if err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 65536)
	for i, want := range bufs {
		sink.SetReadDeadline(time.Now().Add(2 * time.Second))
		n, _, err := sink.ReadFromUDP(buf)
		if err != nil {
			t.Fatal(err)
		}
		payload, plane, v, err := unwrapDatagram(buf[:n], 1400)
		if err != nil || plane != 0 || v != 2 || !bytes.Equal(payload, want) {
			t.Fatalf("datagram %d: %d bytes, plane %d, version %d, %v", i, n, plane, v, err)
		}
	}
}

func TestBatchWriteGoesOnAfterAShortCount(t *testing.T) {
	msgs := make([]ipv6.Message, 5)
	var calls []int
	sent, err := writeBatch(msgs, func(m []ipv6.Message) (int, error) {
		calls = append(calls, len(m))
		return min(2, len(m)), nil
	})
	if err != nil || sent != 5 || len(calls) != 3 || calls[0] != 5 || calls[1] != 3 || calls[2] != 1 {
		t.Fatalf("sent %d, calls %v, %v", sent, calls, err)
	}
	broken := errors.New("broken")
	sent, err = writeBatch(msgs, func(m []ipv6.Message) (int, error) { return 1, broken })
	if sent != 1 || !errors.Is(err, broken) {
		t.Fatalf("sent %d, %v", sent, err)
	}
	sent, err = writeBatch(msgs, func(m []ipv6.Message) (int, error) { return 0, nil })
	if sent != 0 || !errors.Is(err, ErrBackpressure) {
		t.Fatalf("sent %d, %v", sent, err)
	}
}
