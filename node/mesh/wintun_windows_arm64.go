package mesh

import _ "embed"

//go:embed wintun/arm64/wintun.dll
var wintunDLL []byte

const wintunSHA256 = "f7ba89005544be9d85231a9e0d5f23b2d15b3311667e2dad0debd344918a3f80"
