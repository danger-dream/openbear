//go:build unix && !linux && !darwin

package node

import "errors"

func startShell(int, int) (console, error) { return nil, errors.New("unsupported_platform") }
