package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"runtime"
	"runtime/debug"
	"syscall"
	"time"

	"openbear/node"
	"openbear/node/browser"
	"openbear/node/mesh"
)

const usage = `openbear-node connects this machine to an OpenBear controller.

Usage:
  openbear-node enroll --url URL --code CODE [--fingerprint SHA256] [--backup URL] [--config PATH] [--no-exec] [--no-terminal] [--browser]
  openbear-node run [--config PATH]
  openbear-node uninstall [--config PATH]   stop the service and remove its files
  openbear-node probe      print what this machine would report, then exit
  openbear-node mesh run --dir DIR   the mesh data plane, started by its own service
  openbear-node version
`

func main() {
	if e := run(os.Args[1:]); e != nil {
		fmt.Fprintln(os.Stderr, "openbear-node:", e)
		os.Exit(1)
	}
}

// lean keeps a monitoring agent small on any machine size: two OS threads
// for Go code and a tighter GC, unless the environment says otherwise.
func lean() {
	if os.Getenv("GOMAXPROCS") == "" {
		runtime.GOMAXPROCS(min(runtime.NumCPU(), 2))
	}
	if os.Getenv("GOGC") == "" {
		debug.SetGCPercent(50)
	}
	if os.Getenv("GOMEMLIMIT") == "" {
		debug.SetMemoryLimit(64 << 20)
	}
}

func run(args []string) error {
	if len(args) == 0 || args[0] == "-h" || args[0] == "--help" || args[0] == "help" {
		fmt.Print(usage)
		return nil
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	switch args[0] {
	case "browser-engine":
		// Same length-prefixed stdin/stdout protocol as app/browser/pyworker.
		return browser.Serve(ctx, os.Stdin, os.Stdout)
	case "job-run":
		// Started by the node for one background job; see node/jobs.go.
		if len(args) != 2 {
			return errors.New("usage: openbear-node job-run DIR")
		}
		lean()
		return node.RunJob(ctx, args[1])
	case "mesh":
		fs := flag.NewFlagSet("mesh run", flag.ContinueOnError)
		dir := fs.String("dir", "", "mesh directory")
		if len(args) < 2 || args[1] != "run" || fs.Parse(args[2:]) != nil || *dir == "" {
			return errors.New("usage: openbear-node mesh run --dir DIR")
		}
		mesh.Version = node.Version
		return meshRun(ctx, *dir)
	case "version", "--version":
		fmt.Printf("openbear-node %s (protocol v%d)\n", node.Version, node.ProtocolVersion)
		return nil
	case "probe":
		c := node.NewCollector()
		c.Sample() // rates need two samples
		time.Sleep(time.Second)
		out, _ := json.MarshalIndent(map[string]any{"info": c.Info(), "sample": c.Sample()}, "", "  ")
		fmt.Println(string(out))
		return nil
	case "enroll":
		fs := flag.NewFlagSet("enroll", flag.ContinueOnError)
		url := fs.String("url", "", "controller origin, e.g. https://example.com")
		backup := fs.String("backup", "", "second address of the same controller, used while --url is unreachable")
		code := fs.String("code", "", "one-time enrollment code from the Servers page")
		// Install scripts pass it in the environment, which a node build from before it ignores.
		fingerprint := fs.String("fingerprint", os.Getenv("OPENBEAR_CONTROLLER_FINGERPRINT"), "SHA-256 of the controller's identity key, from the install command; enrollment stops on any other key")
		path := fs.String("config", node.DefaultConfigPath, "config file to write")
		noExec := fs.Bool("no-exec", false, "refuse commands, file access and remote updates from the controller")
		noTerminal := fs.Bool("no-terminal", false, "refuse web terminal sessions")
		browser := fs.Bool("browser", false, "let the controller drive a browser on this machine, with its network and logins")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		c, err := node.Enroll(ctx, *url, *backup, *code, *fingerprint, node.Capabilities{Exec: !*noExec, Terminal: !*noTerminal, Browser: *browser})
		if err != nil {
			return err
		}
		if err = node.SaveConfig(*path, c); err != nil {
			return err
		}
		fmt.Printf("enrolled as %s; config written to %s\n", c.NodeID, *path)
		return nil
	case "uninstall":
		fs := flag.NewFlagSet("uninstall", flag.ContinueOnError)
		path := fs.String("config", node.DefaultConfigPath, "config file")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		if err := node.Uninstall(*path, true); err != nil {
			return err
		}
		fmt.Println("openbear-node removed")
		return nil
	case "run":
		fs := flag.NewFlagSet("run", flag.ContinueOnError)
		path := fs.String("config", node.DefaultConfigPath, "config file")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		c, err := node.LoadConfig(*path)
		if err != nil {
			return err
		}
		if err = node.SecureConfig(*path, c); err != nil {
			return err
		}
		lean()
		if exe, err := os.Executable(); err == nil {
			node.RemoveReplaced(exe)
		}
		logger := log.New(os.Stderr, "", log.LstdFlags)
		return serve(ctx, stop, *path, logger, func(ctx context.Context) error {
			agent := node.NewAgent(c, logger)
			agent.ConfigPath = *path
			err := agent.Run(ctx)
			if errors.Is(err, node.ErrUninstalled) {
				logger.Print("uninstalling at the controller's request")
				return node.Uninstall(*path, false)
			}
			return err
		})
	}
	fmt.Print(usage)
	return errors.New("unknown command " + args[0])
}
