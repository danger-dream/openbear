package mesh

import (
	"bytes"
	"errors"
	"os"
	"testing"
	"time"
)

// The existing LargeReader copies into the buffers supplied by WireGuard.
// Exercise both the payload and the reserved header/tail, including an
// oversize entry before a valid one, without assuming pooled ownership.
func TestLargeReaderCopyBoundaryAndOversize(t *testing.T) {
	mt, _, _, _ := largeTUN(t, false, nil)
	large := bytes.Repeat([]byte{0x35}, 8193)
	want := bytes.Clone(large)
	mt.large <- queued{b: bytes.Repeat([]byte{0x44}, 9472)}
	mt.large <- queued{b: large[:len(large):len(large)]}
	buf := bytes.Repeat([]byte{0xab}, 9472)
	sizes := []int{0, 0}
	bufs := [][]byte{buf, make([]byte, 9472)}
	n, err := mt.ReadLarge(bufs, sizes, 16, nil)
	if err != nil || n != 1 || sizes[0] != len(want) || mt.stats.DroppedQueue.Load() != 1 {
		t.Fatalf("n=%d sizes=%v dropped=%d err=%v", n, sizes, mt.stats.DroppedQueue.Load(), err)
	}
	if !bytes.Equal(buf[16:16+len(want)], want) || !bytes.Equal(buf[:16], bytes.Repeat([]byte{0xab}, 16)) || buf[16+len(want)] != 0xab {
		t.Fatal("payload or head/tail changed")
	}
	large[0] ^= 0xff
	if !bytes.Equal(buf[16:16+len(want)], want) {
		t.Fatal("output aliases discarded source")
	}
}

func TestLargeReaderCloseUnblocks(t *testing.T) {
	mt, _, _, _ := largeTUN(t, false, nil)
	done := make(chan error, 1)
	go func() { _, err := mt.ReadLarge([][]byte{make([]byte, 9472)}, []int{0}, 16, nil); done <- err }()
	mt.sched.close()
	select {
	case err := <-done:
		if !errors.Is(err, os.ErrClosed) {
			t.Fatalf("got %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("large reader did not unblock on close")
	}
}
