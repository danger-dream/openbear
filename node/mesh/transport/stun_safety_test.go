package transport

import (
	"encoding/binary"
	"net/netip"
	"testing"
)

func malformedSTUN(body []byte) []byte {
	p := make([]byte, 20)
	binary.BigEndian.PutUint16(p[0:2], stunResponse)
	binary.BigEndian.PutUint16(p[2:4], uint16(len(body)))
	binary.BigEndian.PutUint32(p[4:8], stunCookie)
	return append(p, body...)
}

func TestSTUNRejectsTruncatedPaddingWithoutConsumingWaiter(t *testing.T) {
	for _, body := range [][]byte{
		{0x80, 0x22, 0, 1, 1}, // original panic: value present, three padding bytes missing
		{0x80, 0x22, 0, 2, 1, 2},
		{0x80, 0x22, 0, 3, 1, 2, 3},
		{0x80, 0x22, 0, 5, 1, 2, 3, 4}, // aligned message, truncated value/span
	} {
		if _, err := parseSTUN(malformedSTUN(body)); err == nil {
			t.Fatal("truncation accepted", body)
		}
	}
	from := netip.MustParseAddrPort("203.0.113.1:3478")
	var tx [12]byte
	w := &stunWaiter{server: from, reply: make(chan netip.AddrPort, 1)}
	b := &Bind{stunWait: map[[12]byte]*stunWaiter{tx: w}}
	b.stunReply(malformedSTUN([]byte{0x80, 0x22, 0, 5, 1, 2, 3, 4}), from)
	if b.stunWait[tx] != w || len(w.reply) != 0 {
		t.Fatal("invalid packet consumed pending transaction")
	}
	want := netip.MustParseAddrPort("198.51.100.1:12345")
	valid := stunAnswer(tx[:], want, true)
	// A valid mapped address followed by an invalid attribute is still invalid.
	badTail := append(append([]byte{}, valid...), 0x80, 0x22, 0, 5)
	binary.BigEndian.PutUint16(badTail[2:4], uint16(len(badTail)-20))
	if _, err := parseSTUN(badTail); err == nil {
		t.Fatal("early XOR return skipped malformed tail")
	}
	if !b.stunReply(valid, from) || <-w.reply != want {
		t.Fatal("valid retry lost")
	}
}

func FuzzParseSTUNNeverPanics(f *testing.F) {
	f.Add([]byte{})
	f.Add(malformedSTUN([]byte{0x80, 0x22, 0, 1, 1}))
	f.Add(stunAnswer(make([]byte, 12), netip.MustParseAddrPort("198.51.100.1:12345"), true))
	f.Fuzz(func(t *testing.T, p []byte) { _, _ = parseSTUN(p) })
}
