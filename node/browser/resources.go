package browser

import (
	"embed"
	"sync"
)

// Resources are byte-identical copies of app/browser/pyworker/resources.
//
//go:embed resources/injected.js resources/bridge.js resources/keys.json resources/LICENSE resources/NOTICE resources/ThirdPartyNotices.txt resources/provenance.json
var resources embed.FS

func resource(name string) []byte {
	b, err := resources.ReadFile("resources/" + name)
	if err != nil {
		panic(err)
	}
	return b
}

var (
	injectedJS = string(resource("injected.js"))
	bridgeJS   = string(resource("bridge.js"))
)

// keyRow is one keys.json entry; fields mirror Python dict lookups.
type keyRow struct {
	code string
	row  *Obj
}

var (
	keysOnce sync.Once
	keyRows  []keyRow
	keyIndex map[string]*Obj
)

// keys returns keys.json in file order (Python dict iteration order).
func keys() ([]keyRow, map[string]*Obj) {
	keysOnce.Do(func() {
		parsed, err := parseJSON(resource("keys.json"))
		if err != nil {
			panic(err)
		}
		table := parsed.(*Obj)
		keyIndex = map[string]*Obj{}
		for _, code := range table.Keys() {
			row := table.Obj(code)
			keyRows = append(keyRows, keyRow{code, row})
			keyIndex[code] = row
		}
	})
	return keyRows, keyIndex
}

// ResourceFiles lists the embedded resource names (for verification).
func ResourceFiles() map[string][]byte {
	out := map[string][]byte{}
	for _, name := range []string{"injected.js", "bridge.js", "keys.json", "LICENSE", "NOTICE", "ThirdPartyNotices.txt", "provenance.json"} {
		out[name] = resource(name)
	}
	return out
}
