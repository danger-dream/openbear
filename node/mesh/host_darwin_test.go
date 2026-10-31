package mesh

import "testing"

func TestResidentBytesDarwin(t *testing.T) {
	if got := residentBytes(); got < 1<<20 {
		t.Fatalf("residentBytes %d", got)
	}
}
