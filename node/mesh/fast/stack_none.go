//go:build linux && !fastnetstack

package fast

import "errors"

// tunCapture is capture in the data plane, which Linux leaves to nftables:
// without it connections go over the mesh.
type tunCapture struct{}

func newTunCapture(*Service, func([]byte), *udpSessionTable) (*tunCapture, error) {
	return nil, errors.New("capture in the data plane is not built for Linux")
}

func (*tunCapture) close()                                    {}
func (*tunCapture) status() *CaptureStatus                    { return &CaptureStatus{Mode: "tun"} }
func (*tunCapture) take([]byte, int, byte, bool, string) bool { return false }
