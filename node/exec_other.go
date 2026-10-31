//go:build !unix && !windows

package node

import (
	"context"
	"encoding/json"
	"errors"
)

func Exec(context.Context, json.RawMessage) (ExecResult, error) {
	return ExecResult{}, errors.New("unsupported_platform")
}
