package transport

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"testing"

	"openbear/node/mesh/wire"
)

// dialPreamble opens a raw stream to a bind as this build's dialing end
// does, and acceptPreamble answers one from a bind as its accepting end
// does, with a token no companion is given.
func dialPreamble(c net.Conn) error {
	v, err := exchangeVersions(c, wire.Local())
	if err == nil && v >= companionVersion {
		_, err = dialHello(c)
	}
	return err
}

func acceptPreamble(c net.Conn) error {
	v, err := exchangeVersions(c, wire.Local())
	if err != nil || v < companionVersion {
		return err
	}
	var hello [1]byte
	if _, err = io.ReadFull(c, hello[:]); err != nil {
		return err
	}
	if hello[0] != helloStream {
		return ErrFrame
	}
	return writeAll(c, make([]byte, 32))
}

type shortReader struct{ io.Reader }

func (r shortReader) Read(p []byte) (int, error) { return r.Reader.Read(p[:min(len(p), 1)]) }

func TestFrames(t *testing.T) {
	var wire bytes.Buffer
	wire.WriteString(preamble)
	for _, p := range [][]byte{{1}, bytes.Repeat([]byte{9}, maxDatagram(1400))} {
		if err := writeFrame(&wire, p, maxDatagram(1400)); err != nil {
			t.Fatal(err)
		}
	}
	r := shortReader{&wire}
	if err := readPreamble(r); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, maxDatagram(1400))
	if n, err := readFrame(r, buf); err != nil || n != 1 || buf[0] != 1 {
		t.Fatalf("short frame %d %v", n, err)
	}
	if n, err := readFrame(r, buf); err != nil || n != len(buf) || !bytes.Equal(buf, bytes.Repeat([]byte{9}, len(buf))) {
		t.Fatalf("large frame %d %v", n, err)
	}
	if _, err := readFrame(r, buf); err != io.EOF {
		t.Fatalf("tail: %v", err)
	}
	for _, raw := range [][]byte{{0, 0, 0}, {0, 1, 0}, {0, 2, 1, 3}, {255, 255, 0}} {
		if _, err := readFrame(bytes.NewReader(raw), buf); !errors.Is(err, ErrFrame) {
			t.Fatalf("accepted invalid frame %x: %v", raw, err)
		}
	}
	var h [3]byte
	binary.BigEndian.PutUint16(h[:2], 5)
	if _, err := readFrame(bytes.NewReader(append(h[:], 1)), buf); !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("truncated: %v", err)
	}
	if err := readPreamble(bytes.NewBufferString("OBM1")); !errors.Is(err, ErrFrame) {
		t.Fatal(err)
	}
	if _, err := unwrapUDP([]byte(preamble+"\x00payload"), 1400); err != nil {
		t.Fatal(err)
	}
	// A datagram of protocol 1, which this build no longer speaks.
	if _, err := unwrapUDP([]byte("OBM2\x00payload"), 1400); err == nil {
		t.Fatal("accepted a protocol 1 datagram")
	}
	for _, raw := range [][]byte{[]byte(preamble), []byte(preamble + "\x01a"), []byte("OBM1\x00a"), []byte(preamble + "\x00abc")} {
		if _, err := unwrapUDP(raw, 2); err == nil {
			t.Fatalf("accepted UDP %x", raw)
		}
	}
}
