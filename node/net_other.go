//go:build !linux && !darwin && !windows

package node

import (
	"context"
	"errors"
)

type probeManager struct{}

func (a *Agent) netCall(context.Context, envelope) (any, error) {
	return nil, errors.New("unsupported_method")
}
