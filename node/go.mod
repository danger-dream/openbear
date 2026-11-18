module openbear/node

go 1.24.0

require (
	golang.org/x/crypto v0.37.0
	golang.org/x/net v0.39.0
	golang.org/x/sys v0.38.0
	golang.zx2c4.com/wireguard v0.0.0-20261024220952-b3bc704ef0ee
	golang.zx2c4.com/wireguard/windows v0.5.3
	gvisor.dev/gvisor v0.0.0-20250503011706-39ed1f5ac29c
)

require (
	github.com/google/btree v1.1.2 // indirect
	github.com/lxn/walk v0.0.0-20210112085537-c389da54e794 // indirect
	github.com/lxn/win v0.0.0-20210218163916-a377121e959e // indirect
	golang.org/x/mod v0.21.0 // indirect
	golang.org/x/sync v0.13.0 // indirect
	golang.org/x/text v0.24.0 // indirect
	golang.org/x/time v0.7.0 // indirect
	golang.org/x/tools v0.26.0 // indirect
	golang.org/x/xerrors v0.0.0-20220907171357-04be3eba64a2 // indirect
	golang.zx2c4.com/wintun v0.0.0-20230126152724-0fa3db229ce2 // indirect
	gopkg.in/Knetic/govaluate.v3 v3.0.0 // indirect
)

replace golang.zx2c4.com/wireguard => ./third_party/wireguard-go
