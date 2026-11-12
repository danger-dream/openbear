//go:build !linux

package mesh

// HostRoutes is nil where this machine's routes are not read: the
// controller then knows only the networks on its interfaces.
func HostRoutes(dir, iface string) []HostRoute { return nil }
