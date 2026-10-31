//go:build !unix && !windows

package node

import (
	"encoding/json"
	"errors"
)

type uploads struct{}

func newUploads() *uploads { return &uploads{} }

func (u *uploads) handle(string, json.RawMessage) (any, error) {
	return nil, errors.New("unsupported_platform")
}

func (u *uploads) edit(json.RawMessage) (any, error) {
	return nil, errors.New("unsupported_platform")
}

func fileError(err error) error { return err }
