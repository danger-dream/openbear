package fast

// Status is what status.json reports under "fast" while fast relay is enabled
// on this node.
type Status struct {
	// Port is this node's relay listen port, 0 when it does not listen.
	Port         int              `json:"port"`
	ListenError  string           `json:"listenError,omitempty"`
	Capture      *CaptureStatus   `json:"capture,omitempty"`
	Channels     []ChannelStatus  `json:"channels,omitempty"`
	Entry        RoleStatus       `json:"entry"`
	Transit      RoleStatus       `json:"transit"`
	Exit         RoleStatus       `json:"exit"`
	Peers        []PeerPoolStatus `json:"peers,omitempty"`
	HeaderErrors int64            `json:"headerErrors"`
	RefusedEntry int64            `json:"refusedEntry"`
	RefusedExit  int64            `json:"refusedExit"`
	TCPMem       *TCPMemStatus    `json:"tcpMem,omitempty"`
}

// RoleStatus counts connections and bytes for one fast-relay role on this node.
type RoleStatus struct {
	Active   int64 `json:"active"`
	Opened   int64 `json:"opened"`
	Failed   int64 `json:"failed"`
	BytesIn  int64 `json:"bytesIn"`
	BytesOut int64 `json:"bytesOut"`
}

// CaptureStatus reports how this node takes connections to the fast ports:
// Mode "nft" (Linux nftables) or "tun" (the data plane's own stack).
type CaptureStatus struct {
	Mode   string `json:"mode"`
	Active bool   `json:"active"`
	UDP    bool   `json:"udp"`
	Error  string `json:"error,omitempty"`
	// Missing names the system command nftables capture needs and the
	// machine lacks; it starts by itself once that is installed.
	Missing string `json:"missing,omitempty"`
}

// TCPMemStatus reports Linux net.ipv4.tcp_mem auto-tuning and pressure state.
type TCPMemStatus struct {
	PageBytes     int       `json:"pageBytes"`
	Original      [3]uint64 `json:"original"`
	Current       [3]uint64 `json:"current"`
	OriginalBytes [3]uint64 `json:"originalBytes"`
	CurrentBytes  [3]uint64 `json:"currentBytes"`
	Changed       bool      `json:"changed"`
	Limited       bool      `json:"limited,omitempty"`
	ReadOnly      bool      `json:"readOnly,omitempty"`
	Container     bool      `json:"container,omitempty"`
	Pressures     uint64    `json:"pressures"`
	Error         string    `json:"error,omitempty"`
}
