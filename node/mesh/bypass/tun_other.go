//go:build !windows && !darwin

package bypass

// Linux has the marks instead (see package bypass); elsewhere nothing is bound.
const tunSupported = false

func physicalInterface() int { return 0 }

func bindInterface(uintptr, string, bool, int) error { return nil }
