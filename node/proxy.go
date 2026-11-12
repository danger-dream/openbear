package node

import "sync/atomic"

// ProxyStatus is what host.details reports of a transparent proxy taking over
// this machine's own traffic (see watchProxy): what gave it away (tun, tproxy,
// redirect), whether the bypass is in place, and why not when it should be.
type ProxyStatus struct {
	Kinds  []string `json:"kinds,omitempty"`
	Active bool     `json:"active"`
	Error  string   `json:"error,omitempty"`
}

var proxyNow atomic.Pointer[ProxyStatus]

func proxyStatus() *ProxyStatus { return proxyNow.Load() }
