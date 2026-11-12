//go:build !linux

package node

import "context"

// Only Linux nodes look for a transparent proxy.
func (a *Agent) watchProxy(context.Context) {}

func removeBypass() {}
