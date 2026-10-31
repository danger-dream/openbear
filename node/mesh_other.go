//go:build !linux && !darwin && !windows

package node

import (
	"context"
	"encoding/json"
	"errors"
)

type meshHost struct{}

func (a *Agent) meshCall(context.Context, string, json.RawMessage) (any, error) {
	return nil, errors.New("unsupported_platform")
}

func (a *Agent) addMeshReport(map[string]any) func() { return func() {} }

func (a *Agent) platformMeshLink() meshLink { return nil }

func (a *Agent) resumeMeshChange()                  {}
func (a *Agent) lockMeshAddresses() (func(), error) { return func() {}, nil }
