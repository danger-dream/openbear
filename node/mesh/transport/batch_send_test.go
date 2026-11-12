package transport

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"runtime"
	"testing"
	"time"
)

func farDeadline() time.Time { return time.Now().Add(sendTimeout) }

// A data batch of the device's size goes out in one call on every
// transport, in order, and the other end reads it as separate frames or
// datagrams.
func TestDataBatchGoesOutWhole(t *testing.T) {
	for _, protocol := range []string{"udp", "tcp", "tls"} {
		t.Run(protocol, func(t *testing.T) {
			uri, pin, receive := batchSink(t, protocol)
			b := newBind(t, Options{})
			openBind(t, b)
			if pin != "" {
				b.SetPins(uri, []string{pin})
			}
			ep, err := b.ParseEndpoint(uri)
			if err != nil {
				t.Fatal(err)
			}
			if protocol != "udp" {
				pathReady(t, b, uri)
			}
			packets := make([][]byte, b.BatchSize())
			for i := range packets {
				// The last shorter, as the tail of a TCP burst.
				packets[i] = bytes.Repeat([]byte{byte(i)}, 1200)
				if i == len(packets)-1 {
					packets[i] = packets[i][:300]
				}
				binary.LittleEndian.PutUint32(packets[i], 4)
			}
			var handled bool
			if protocol == "udp" {
				handled, err = b.sendUDPBatch(packets, ep, false, nil, farDeadline())
			} else {
				handled, err = b.sendStreamBatch(packets, ep, false, nil, farDeadline())
			}
			if protocol == "udp" && runtime.GOOS != "linux" {
				t.Skip("UDP goes datagram by datagram here")
			}
			if !handled || err != nil {
				t.Fatalf("handled %v, %v", handled, err)
			}
			for i, want := range packets {
				plane, got := receive()
				if plane != 0 || !bytes.Equal(got, want) {
					t.Fatalf("packet %d: plane %d, %d bytes", i, plane, len(got))
				}
			}
		})
	}
}

// A batch holding a handshake goes frame by frame, which sends handshakes
// over a companion.
func TestBatchWithHandshakeGoesFrameByFrame(t *testing.T) {
	uri, _, receive := batchSink(t, "tcp")
	b := newBind(t, Options{})
	openBind(t, b)
	ep, err := b.ParseEndpoint(uri)
	if err != nil {
		t.Fatal(err)
	}
	pathReady(t, b, uri)
	initiation := make([]byte, 148)
	binary.LittleEndian.PutUint32(initiation, 1)
	batch := [][]byte{packet4(), initiation}
	if handled, _ := b.sendStreamBatch(batch, ep, false, nil, farDeadline()); handled {
		t.Fatal("a batch with a handshake was written whole")
	}
	if err := b.Send(batch, ep); err != nil {
		t.Fatal(err)
	}
	for i, want := range batch {
		if plane, got := receive(); plane != 0 || !bytes.Equal(got, want) {
			t.Fatalf("frame %d: plane %d, %d bytes", i, plane, len(got))
		}
	}
}

// A short vectored write is an error, after which the stream is closed:
// what went out whole is counted, the frame cut off is not, and nothing is
// written twice.
func TestFrameBatchShortWrite(t *testing.T) {
	bufs := [][]byte{bytes.Repeat([]byte{1}, 100), bytes.Repeat([]byte{2}, 100), bytes.Repeat([]byte{3}, 100)}
	var out bytes.Buffer
	calls := 0
	complete, err := writeFrameBatch(bufs, func(v net.Buffers) (int64, error) {
		calls++
		var all bytes.Buffer
		v.WriteTo(&all)
		out.Write(all.Bytes()[:150])
		return 150, nil
	})
	if !errors.Is(err, io.ErrShortWrite) || complete != 1 || calls != 1 {
		t.Fatalf("complete %d, calls %d, %v", complete, calls, err)
	}
	n, err := readFrame(&out, make([]byte, 200))
	if err != nil || n != 100 {
		t.Fatalf("first frame %d %v", n, err)
	}
	broken := errors.New("broken pipe")
	complete, err = writeFrameBatch(bufs, func(net.Buffers) (int64, error) { return 206, broken })
	if !errors.Is(err, broken) || complete != 2 {
		t.Fatalf("complete %d, %v", complete, err)
	}
	complete, err = writeFrameBatch(bufs, func(v net.Buffers) (int64, error) { return v.WriteTo(io.Discard) })
	if err != nil || complete != 3 {
		t.Fatalf("complete %d, %v", complete, err)
	}
}

// A single frame on plain TCP or TLS reads back as written.
func TestSingleFrameWriters(t *testing.T) {
	for _, protocol := range []string{"tcp", "tls"} {
		t.Run(protocol, func(t *testing.T) {
			uri, pin, receive := batchSink(t, protocol)
			b := newBind(t, Options{})
			openBind(t, b)
			if pin != "" {
				b.SetPins(uri, []string{pin})
			}
			ep, err := b.ParseEndpoint(uri)
			if err != nil {
				t.Fatal(err)
			}
			pathReady(t, b, uri)
			want := bytes.Repeat([]byte{7}, 1000)
			binary.LittleEndian.PutUint32(want, 4)
			if err := b.Send([][]byte{want}, ep); err != nil {
				t.Fatal(err)
			}
			control := []byte("control frame")
			if err := b.ControlBind().Send([][]byte{control}, ep); err != nil {
				t.Fatal(err)
			}
			if plane, got := receive(); plane != 0 || !bytes.Equal(got, want) {
				t.Fatalf("data: plane %d, %d bytes", plane, len(got))
			}
			if plane, got := receive(); plane != 1 || !bytes.Equal(got, control) {
				t.Fatalf("control: plane %d, %q", plane, got)
			}
		})
	}
}
