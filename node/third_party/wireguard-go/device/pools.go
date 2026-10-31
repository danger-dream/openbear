/* SPDX-License-Identifier: MIT
 *
 * Copyright (C) 2017-2025 WireGuard LLC. All Rights Reserved.
 */

package device

import (
	"sync"
)

type WaitPool struct {
	fixed chan any
	pool  sync.Pool
	cond  sync.Cond
	lock  sync.Mutex
	count uint32 // Get calls not yet Put back
	max   uint32
}

func NewWaitPool(max uint32, new func() any) *WaitPool {
	p := &WaitPool{pool: sync.Pool{New: new}, max: max}
	p.cond = sync.Cond{L: &p.lock}
	return p
}

func (p *WaitPool) Get() any {
	if p.fixed != nil {
		return <-p.fixed
	}
	if p.max != 0 {
		p.lock.Lock()
		for p.count >= p.max {
			p.cond.Wait()
		}
		p.count++
		p.lock.Unlock()
	}
	return p.pool.Get()
}

func (p *WaitPool) Put(x any) {
	if p.fixed != nil {
		p.fixed <- x
		return
	}
	p.pool.Put(x)
	if p.max == 0 {
		return
	}
	p.lock.Lock()
	defer p.lock.Unlock()
	p.count--
	p.cond.Signal()
}

func fixedPool(count int, new func() any) *WaitPool {
	p := &WaitPool{fixed: make(chan any, count)}
	for i := 0; i < count; i++ {
		p.fixed <- new()
	}
	return p
}

func (device *Device) PopulatePools() {
	if device.resources != nil {
		l := device.resources.limits
		out := 2*(l.TxBuffers+l.KeepaliveBuffers+l.MaxPeers) + l.BatchSize
		in := 2*(l.RxBuffers+l.MaxPeers) + l.BatchSize
		device.pool.inboundElementsContainer = fixedPool(in, func() any { return &QueueInboundElementsContainer{elems: make([]*QueueInboundElement, 0, l.BatchSize)} })
		device.pool.outboundElementsContainer = fixedPool(out, func() any {
			return &QueueOutboundElementsContainer{elems: make([]*QueueOutboundElement, 0, l.BatchSize)}
		})
		device.pool.inboundElements = fixedPool(in, func() any { return new(QueueInboundElement) })
		device.pool.outboundElements = fixedPool(out, func() any { return new(QueueOutboundElement) })
		return
	}
	device.pool.inboundElementsContainer = NewWaitPool(PreallocatedBuffersPerPool, func() any {
		s := make([]*QueueInboundElement, 0, device.BatchSize())
		return &QueueInboundElementsContainer{elems: s}
	})
	device.pool.outboundElementsContainer = NewWaitPool(PreallocatedBuffersPerPool, func() any {
		s := make([]*QueueOutboundElement, 0, device.BatchSize())
		return &QueueOutboundElementsContainer{elems: s}
	})
	device.pool.messageBuffers = NewWaitPool(PreallocatedBuffersPerPool, func() any {
		return new([MaxMessageSize]byte)
	})
	device.pool.inboundElements = NewWaitPool(PreallocatedBuffersPerPool, func() any {
		return new(QueueInboundElement)
	})
	device.pool.outboundElements = NewWaitPool(PreallocatedBuffersPerPool, func() any {
		return new(QueueOutboundElement)
	})
}

func (device *Device) GetInboundElementsContainer() *QueueInboundElementsContainer {
	c := device.pool.inboundElementsContainer.Get().(*QueueInboundElementsContainer)
	c.Mutex = sync.Mutex{}
	return c
}

func (device *Device) PutInboundElementsContainer(c *QueueInboundElementsContainer) {
	for i := range c.elems {
		c.elems[i] = nil
	}
	c.elems = c.elems[:0]
	device.pool.inboundElementsContainer.Put(c)
}

func (device *Device) GetOutboundElementsContainer() *QueueOutboundElementsContainer {
	c := device.pool.outboundElementsContainer.Get().(*QueueOutboundElementsContainer)
	c.Mutex = sync.Mutex{}
	return c
}

func (device *Device) PutOutboundElementsContainer(c *QueueOutboundElementsContainer) {
	for i := range c.elems {
		c.elems[i] = nil
	}
	c.elems = c.elems[:0]
	device.pool.outboundElementsContainer.Put(c)
}

func (device *Device) GetMessageBuffer() *[MaxMessageSize]byte {
	if device.resources != nil {
		return device.resources.rx.get(device.resources.stop)
	}
	return device.pool.messageBuffers.Get().(*[MaxMessageSize]byte)
}

func (device *Device) PutMessageBuffer(msg *[MaxMessageSize]byte) {
	if device.resources != nil {
		device.resources.owners[msg].free <- msg
		return
	}
	device.pool.messageBuffers.Put(msg)
}

func (device *Device) GetInboundElement() *QueueInboundElement {
	return device.pool.inboundElements.Get().(*QueueInboundElement)
}

func (device *Device) PutInboundElement(elem *QueueInboundElement) {
	elem.clearPointers()
	device.pool.inboundElements.Put(elem)
}

func (device *Device) GetOutboundElement() *QueueOutboundElement {
	return device.pool.outboundElements.Get().(*QueueOutboundElement)
}

func (device *Device) PutOutboundElement(elem *QueueOutboundElement) {
	device.releasePending(elem)
	elem.clearPointers()
	device.pool.outboundElements.Put(elem)
}
