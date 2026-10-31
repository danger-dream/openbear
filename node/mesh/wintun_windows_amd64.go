package mesh

import _ "embed"

//go:embed wintun/amd64/wintun.dll
var wintunDLL []byte

const wintunSHA256 = "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce"
