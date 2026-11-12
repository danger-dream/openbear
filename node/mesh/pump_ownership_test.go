package mesh

import (
	"bytes"
	"encoding/binary"
	"net/netip"
	"testing"
)

// Every TUN read overwrites its slot. Packets already in the scheduler must
// retain their own bytes, across both transferred and copied read slots.
func TestPumpOwnsLargeReadSlots(t *testing.T) {
	_, ct, s, self := largeTUN(t, false, map[string]bool{"100.96.0.12": true})
	dst := netip.MustParseAddr("100.96.0.12")
	var want [][]byte
	for i, size := range []int{largeInner, 8193, 8192, largeInner} {
		p := dfPacket(self, dst, protoTCP, size, true)
		binary.BigEndian.PutUint16(p[4:6], uint16(i+1))
		p[len(p)-1] = byte(i + 1)
		want = append(want, p)
		ct.Outbound <- p
	}
	waitLarge(t, func() bool { return s.queuedBytes() == 2*largeInner+8193+8192 })
	for i, expected := range want {
		got := s.tryDequeue()
		if !bytes.Equal(got, expected) {
			t.Fatalf("packet %d was changed or reordered", i)
		}
	}
}

// Under queue pressure, the normal scheduler still drops the oldest packets;
// a later TUN read must not alter the remaining large packets.
func TestPumpLargeSlotsQueuePressure(t *testing.T) {
	mt, ct, s, self := largeTUN(t, false, map[string]bool{"100.96.0.12": true})
	dst := netip.MustParseAddr("100.96.0.12")
	for i := 0; i < 30; i++ {
		p := dfPacket(self, dst, protoTCP, largeInner, true)
		binary.BigEndian.PutUint16(p[4:6], uint16(i))
		p[len(p)-1] = byte(i)
		ct.Outbound <- p
	}
	waitLarge(t, func() bool { return mt.stats.DroppedQueue.Load() == uint64(30-s.flowBytes/largeInner) })
	last := -1
	for p := s.tryDequeue(); p != nil; p = s.tryDequeue() {
		i := int(binary.BigEndian.Uint16(p[4:6]))
		if i <= last || p[len(p)-1] != byte(i) {
			t.Fatalf("changed or reordered large packet %d after %d", i, last)
		}
		last = i
	}
}

func TestTakeTUNPacketTransfersOnlyLargeSlots(t *testing.T) {
	for _, size := range []int{1400, 8192, 8193, largeInner} {
		bufs := [][]byte{make([]byte, readHeadroom+readBufSize)}
		original := &bufs[0][readHeadroom]
		bufs[0][readHeadroom] = 73
		got := takeTUNPacket(bufs, 0, size)
		if size > 8192 {
			if &got[0] != original || &bufs[0][readHeadroom] == original {
				t.Fatalf("%d: slot was not transferred and replaced", size)
			}
			// A separately allocated copy would occupy the same size class.
			copyClass := cap(append([]byte(nil), make([]byte, size)...))
			slotClass := cap(append([]byte(nil), make([]byte, readHeadroom+readBufSize)...))
			if copyClass != slotClass {
				t.Fatalf("%d: copy class %d, slot class %d", size, copyClass, slotClass)
			}
		} else if &got[0] == original || &bufs[0][readHeadroom] != original {
			t.Fatalf("%d: small packet did not keep the read slot", size)
		}
		bufs[0][readHeadroom] = 5
		if got[0] != 73 {
			t.Fatalf("%d: packet aliases the next read", size)
		}
	}
}
