//go:build linux

package node

import (
	"encoding/json"
	"os"
	"testing"
)

// On Linux the data plane runs the node's own program, as do background jobs: of the processes
// running it, the data plane is the one status.json names.
func TestNetPortsKnowTheDataPlaneRunningTheNodesProgram(t *testing.T) {
	meshCopying(t, false)
	a := portsAgent(t)
	self, _ := os.Executable()
	a.meshHost.exe = func() (string, error) { return self, nil }
	mesh, meshPorts := holder(t, self, "tcp")
	job, jobPorts := holder(t, self, "tcp")
	writeStatus(t, a.meshHost, map[string]any{"pid": mesh.Process.Pid, "ready": true})
	params, _ := json.Marshal(map[string][]int{"tcp": {atoi(meshPorts[0]), atoi(jobPorts[0])}})
	got, err := callPorts(t, a, string(params))
	if err != nil {
		t.Fatal(err)
	}
	if !got["tcp"][meshPorts[0]].Ours || got["tcp"][jobPorts[0]].Ours {
		t.Fatalf("data plane %d, job %d: %+v", mesh.Process.Pid, job.Process.Pid, got["tcp"])
	}
}
