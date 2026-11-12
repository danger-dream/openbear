package proxy

import (
	"bufio"
	"errors"
	"io"
	"net"
	"net/netip"
	"sync"
	"sync/atomic"
	"time"
)

// udpHub is one UDP socket on a listener's own address and port, shared by
// every association of clients that reached that listener: a firewall in
// front lets UDP in at the port it lets TCP in at. A datagram goes to the
// association of its sender, the first from an address taking an
// association of that address still without one.
type udpHub struct {
	pc     *net.UDPConn
	mu     sync.Mutex
	assocs map[*udpAssoc]bool
}

type udpAssoc struct {
	peer   netip.Addr
	client netip.AddrPort // set by the first datagram, under the hub's mu
	mu     sync.Mutex
	w      *bufio.Writer
	st     *userStats
}

func startHub(at net.Addr) *udpHub {
	a, err := netip.ParseAddrPort(at.String())
	if err != nil {
		return nil
	}
	pc, err := net.ListenUDP("udp", net.UDPAddrFromAddrPort(a))
	if err != nil {
		return nil
	}
	h := &udpHub{pc: pc, assocs: map[*udpAssoc]bool{}}
	go h.run()
	return h
}

func (h *udpHub) close() { h.pc.Close() }

func (h *udpHub) find(from netip.AddrPort) *udpAssoc {
	h.mu.Lock()
	defer h.mu.Unlock()
	var free *udpAssoc
	for a := range h.assocs {
		if a.client == from {
			return a
		}
		if free == nil && !a.client.IsValid() && a.peer == from.Addr().Unmap() {
			free = a
		}
	}
	if free != nil {
		free.client = from
	}
	return free
}

func (h *udpHub) run() {
	buf := make([]byte, 0x10000)
	for {
		n, from, err := h.pc.ReadFromUDPAddrPort(buf)
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			continue
		}
		// RSV (2), FRAG (1): fragments are not taken.
		if n < 4 || buf[2] != 0 {
			continue
		}
		a := h.find(from)
		if a == nil {
			continue
		}
		target, payload, err := parseAddr(buf[3:n])
		if err != nil {
			continue
		}
		a.mu.Lock()
		if writeFrame(a.w, target, payload) == nil && a.w.Flush() == nil {
			a.st.BytesUp.Add(int64(len(payload)))
		}
		a.mu.Unlock()
	}
}

// socksUDP serves a UDP ASSOCIATE: a socket on the address the client
// reached, its datagrams as frames down a tunnel, the exit's frames back as
// datagrams to the client. It lasts as long as the client's TCP connection.
func (s *entryServer) socksUDP(c *net.TCPConn, u *User) {
	st := s.stats[u.Name]
	local, _ := netip.ParseAddrPort(c.LocalAddr().String())
	peer, _ := netip.ParseAddrPort(c.RemoteAddr().String())
	if h := s.hubs[local]; h != nil {
		s.sharedUDP(c, u, h, peer.Addr().Unmap())
		return
	}
	pc, err := net.ListenUDP("udp", net.UDPAddrFromAddrPort(netip.AddrPortFrom(local.Addr(), 0)))
	if err != nil {
		st.Failed.Add(1)
		socksReply(c, statusFailure, netip.AddrPort{})
		return
	}
	defer pc.Close()
	t, err := s.open(u, cmdUDP, Addr{IP: netip.IPv4Unspecified()})
	if err != nil {
		st.Failed.Add(1)
		socksReply(c, statusNetwork, netip.AddrPort{})
		return
	}
	defer t.Close()
	if status := waitStatus(t); status != statusOK {
		st.Failed.Add(1)
		socksReply(c, status, netip.AddrPort{})
		return
	}
	bound, _ := netip.ParseAddrPort(pc.LocalAddr().String())
	if socksReply(c, statusOK, bound) != nil {
		return
	}
	c.SetDeadline(time.Time{})
	st.Active.Add(1)
	defer st.Active.Add(-1)
	// The association ends with the client's TCP connection.
	go func() {
		io.Copy(io.Discard, c)
		pc.Close()
		t.Close()
	}()
	var client atomic.Pointer[netip.AddrPort]
	go func() {
		defer pc.Close()
		buf := make([]byte, 0x10000)
		for {
			a, payload, err := readFrame(t, buf)
			if err != nil {
				return
			}
			to := client.Load()
			if to == nil {
				continue
			}
			out := appendAddr([]byte{0, 0, 0}, a)
			if n, err := pc.WriteToUDPAddrPort(append(out, payload...), *to); err == nil {
				st.BytesDown.Add(int64(n))
			}
		}
	}()
	w := bufio.NewWriterSize(t, 0x10000)
	buf := make([]byte, 0x10000)
	for {
		n, from, err := pc.ReadFromUDPAddrPort(buf)
		if err != nil {
			return
		}
		// Only the client's own address, the port of its first datagram.
		if from.Addr().Unmap() != peer.Addr().Unmap() {
			continue
		}
		if to := client.Load(); to == nil {
			client.Store(&from)
		} else if *to != from {
			continue
		}
		// RSV (2), FRAG (1): fragments are not taken.
		if n < 4 || buf[2] != 0 {
			continue
		}
		a, payload, err := parseAddr(buf[3:n])
		if err != nil {
			continue
		}
		if writeFrame(w, a, payload) != nil || w.Flush() != nil {
			return
		}
		st.BytesUp.Add(int64(len(payload)))
	}
}

// sharedUDP serves a UDP ASSOCIATE on the listener's hub (see udpHub).
func (s *entryServer) sharedUDP(c *net.TCPConn, u *User, h *udpHub, peer netip.Addr) {
	st := s.stats[u.Name]
	t, err := s.open(u, cmdUDP, Addr{IP: netip.IPv4Unspecified()})
	if err != nil {
		st.Failed.Add(1)
		socksReply(c, statusNetwork, netip.AddrPort{})
		return
	}
	defer t.Close()
	if status := waitStatus(t); status != statusOK {
		st.Failed.Add(1)
		socksReply(c, status, netip.AddrPort{})
		return
	}
	a := &udpAssoc{peer: peer, w: bufio.NewWriterSize(t, 0x10000), st: st}
	h.mu.Lock()
	h.assocs[a] = true
	h.mu.Unlock()
	defer func() {
		h.mu.Lock()
		delete(h.assocs, a)
		h.mu.Unlock()
	}()
	bound, _ := netip.ParseAddrPort(h.pc.LocalAddr().String())
	if socksReply(c, statusOK, bound) != nil {
		return
	}
	c.SetDeadline(time.Time{})
	st.Active.Add(1)
	defer st.Active.Add(-1)
	// The association ends with the client's TCP connection.
	go func() {
		io.Copy(io.Discard, c)
		t.Close()
	}()
	buf := make([]byte, 0x10000)
	for {
		target, payload, err := readFrame(t, buf)
		if err != nil {
			return
		}
		h.mu.Lock()
		to := a.client
		h.mu.Unlock()
		if !to.IsValid() {
			continue
		}
		out := appendAddr([]byte{0, 0, 0}, target)
		if n, err := h.pc.WriteToUDPAddrPort(append(out, payload...), to); err == nil {
			st.BytesDown.Add(int64(n))
		}
	}
}
