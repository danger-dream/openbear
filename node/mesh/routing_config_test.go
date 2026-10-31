package mesh

import (
	"encoding/json"
	"strings"
	"testing"
)

// TestValidateRouting: routing is for relays, mode dynamic or static,
// bias only for linked relays and within ±10 s.
func TestValidateRouting(t *testing.T) {
	l := newLab(t, spec{relays: []string{"R1", "R2"}, members: map[string][]string{"A": {"R1"}}})
	for _, r := range []*Routing{nil, {}, {Mode: "dynamic"}, {Mode: "static", Bias: map[string]float64{"R2": -20}}, {Bias: map[string]float64{"R2": 10000}}} {
		c := l.config("R1")
		c.Routing = r
		if raw, _ := json.Marshal(c); Validate(raw) != nil {
			t.Errorf("%+v refused: %v", r, Validate(raw))
		}
	}
	for name, tc := range map[string]struct {
		node string
		r    Routing
		want string
	}{
		"on a member":         {"A", Routing{Mode: "dynamic"}, "routing is for relays"},
		"unknown mode":        {"R1", Routing{Mode: "auto"}, "routing.mode"},
		"bias for a member":   {"R1", Routing{Bias: map[string]float64{"A": 5}}, "routing.bias"},
		"bias for an unknown": {"R1", Routing{Bias: map[string]float64{"R9": 5}}, "routing.bias"},
		"bias too large":      {"R1", Routing{Bias: map[string]float64{"R2": 10001}}, "between -10000 and 10000"},
	} {
		c := l.config(tc.node)
		c.Routing = &tc.r
		raw, _ := json.Marshal(c)
		if err := Validate(raw); err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: got %v, want an error about %q", name, err, tc.want)
		}
	}
}
