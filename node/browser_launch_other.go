//go:build !linux && !darwin && !windows

package node

import (
	"context"
	"errors"
	"path/filepath"
)

var headlessArgs = []string{"--headless=new"}

func browserDataDir(config string) string { return filepath.Join(filepath.Dir(config), "browser") }

func browserCandidates() []string { return nil }

func browserCanRun(string) bool { return true }

func launchBrowser(context.Context, string, string) (*launched, error) {
	return nil, errors.New("starting a browser is not supported on this system; set an existing browser endpoint")
}

func checkLibraries(string) error { return nil }
