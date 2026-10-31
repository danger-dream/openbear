//go:build !windows

package mesh

// ExtractWintun is only needed on Windows.
func ExtractWintun(dir string) (string, error) { return "", nil }
