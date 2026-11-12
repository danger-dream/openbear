//go:build linux

package tun

import (
	"bytes"
	"encoding/binary"
	"testing"
)

// groTestTCP is a 1300-byte TCP segment to 192.0.2.dst in a 2048-byte
// buffer, as a device's fixed pool hands them over.
func groTestTCP(seq uint32, dst byte) []byte {
	const n = 1300
	b := make([]byte, virtioNetHdrLen+40+n, 2048)
	p := b[virtioNetHdrLen:]
	p[0] = 0x45
	binary.BigEndian.PutUint16(p[2:4], uint16(len(p)))
	p[8] = 64
	p[9] = 6
	copy(p[12:16], []byte{192, 0, 2, 1})
	copy(p[16:20], []byte{192, 0, 2, dst})
	binary.BigEndian.PutUint16(p[10:12], ^checksum(p[:20], 0))
	h := p[20:]
	binary.BigEndian.PutUint16(h[:2], 1000)
	binary.BigEndian.PutUint16(h[2:4], 2000)
	binary.BigEndian.PutUint32(h[4:8], seq)
	binary.BigEndian.PutUint32(h[8:12], 1)
	h[12] = 0x50
	h[13] = 0x10
	binary.BigEndian.PutUint16(h[14:16], 3000)
	ps := pseudoHeaderChecksumNoFold(6, p[12:16], p[16:20], uint16(len(h)))
	binary.BigEndian.PutUint16(h[16:18], ^checksum(h, ps))
	return b
}

func TestGROWorkspaceCoalescesShortBuffers(t *testing.T) {
	in := [][]byte{groTestTCP(1, 2), groTestTCP(1, 3), groTestTCP(1301, 2), groTestTCP(1301, 3)}
	original := make([][]byte, len(in))
	for i := range in {
		original[i] = bytes.Clone(in[i])
	}
	var writes []int
	if err := handleGRO(append([][]byte(nil), in...), virtioNetHdrLen, newTCPGROTable(), newUDPGROTable(), false, &writes); err != nil || len(writes) != 4 {
		t.Fatalf("short buffers: %d writes, err %v; want 4, no coalescing", len(writes), err)
	}
	var n NativeTun
	wide := n.groWorkspace(in)
	if cap(wide[0]) != groSlot || &wide[0][0] == &in[0][0] {
		t.Fatal("workspace slot is not a full-size copy")
	}
	writes = nil
	if err := handleGRO(wide, virtioNetHdrLen, newTCPGROTable(), newUDPGROTable(), false, &writes); err != nil || len(writes) != 2 {
		t.Fatalf("workspace: %d writes, err %v; want one per flow", len(writes), err)
	}
	for i := range in {
		if !bytes.Equal(in[i], original[i]) {
			t.Fatal("caller's buffers changed")
		}
	}
	reused := n.groWorkspace(in)
	if len(reused) != 4 || len(n.groStorage) != groBatch*groSlot || !bytes.Equal(reused[0], in[0]) {
		t.Fatal("workspace not reused")
	}
	if got := n.groWorkspace(make([][]byte, groBatch+1)); len(got) != groBatch+1 || n.groStorage == nil {
		t.Fatal("a batch larger than the workspace must pass through")
	}
	full := [][]byte{make([]byte, 100, groSlot), make([]byte, 100, groSlot)}
	if got := n.groWorkspace(full); &got[0][0] != &full[0][0] {
		t.Fatal("buffers that can grow must not be copied")
	}
}
