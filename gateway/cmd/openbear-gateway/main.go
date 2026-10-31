package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"openbear/gateway"
)

func main() {
	if e := run(); e != nil {
		fmt.Fprintln(os.Stderr, "openbear-gateway:", e)
		os.Exit(1)
	}
}
func run() error {
	configPath := flag.String("config", "", "path to gateway JSON configuration")
	version := flag.Bool("version", false, "print gateway wire protocol version and exit")
	flag.Parse()
	if *version {
		fmt.Println("openbear-gateway (protocol v1, Go 1.24)")
		return nil
	}
	if *configPath == "" {
		flag.Usage()
		return errors.New("--config is required")
	}
	c, e := gateway.LoadConfig(*configPath)
	if e != nil {
		return e
	}
	g, e := gateway.New(c)
	if e != nil {
		return e
	}
	defer g.Close()
	public, e := net.Listen("tcp", c.Listen)
	if e != nil {
		return errors.New("cannot bind public listener")
	}
	defer public.Close()
	// The controller installs a loopback controlListen, which takes the control
	// API off the public port; without one both share the public listener.
	var control net.Listener
	if c.ControlListen != "" {
		if control, e = net.Listen("tcp", c.ControlListen); e != nil {
			return errors.New("cannot bind control listener")
		}
		defer control.Close()
	}
	newServer := func(h http.Handler) *http.Server {
		return &http.Server{Handler: h, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 90 * time.Second, MaxHeaderBytes: 64 << 10, ErrorLog: log.New(io.Discard, "", 0)}
	}
	pub, ctl := newServer(g.PublicHandler()), newServer(g.ControlHandler())
	errs := make(chan error, 2)
	go func() {
		if c.TLSCert != "" {
			errs <- pub.ServeTLS(public, c.TLSCert, c.TLSKey)
		} else {
			errs <- pub.Serve(public)
		}
	}()
	if control != nil {
		go func() { errs <- ctl.Serve(control) }()
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	select {
	case <-ctx.Done():
	case err := <-errs:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			_ = pub.Close()
			_ = ctl.Close()
			return errors.New("gateway listener stopped")
		}
	}
	// Close first cancels upgraded connections, which http.Server.Shutdown alone
	// does not know about, as well as SSE and in-flight business requests.
	g.Close()
	deadline, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = pub.Shutdown(deadline)
	_ = ctl.Shutdown(deadline)
	_ = pub.Close()
	_ = ctl.Close()
	return nil
}
