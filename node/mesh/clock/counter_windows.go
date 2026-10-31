package clock

import (
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// counter is the performance counter (QueryPerformanceCounter), nil when
// the system has none.
func counter() func() time.Duration {
	k := windows.NewLazySystemDLL("kernel32.dll")
	qpc, qpf := k.NewProc("QueryPerformanceCounter"), k.NewProc("QueryPerformanceFrequency")
	if qpc.Find() != nil || qpf.Find() != nil {
		return nil
	}
	var freq int64
	if r, _, _ := qpf.Call(uintptr(unsafe.Pointer(&freq))); r == 0 || freq <= 0 {
		return nil
	}
	read := func() (int64, bool) {
		var v int64
		r, _, _ := qpc.Call(uintptr(unsafe.Pointer(&v)))
		return v, r != 0
	}
	if _, ok := read(); !ok {
		return nil
	}
	return func() time.Duration {
		v, _ := read()
		return time.Duration(v/freq)*time.Second + time.Duration(v%freq)*time.Second/time.Duration(freq)
	}
}
