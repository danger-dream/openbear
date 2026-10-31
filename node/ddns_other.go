//go:build !linux && !darwin && !windows

package node

import "net"

// ddnsV6Flags knows no flags here: every IPv6 address counts as stable.
func ddnsV6Flags() (map[ddnsAddrKey]ddnsAddrFlags, error) { return nil, nil }

func ddnsBindDevice(*net.Dialer, string) bool { return false }
