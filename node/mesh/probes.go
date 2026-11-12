package mesh

import "openbear/node/mesh/probe"

// ProbeConfig is the probe section of config.json (package probe).
type ProbeConfig = probe.Config

// ProbeTarget is one candidate peer to measure.
type ProbeTarget = probe.Target

// ProbeResult is one entry of status.json's probes list.
type ProbeResult = probe.Result

// ProbeService runs the lightweight candidate probes for a runner.
type ProbeService = probe.Service
