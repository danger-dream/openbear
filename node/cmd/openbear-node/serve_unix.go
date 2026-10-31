//go:build !windows

package main

import (
	"context"
	"errors"
	"log"
	"os"
	"syscall"

	"openbear/node"
	"openbear/node/mesh"
)

// serve runs the node; after an update it execs the new binary in place, so
// systemd or launchd keep supervising the same process.
func serve(ctx context.Context, stop func(), _ string, logger *log.Logger, run func(context.Context) error) error {
	err := run(ctx)
	if !errors.Is(err, node.ErrRestart) {
		return err
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	logger.Print("updated; restarting")
	stop()
	return syscall.Exec(exe, os.Args, os.Environ())
}

func meshRun(ctx context.Context, dir string) error { return mesh.Run(ctx, dir) }
