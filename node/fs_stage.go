//go:build unix || windows

package node

import (
	"errors"
	"io/fs"
	"os"
	"strconv"
)

var errUploadChanged = errors.New("upload_changed")

// POSIX numbers and Go FileMode use different bits for setuid/setgid/sticky.
func posixMode(m fs.FileMode) uint32 {
	n := uint32(m.Perm())
	if m&fs.ModeSetuid != 0 {
		n |= 04000
	}
	if m&fs.ModeSetgid != 0 {
		n |= 02000
	}
	if m&fs.ModeSticky != 0 {
		n |= 01000
	}
	return n
}
func goMode(n uint64) fs.FileMode {
	m := fs.FileMode(n & 0777)
	if n&04000 != 0 {
		m |= fs.ModeSetuid
	}
	if n&02000 != 0 {
		m |= fs.ModeSetgid
	}
	if n&01000 != 0 {
		m |= fs.ModeSticky
	}
	return m
}

func stageMetadata(f *os.File, mode string, prior fs.FileInfo) error {
	perm := fs.FileMode(0644)
	if prior != nil {
		perm = goMode(uint64(posixMode(prior.Mode())))
	}
	if mode != "" {
		n, err := strconv.ParseUint(mode, 8, 32)
		if err != nil || n > 07777 {
			return errors.New("invalid_mode")
		}
		perm = goMode(n)
	}
	// chown may clear setuid/setgid: restore the requested bits only afterwards.
	if prior != nil {
		if err := keepFileOwner(f, prior); err != nil {
			return fileError(err)
		}
	}
	if err := f.Chmod(perm); err != nil {
		return fileError(err)
	}
	return nil
}

func (s *fileStage) commit(mode string, prior fs.FileInfo, noReplace bool) error {
	if err := stageMetadata(s.file, mode, prior); err != nil {
		return err
	}
	if err := s.file.Sync(); err != nil {
		return fileError(err)
	}
	if err := s.publish(noReplace); err != nil {
		if errors.Is(err, errUploadChanged) {
			return err
		}
		if errors.Is(err, fs.ErrExist) {
			return errors.New("file_exists")
		}
		return fileError(err)
	}
	return nil
}

func (s *fileStage) write(data []byte) error {
	if _, err := s.file.Write(data); err != nil {
		return fileError(err)
	}
	return nil
}
