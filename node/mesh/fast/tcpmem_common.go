package fast

// CalcTCPMem computes the target [low, pressure, high] page watermarks for
// net.ipv4.tcp_mem:
//   - low      = total / 6
//   - pressure = total / 5
//   - high     = min(total / 4, available / 2)
//
// Each watermark is only raised (never lowered below orig), and monotonic
// order low <= pressure <= high is preserved even when available memory caps
// high below total/5 or total/6.
func CalcTCPMem(orig [3]uint64, totalBytes, availBytes, pageSize uint64) (target [3]uint64, wantLow uint64) {
	if pageSize == 0 || totalBytes == 0 {
		return orig, 0
	}
	cLow := (totalBytes / 6) / pageSize
	cPress := (totalBytes / 5) / pageSize
	capBytes := totalBytes / 4
	if availBytes > 0 && availBytes/2 < capBytes {
		capBytes = availBytes / 2
	}
	cHigh := capBytes / pageSize

	high := max(orig[2], cHigh)
	press := max(orig[1], min(cPress, high))
	low := max(orig[0], min(cLow, press))
	return [3]uint64{low, press, high}, cLow
}
