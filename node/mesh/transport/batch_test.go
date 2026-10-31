package transport

import (
	"bytes"
	"crypto/tls"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"testing"
	"time"

	"golang.zx2c4.com/wireguard/conn"
)

func batchSink(t *testing.T, protocol string) (string, string, func() (uint8, []byte)) {
	t.Helper()
	if protocol == "udp" {
		c, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { c.Close() })
		return "udp://" + c.LocalAddr().String(), "", func() (uint8, []byte) {
			t.Helper()
			c.SetReadDeadline(time.Now().Add(2 * time.Second))
			buf := make([]byte, 2048)
			n, _, err := c.ReadFromUDP(buf)
			if err != nil {
				t.Fatal(err)
			}
			data, plane, err := unwrapPlaneUDP(buf[:n], maxDatagram(1400))
			if err != nil {
				t.Fatal(err)
			}
			return plane, data
		}
	}
	var cert *tls.Certificate
	pin := ""
	if protocol == "tls" {
		var err error
		var info Certificate
		cert, info, err = activeCertificate(t.TempDir())
		if err != nil {
			t.Fatal(err)
		}
		pin = info.FingerprintSHA256
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	accepted := make(chan net.Conn, 1)
	done := make(chan struct{})
	go func() {
		defer close(done)
		c, err := listener.Accept()
		if err != nil {
			return
		}
		if cert != nil {
			c = tls.Server(c, &tls.Config{Certificates: []tls.Certificate{*cert}, MinVersion: tls.VersionTLS13})
		}
		accepted <- c
		if err := exchangePreamble(c); err != nil {
			c.Close()
		}
	}()
	var c net.Conn
	t.Cleanup(func() {
		listener.Close()
		if c != nil {
			c.Close()
		}
		<-done
	})
	return protocol + "://" + listener.Addr().String(), pin, func() (uint8, []byte) {
		t.Helper()
		if c == nil {
			select {
			case c = <-accepted:
			case <-time.After(2 * time.Second):
				t.Fatal("sink did not accept")
			}
			<-done
		}
		c.SetReadDeadline(time.Now().Add(2 * time.Second))
		n, plane, err := readFrameHeader(c, maxDatagram(1400))
		if err != nil {
			t.Fatal(err)
		}
		data := make([]byte, n)
		if _, err := io.ReadFull(c, data); err != nil {
			t.Fatal(err)
		}
		return plane, data
	}
}

func TestBatchSendPreservesOrderAndContinuesAfterError(t *testing.T) {
	for _, protocol := range []string{"udp", "tcp", "tls"} {
		for _, plane := range []uint8{0, 1} {
			name := protocol + "/data"
			if plane == 1 {
				name = protocol + "/control"
			}
			t.Run(name, func(t *testing.T) {
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
				var sender conn.Bind = b
				if plane == 1 {
					sender = b.ControlBind()
				}
				packets := make([][]byte, 128)
				for i := range packets {
					packets[i] = make([]byte, 16)
					binary.BigEndian.PutUint32(packets[i], uint32(i))
				}
				if err := sender.Send(packets, ep); err != nil {
					t.Fatal(err)
				}
				for i, want := range packets {
					gotPlane, got := receive()
					if gotPlane != plane || !bytes.Equal(got, want) {
						t.Fatalf("packet %d: plane=%d data=%x", i, gotPlane, got)
					}
				}
				bad := make([]byte, maxDatagram(1400)+1)
				if err := sender.Send([][]byte{packets[0], bad, packets[1]}, ep); !errors.Is(err, ErrFrame) {
					t.Fatalf("batch error: %v", err)
				}
				for _, want := range packets[:2] {
					gotPlane, got := receive()
					if gotPlane != plane || !bytes.Equal(got, want) {
						t.Fatal("valid packet lost after failed packet")
					}
				}
				if err := sender.Send(nil, ep); err != nil {
					t.Fatal("empty batch", err)
				}
			})
		}
	}
}
