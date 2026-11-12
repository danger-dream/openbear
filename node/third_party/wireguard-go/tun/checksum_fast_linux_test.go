//go:build linux

package tun

import (
	"math/rand"
	"testing"
)

func TestTCPFastChecksumReference(t *testing.T) {
	r := rand.New(rand.NewSource(8451))
	b := make([]byte, 65536+32)
	for i := range b {
		b[i] = byte(r.Intn(256))
	}
	lengths := []int{0, 1, 2, 3, 7, 8, 15, 16, 31, 32, 63, 64, 127, 128, 255, 256, 257, 511, 512, 8999, 9000, 9001, 32716, 65535}
	initials := []uint64{0, 1, 0xffff, 0xffffffff, 0xffffffffffffffff, 0x123456789abcdef0}
	for offset := 0; offset < 32; offset++ {
		for _, n := range lengths {
			for _, initial := range initials {
				got, want := tcpPacketChecksum(b[offset:offset+n], initial), checksum(b[offset:offset+n], initial)
				if got != want {
					t.Fatalf("offset %d length %d initial %x got %x want %x", offset, n, initial, got, want)
				}
			}
		}
	}
	for i := 0; i < 20000; i++ {
		offset := r.Intn(32)
		n := r.Intn(65536)
		initial := r.Uint64()
		got, want := tcpPacketChecksum(b[offset:offset+n], initial), checksum(b[offset:offset+n], initial)
		if got != want {
			t.Fatalf("random %d offset %d length %d initial %x got %x want %x", i, offset, n, initial, got, want)
		}
	}
}

func BenchmarkTCPPacketChecksum(b *testing.B) {
	data := make([]byte, 8950)
	r := rand.New(rand.NewSource(123))
	r.Read(data)
	b.Run("legacy", func(b *testing.B) {
		for i := 0; i < b.N; i++ {
			checksum(data, 12345)
		}
	})
	b.Run("candidate", func(b *testing.B) {
		for i := 0; i < b.N; i++ {
			tcpPacketChecksum(data, 12345)
		}
	})
}
