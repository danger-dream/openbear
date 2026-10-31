package mesh

import (
	"context"
	"log"
	"os"
	"path/filepath"

	"golang.org/x/sys/windows/svc"
)

// ServiceName is the Windows service the node registers for the data plane.
const ServiceName = "openbear-mesh"

type service struct{ dir string }

func (s service) Execute(_ []string, req <-chan svc.ChangeRequest, status chan<- svc.Status) (bool, uint32) {
	status <- svc.Status{State: svc.StartPending}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(ctx, s.dir) }()
	status <- svc.Status{State: svc.Running, Accepts: svc.AcceptStop | svc.AcceptShutdown}
	for {
		select {
		case err := <-done:
			if err != nil {
				log.Print(err)
				return false, 1
			}
			return false, 0
		case r := <-req:
			switch r.Cmd {
			case svc.Interrogate:
				status <- r.CurrentStatus
			case svc.Stop, svc.Shutdown:
				status <- svc.Status{State: svc.StopPending}
				cancel()
				if err := <-done; err != nil {
					log.Print(err)
				}
				return false, 0
			}
		}
	}
}

// RunService runs the data plane under the Service Control Manager, or in
// the foreground until interrupted when started from a console. A service
// has no console, so it logs to mesh.log in dir.
func RunService(dir string) error {
	inService, err := svc.IsWindowsService()
	if err != nil || !inService {
		return runForeground(dir)
	}
	path := filepath.Join(dir, "mesh.log")
	if info, err := os.Stat(path); err == nil && info.Size() > 4<<20 {
		os.Rename(path, path+".1")
	}
	if f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600); err == nil {
		log.SetOutput(f)
		defer f.Close()
	}
	return svc.Run(ServiceName, service{dir})
}
