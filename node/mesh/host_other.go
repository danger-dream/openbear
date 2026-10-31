//go:build !linux && !darwin && !windows

package mesh

import (
	"errors"

	"golang.zx2c4.com/wireguard/tun"
)

func createTUN(dir string, mtu int) (tun.Device, error) {
	return nil, errors.New("the mesh data plane does not support this platform")
}

func newHost(string, string, tun.Device) hostNet { return nil }

func residentBytes() uint64 { return 0 }
