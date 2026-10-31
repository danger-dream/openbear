//go:build !unix && !windows

package node

import (
	"context"
	"encoding/json"
	"errors"
)

type jobStore struct{}

func (a *Agent) jobs() *jobStore { return &jobStore{} }

func (s *jobStore) handle(string, json.RawMessage) (any, error) {
	return nil, errors.New("unsupported_platform")
}

func RunJob(context.Context, string) error { return errors.New("unsupported_platform") }
