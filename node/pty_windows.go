//go:build windows

package node

import (
	"errors"
	"io"
	"os"
	"sync"
	"unsafe"

	"golang.org/x/sys/windows"
)

// conpty is a shell on a Windows pseudo console (ConPTY, Windows 10 1809+).
type conpty struct {
	console windows.Handle
	process windows.Handle
	in, out *os.File

	once   sync.Once
	mu     sync.Mutex
	exited bool
	hungUp chan struct{}
	closed chan struct{}
}

func (c *conpty) Read(p []byte) (int, error)  { return c.out.Read(p) }
func (c *conpty) Write(p []byte) (int, error) { return c.in.Write(p) }
func (c *conpty) Wait() {
	<-c.closed
	c.out.Close()
}

func (c *conpty) Resize(cols, rows int) error {
	if cols <= 0 || rows <= 0 || cols > 1000 || rows > 1000 {
		return errors.New("invalid size")
	}
	return windows.ResizePseudoConsole(c.console, windows.Coord{X: int16(cols), Y: int16(rows)})
}

// Hangup ends the shell; closing the console then sends every program still
// attached to it the close event, the Windows counterpart of SIGHUP.
// Programs started on their own console (Start-Process) keep running.
func (c *conpty) Hangup() {
	c.once.Do(func() {
		close(c.hungUp)
		c.mu.Lock()
		if !c.exited {
			windows.TerminateProcess(c.process, 1)
		}
		c.mu.Unlock()
	})
}

// release runs once the shell has exited. ClosePseudoConsole waits until its
// pending output has been read; after a hangup nobody else reads it, so the
// rest is discarded here.
func (c *conpty) release() {
	windows.WaitForSingleObject(c.process, windows.INFINITE)
	c.mu.Lock()
	c.exited = true
	windows.CloseHandle(c.process)
	c.mu.Unlock()
	go func() {
		select {
		case <-c.hungUp:
			io.Copy(io.Discard, c.out)
		case <-c.closed:
		}
	}()
	windows.ClosePseudoConsole(c.console)
	c.in.Close()
	close(c.closed)
}

func startShell(cols, rows int) (console, error) {
	if cols <= 0 || rows <= 0 || cols > 1000 || rows > 1000 {
		cols, rows = 80, 24
	}
	var inRead, inWrite, outRead, outWrite windows.Handle
	if err := windows.CreatePipe(&inRead, &inWrite, nil, 0); err != nil {
		return nil, err
	}
	if err := windows.CreatePipe(&outRead, &outWrite, nil, 0); err != nil {
		windows.CloseHandle(inRead)
		windows.CloseHandle(inWrite)
		return nil, err
	}
	c := &conpty{in: os.NewFile(uintptr(inWrite), "conpty-in"), out: os.NewFile(uintptr(outRead), "conpty-out"),
		hungUp: make(chan struct{}), closed: make(chan struct{})}
	err := windows.CreatePseudoConsole(windows.Coord{X: int16(cols), Y: int16(rows)}, inRead, outWrite, 0, &c.console)
	// The console holds its own copies of its ends of the pipes.
	windows.CloseHandle(inRead)
	windows.CloseHandle(outWrite)
	if err != nil {
		c.in.Close()
		c.out.Close()
		return nil, err
	}
	if err = c.spawn(); err != nil {
		windows.ClosePseudoConsole(c.console)
		c.in.Close()
		c.out.Close()
		return nil, err
	}
	go c.release()
	return c, nil
}

func (c *conpty) spawn() error {
	attrs, err := windows.NewProcThreadAttributeList(1)
	if err != nil {
		return err
	}
	defer attrs.Delete()
	// The attribute's value is the console handle itself, not its address.
	console := c.console
	if err = attrs.Update(windows.PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, *(*unsafe.Pointer)(unsafe.Pointer(&console)), unsafe.Sizeof(console)); err != nil {
		return err
	}
	si := &windows.StartupInfoEx{ProcThreadAttributeList: attrs.List()}
	si.Cb = uint32(unsafe.Sizeof(*si))
	// Empty standard handles keep a redirected parent's handles from
	// replacing the console's.
	si.Flags = windows.STARTF_USESTDHANDLES
	line, err := windows.UTF16PtrFromString(`"` + powershell() + `" -NoLogo`)
	if err != nil {
		return err
	}
	dir, _ := windows.UTF16PtrFromString(os.Getenv("SystemDrive") + `\`)
	var pi windows.ProcessInformation
	if err = windows.CreateProcess(nil, line, nil, nil, false, windows.EXTENDED_STARTUPINFO_PRESENT, nil, dir, &si.StartupInfo, &pi); err != nil {
		return err
	}
	windows.CloseHandle(pi.Thread)
	c.process = pi.Process
	return nil
}
