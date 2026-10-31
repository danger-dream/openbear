package mesh

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// withConfigSchemas makes this data plane stand in for a release reading
// low..high until the test ends.
func withConfigSchemas(t *testing.T, low, high int) {
	t.Helper()
	saved := configSchemas
	configSchemas = [2]int{low, high}
	t.Cleanup(func() { configSchemas = saved })
}

// A data plane one release newer, reading schemas 1..2, takes the schema 1
// config a controller writes while a node of the release before is in the
// mesh; one release older refuses schema 2 saying so, not silently.
func TestConfigSchemaRange(t *testing.T) {
	l := newLab(t, spec{relays: []string{"R1"}, members: map[string][]string{"A": {"R1"}}})
	raw := func(schema int) []byte {
		c := l.config("A")
		c.Schema = schema
		b, _ := json.Marshal(c)
		return b
	}
	withConfigSchemas(t, 1, 2)
	for _, schema := range []int{1, 2} {
		if c, err := ParseConfig(raw(schema)); err != nil || c.Schema != schema {
			t.Fatalf("a 1..2 data plane refused schema %d: %v", schema, err)
		}
	}
	if _, err := ParseConfig(raw(3)); err == nil || !strings.Contains(err.Error(), "unsupported config schema 3 (this data plane reads 1..2)") {
		t.Fatalf("schema 3: %v", err)
	}
	if ConfigSchemas() != [2]int{1, 2} {
		t.Fatalf("ConfigSchemas() = %v", ConfigSchemas())
	}

	withConfigSchemas(t, 1, 1)
	if _, err := ParseConfig(raw(2)); err == nil || !strings.Contains(err.Error(), "unsupported config schema 2 (this data plane reads 1..1)") {
		t.Fatalf("a 1..1 data plane given schema 2: %v", err)
	}
	// One that has dropped schema 1.
	withConfigSchemas(t, 2, 3)
	if _, err := ParseConfig(raw(1)); err == nil || !strings.Contains(err.Error(), "reads 2..3") {
		t.Fatalf("a 2..3 data plane given schema 1: %v", err)
	}
}

// status.json carries the range.
func TestStatusCarriesConfigSchemas(t *testing.T) {
	withConfigSchemas(t, 1, 2)
	var s Status
	fillProcess(&s, time.Now(), &eventLog{})
	raw, _ := json.Marshal(s)
	if !strings.Contains(string(raw), `"configSchemas":[1,2]`) {
		t.Fatalf("status.json: %s", raw)
	}
}
