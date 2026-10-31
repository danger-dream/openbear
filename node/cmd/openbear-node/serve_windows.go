//go:build windows

package main

import (
	"context"
	"errors"
	"log"
	"os"
	"os/exec"
	"path/filepath"

	"golang.org/x/sys/windows/svc"

	"openbear/node"
	"openbear/node/mesh"
)

const logLimit = 1 << 20

// serve runs the node as the Windows service when the service manager
// started it. After an update the service exits with an error and its
// recovery settings start the new binary; run by hand, it starts a copy.
func serve(ctx context.Context, _ func(), configPath string, logger *log.Logger, run func(context.Context) error) error {
	if ok, _ := svc.IsWindowsService(); !ok {
		err := run(ctx)
		if !errors.Is(err, node.ErrRestart) {
			return err
		}
		exe, err := os.Executable()
		if err != nil {
			return err
		}
		logger.Print("updated; restarting")
		next := exec.Command(exe, os.Args[1:]...)
		next.Stdout, next.Stderr = os.Stdout, os.Stderr
		return next.Start()
	}
	path := filepath.Join(filepath.Dir(configPath), "node.log")
	if info, err := os.Stat(path); err == nil && info.Size() > logLimit {
		os.Remove(path)
	}
	if f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600); err == nil {
		defer f.Close()
		logger.SetOutput(f)
	}
	return svc.Run(node.ServiceName, &service{ctx: ctx, run: run, logger: logger})
}

func meshRun(_ context.Context, dir string) error { return mesh.RunService(dir) }

type service struct {
	ctx    context.Context
	run    func(context.Context) error
	logger *log.Logger
}

func (s *service) Execute(_ []string, requests <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending}
	ctx, cancel := context.WithCancel(s.ctx)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- s.run(ctx) }()
	status <- svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	for {
		select {
		case r := <-requests:
			switch r.Cmd {
			case svc.Interrogate:
				status <- r.CurrentStatus
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending}
				cancel()
			}
		case err := <-done:
			if errors.Is(err, node.ErrRestart) {
				s.logger.Print("updated; restarting")
				return false, 1
			}
			if err != nil && ctx.Err() == nil {
				s.logger.Print(err)
				return false, 1
			}
			return false, 0
		}
	}
}
