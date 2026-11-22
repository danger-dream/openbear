package node

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"testing"
)

// TestMain lets the test binary act as the job runner, as the node binary
// does when started with "job-run DIR".
func TestMain(m *testing.M) {
	if len(os.Args) == 3 && os.Args[1] == "job-run" {
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		err := RunJob(ctx, os.Args[2])
		stop()
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	if len(os.Args) > 1 && os.Args[1] == "bypass-run" {
		fmt.Fprintln(os.Stderr, RunBypassed(os.Args[2:]))
		os.Exit(1)
	}
	if len(os.Args) > 2 && os.Args[1] == "hold-ports" {
		holdPorts(os.Args[2:])
		os.Exit(0)
	}
	// A node looks its public address up as a session starts and says hello
	// again with it: here from a local echo, at once, not from the services on
	// the internet, which answered within tens of milliseconds on some
	// machines and never on others.
	echo := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { io.WriteString(w, testPublicIPv4) }))
	publicV4Abroad, publicV4China, publicV6 = []string{echo.URL}, []string{echo.URL}, nil
	code := m.Run()
	echo.Close()
	os.Exit(code)
}

// testPublicIPv4 is the public address the tests' echo gives.
const testPublicIPv4 = "198.51.100.10"

// holdPorts listens on a free port for each network ("tcp", "udp4", ...),
// prints the ports and keeps them until stdin closes.
func holdPorts(networks []string) {
	var ports []string
	for _, n := range networks {
		addr := ":0"
		if strings.HasSuffix(n, "4") {
			addr = "0.0.0.0:0"
		}
		var a net.Addr
		if strings.HasPrefix(n, "tcp") {
			l, err := net.Listen(n, addr)
			if err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(1)
			}
			a = l.Addr()
		} else {
			c, err := net.ListenPacket(n, addr)
			if err != nil {
				fmt.Fprintln(os.Stderr, err)
				os.Exit(1)
			}
			a = c.LocalAddr()
		}
		_, port, _ := net.SplitHostPort(a.String())
		ports = append(ports, port)
	}
	fmt.Println(strings.Join(ports, " "))
	io.Copy(io.Discard, os.Stdin)
}
