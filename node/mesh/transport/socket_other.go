//go:build !linux && !windows

package transport

func setNotSent(int) error { return nil }

// allowFragments: macOS sends UDP without DF unless asked to.
func allowFragments(int) {}
