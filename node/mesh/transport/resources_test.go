package transport

import "testing"

// Both WG devices' message storage stays within what it was with batches of
// one, 128 and 248 full-size buffers (8 and 15.5 MiB): the data device holds
// more, shorter ones for batches of 32 and, with large frames, a pool of
// large ones, beside which the bind and the TUN keep spare large buffers;
// the control device keeps its own.
func TestTwoPlaneFixedBufferBudget(t *testing.T) {
	const full = 64 << 10
	for _, c := range []struct {
		profile string
		large   bool
	}{{"member", false}, {"relay", false}, {"member", true}, {"relay", true}} {
		profile := c.profile
		name := profile
		if c.large {
			name += "/large"
		}
		t.Run(name, func(t *testing.T) {
			b := newBind(t, Options{ResourceProfile: profile, LargeFrames: c.large})
			data, control := b.DeviceLimits(), b.ControlBind().DeviceLimits()
			beforeData, beforeControl := data, control
			for i := 0; i < data.MaxPeers; i++ {
				var d, c [32]byte
				d[0], d[1], c[0], c[1] = byte(i+1), 1, byte(i+1), 2
				if err := b.RegisterPeer(PeerIdentity{ID: string(rune(i + 1)), DataKey: d, ControlKey: c}); err != nil {
					t.Fatal(err)
				}
			}
			if b.DeviceLimits() != beforeData || b.ControlBind().DeviceLimits() != beforeControl {
				t.Fatal("budgets grew with peers")
			}
			bytes := (data.TxBuffers+data.RxBuffers)*data.MessageBufferSize + (data.LargeTxBuffers+data.LargeRxBuffers)*data.LargeBufferSize +
				(data.KeepaliveBuffers+control.TxBuffers+control.RxBuffers+control.KeepaliveBuffers)*full
			if c.large {
				// The bind's spares and the TUN's, of the same size (package
				// mesh checks that).
				bytes += 2 * LargeSpares * largeMessageBuffer
			}
			if large := data.LargeTxBuffers > 0; large != c.large || control.LargeTxBuffers != 0 ||
				large && (data.LargeBufferSize != largeMessageBuffer || data.LargeBatchSize != 8 || data.LargeRxBuffers <= data.LargeBatchSize) {
				t.Fatalf("large pool: data=%+v control=%+v", data, control)
			}
			before := 128 * full
			if profile == "relay" {
				before = 248 * full
			}
			if bytes > before || data.MessageBufferSize != 2048 || data.BatchSize != 32 || b.BatchSize() != 32 || control.BatchSize != 1 || control.MessageBufferSize != 0 ||
				data.PriorityRxBuffers != data.BatchSize+1 || data.PriorityRxBuffers > data.RxBuffers || data.QueueDepth != 4 || control.QueueDepth != 4 {
				t.Fatalf("unexpected budgets: data=%+v control=%+v", data, control)
			}
			t.Logf("%s: both WG devices reserve %.2f MiB message storage for any allowed peer count, %.2f MiB before", profile, float64(bytes)/(1<<20), float64(before)/(1<<20))
		})
	}
}

// A network MTU above what the 2048-byte buffers hold keeps full-size ones.
func TestLargeMTUKeepsFullBuffers(t *testing.T) {
	b := newBind(t, Options{MTU: 9000})
	if l := b.DeviceLimits(); l.MessageBufferSize != 0 || l.BatchSize != 8 || l.TxBuffers != 32 || b.BatchSize() != 8 {
		t.Fatalf("limits %+v", l)
	}
}
