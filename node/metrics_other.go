//go:build !linux && !darwin && !windows

package node

import (
	"os"
	"runtime"
)

// Other platforms report identity only until their collectors exist.
type Collector struct{}

func NewCollector() *Collector { return &Collector{} }

func (c *Collector) Info() Info {
	name, _ := os.Hostname()
	return Info{Hostname: name, Platform: runtime.GOOS, OS: runtime.GOOS, Arch: runtime.GOARCH, Cores: runtime.NumCPU()}
}

func (c *Collector) Sample() Sample { return Sample{} }
