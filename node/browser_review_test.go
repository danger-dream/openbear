package node

import (
	"bufio"
	"context"
	"encoding/json"
	"io"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"golang.org/x/net/websocket"
	"openbear/node/browser"
)

func awaitBrowser(t *testing.T, done <-chan struct{}) {
	t.Helper()
	select {
	case <-done:
	case <-time.After(8 * time.Second):
		t.Fatal("browser operation did not finish")
	}
}

func TestBrowserStopInterruptsItsBlockedPipe(t *testing.T) {
	_, h := browserAgent(t)
	engineIn, browserOut := io.Pipe()
	browserIn, engineOut := io.Pipe()
	var once sync.Once
	closeAll := func() { once.Do(func() { engineIn.Close(); engineOut.Close(); browserIn.Close(); browserOut.Close() }) }
	t.Cleanup(closeAll)
	blocked, release := make(chan struct{}), make(chan struct{})
	defer close(release)
	go func() {
		r := bufio.NewReader(browserIn)
		for {
			data, err := r.ReadBytes(0)
			if err != nil {
				return
			}
			var msg map[string]any
			json.Unmarshal(data[:len(data)-1], &msg)
			if msg["method"] == "Test.stopReading" {
				close(blocked)
				<-release
				return
			}
			body, _ := json.Marshal(map[string]any{"id": msg["id"], "result": map[string]any{}})
			if _, err := browserOut.Write(append(body, 0)); err != nil {
				return
			}
		}
	}()
	transport := browser.NewPipe(engineIn, engineOut, testCloser(func() error { closeAll(); return nil }))
	engine := browser.New()
	engine.UseTransport(transport)
	init, _ := json.Marshal(map[string]any{"connectTimeoutS": 5, "downloadHostPath": t.TempDir()})
	if _, err := engine.Execute(context.Background(), "initialize", init); err != nil {
		t.Fatal(err)
	}
	attachEngine(h, engine)
	stopped := make(chan struct{})
	h.mu.Lock()
	h.launch = &launched{transport: transport, stop: func() { close(stopped) }}
	h.mu.Unlock()
	callDone := make(chan struct{})
	go func() { engine.Call(context.Background(), "Test.stopReading", json.RawMessage(`{}`)); close(callDone) }()
	awaitBrowser(t, blocked)
	secondDone := make(chan struct{})
	go func() {
		engine.Call(context.Background(), "Browser.getVersion", json.RawMessage(`{}`))
		close(secondDone)
	}()
	finished := make(chan struct{})
	go func() { h.stop(); close(finished) }()
	awaitBrowser(t, finished)
	awaitBrowser(t, callDone)
	awaitBrowser(t, secondDone)
	awaitBrowser(t, stopped)
	if current, _ := h.current(); current != nil {
		t.Fatal("stopped engine remained current")
	}
}

func endpointBrowser(t *testing.T, version func()) string {
	t.Helper()
	server := httptest.NewServer(websocket.Server{Handler: func(ws *websocket.Conn) {
		defer ws.Close()
		for {
			var msg map[string]any
			if err := websocket.JSON.Receive(ws, &msg); err != nil {
				return
			}
			if msg["method"] == "Browser.getVersion" && version != nil {
				version()
			}
			if err := websocket.JSON.Send(ws, map[string]any{"id": msg["id"], "result": map[string]any{"product": "Fake/2"}}); err != nil {
				return
			}
		}
	}})
	t.Cleanup(server.Close)
	return "ws" + server.URL[4:]
}

func TestBrowserConfigDiscardsAnInFlightStart(t *testing.T) {
	for _, disable := range []bool{false, true} {
		t.Run(map[bool]string{false: "endpoint", true: "disabled"}[disable], func(t *testing.T) {
			a, h := browserAgent(t)
			entered, release := make(chan struct{}), make(chan struct{})
			var once sync.Once
			old := endpointBrowser(t, func() { once.Do(func() { close(entered) }); <-release })
			next := endpointBrowser(t, nil)
			defer close(release)
			a.mu.Lock()
			a.Config.Browser.Endpoint = old
			a.mu.Unlock()
			started := make(chan struct{})
			go func() { h.ensure(context.Background()); close(started) }()
			awaitBrowser(t, entered)
			config, _ := json.Marshal(map[string]any{"enabled": !disable, "endpoint": next, "download": false})
			if _, err := a.configureBrowser(config); err != nil {
				t.Fatal(err)
			}
			awaitBrowser(t, started)
			if current, _ := h.current(); current != nil {
				t.Fatal("obsolete startup was published")
			}
			if status := h.status(); status["state"] != "stopped" {
				t.Fatalf("obsolete result changed state: %v", status)
			}
			if !disable {
				status, err := h.ensure(context.Background())
				if err != nil || status["state"] != "ready" || status["endpoint"] != next {
					t.Fatalf("new endpoint: %v %v", status, err)
				}
			}
			h.stop()
		})
	}
}
