package mesh

import (
	"context"
	"os"
	"os/signal"
	"syscall"
)

// runForeground runs until SIGINT or SIGTERM, which is how systemd and
// launchd stop the service.
func runForeground(dir string) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return Run(ctx, dir)
}
