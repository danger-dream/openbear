package transport

import "testing"

func TestTwoPlaneFixedBufferBudget(t *testing.T) {
	for _, profile := range []string{"member", "relay"} {
		t.Run(profile, func(t *testing.T) {
			b := newBind(t, Options{ResourceProfile: profile})
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
			count := data.TxBuffers + data.RxBuffers + data.KeepaliveBuffers + control.TxBuffers + control.RxBuffers + control.KeepaliveBuffers
			want := 128
			if profile == "relay" {
				want = 248
			}
			if count != want || data.BatchSize != 1 || control.BatchSize != 1 || data.QueueDepth != 4 || control.QueueDepth != 4 {
				t.Fatalf("unexpected budgets: data=%+v control=%+v", data, control)
			}
			t.Logf("%s: both WG devices reserve at most %d x 64 KiB = %.2f MiB message storage for any allowed peer count", profile, count, float64(count)/16)
		})
	}
}
