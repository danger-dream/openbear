package mesh

import "testing"

// The alarm follows the machine's memory between the fixed lower end, which
// small machines and those of unknown memory keep, and an upper end; the GC
// limit is four fifths of it, the old 128 and 208 MiB at the lower ends.
func TestMemoryAlarmFollowsTheMachine(t *testing.T) {
	const mib = 1 << 20
	for _, c := range []struct {
		profile string
		machine uint64
		alarm   uint64
	}{
		{roleMember, 0, 160 * mib}, {roleMember, 929 * mib, 160 * mib}, {roleMember, 2048 * mib, 2048 * mib / 6},
		{roleMember, 32 << 30, 1 << 30}, {roleRelay, 948 * mib, 260 * mib}, {roleRelay, 2048 * mib, 512 * mib}, {roleRelay, 64 << 30, 2 << 30},
	} {
		if got := memoryAlarm(c.profile, c.machine); got != c.alarm {
			t.Errorf("%s on %d MiB: alarm %d MiB, want %d MiB", c.profile, c.machine/mib, got/mib, c.alarm/mib)
		}
	}
	if memoryAlarm(roleMember, 0)/5*4 != 128*mib || memoryAlarm(roleRelay, 0)/5*4 != 208*mib {
		t.Fatal("the lower ends no longer give the old GC limits")
	}
	if machineMemory() == 0 {
		t.Log("this platform reports no machine memory")
	}
}
