//go:build windows

package node

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"golang.org/x/sys/windows"
)

// SecureConfig makes the directories above the node's state its own before
// anything runs from them: ProgramData lets every user create folders, so
// OpenBear or OpenBear\node may be there before the node. In the installed
// layout a directory holding this node's config is the node's own and only
// taken over; one without it, or a link, is deleted and created again. A
// custom config path is the user's own directory and only has its access
// narrowed, as SaveConfig always did. c is written back whenever the config
// is gone afterwards, also when a deletion stopped halfway.
func SecureConfig(path string, c Config) error {
	err := secureDataDirs(filepath.Dir(path))
	if _, statErr := os.Stat(path); errors.Is(statErr, os.ErrNotExist) {
		if written := writeConfig(path, c); err == nil {
			err = written
		}
	}
	return err
}

func secureDataDirs(dir string) error {
	if !strings.EqualFold(filepath.Clean(dir), filepath.Dir(DefaultConfigPath)) {
		return nil
	}
	return secureLayout(dir)
}

// secureLayout: the OpenBear level, then the node level dir below it.
func secureLayout(dir string) error {
	ours := nodeConfig(filepath.Join(dir, "config.json"))
	for _, d := range []string{filepath.Dir(dir), dir} {
		if err := ownedDir(d, ours); err != nil {
			return err
		}
	}
	return nil
}

// nodeConfig: path holds a node's config, which only the node writes there.
func nodeConfig(path string) bool {
	data, err := os.ReadFile(path)
	var c Config
	if err != nil || json.Unmarshal(data, &c) != nil || c.NodeID == "" {
		return false
	}
	_, err = c.Key()
	return err == nil
}

// ownedDir leaves dir an ordinary directory owned by SYSTEM or
// Administrators, readable only by them. A directory of the node's own (ours)
// keeps what it holds; any other is deleted first.
func ownedDir(dir string, ours bool) error {
	if info, err := os.Lstat(dir); err == nil {
		switch {
		case !info.IsDir() || reparsePoint(info):
			if err := removeForeign(dir); err != nil {
				return err
			}
		case trusted(dir):
		case ours:
			if err := takeOver(dir); err != nil {
				return err
			}
		default:
			if err := removeForeign(dir); err != nil {
				return err
			}
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	return protectDir(dir)
}

func reparsePoint(info os.FileInfo) bool {
	data, ok := info.Sys().(*syscall.Win32FileAttributeData)
	return ok && data.FileAttributes&windows.FILE_ATTRIBUTE_REPARSE_POINT != 0
}

func trusted(path string) bool {
	sd, err := windows.GetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.OWNER_SECURITY_INFORMATION)
	if err != nil {
		return false
	}
	owner, _, err := sd.Owner()
	return err == nil && owner != nil && (owner.IsWellKnown(windows.WinLocalSystemSid) || owner.IsWellKnown(windows.WinBuiltinAdministratorsSid))
}

// removeForeign deletes path. A link goes without its target; anything else
// is first taken over (owner Administrators, access only for SYSTEM and
// Administrators), so its former owner can no longer change it meanwhile.
func removeForeign(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if reparsePoint(info) {
		return os.Remove(path)
	}
	if err := takeOver(path); err != nil {
		return err
	}
	if info.IsDir() {
		entries, err := os.ReadDir(path)
		if err != nil {
			return err
		}
		for _, entry := range entries {
			if err := removeForeign(filepath.Join(path, entry.Name())); err != nil {
				return err
			}
		}
	}
	return os.Remove(path)
}

func takeOver(path string) error {
	enablePrivileges("SeTakeOwnershipPrivilege", "SeRestorePrivilege")
	admins, err := windows.CreateWellKnownSid(windows.WinBuiltinAdministratorsSid)
	if err != nil {
		return err
	}
	sd, err := windows.SecurityDescriptorFromString("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")
	if err != nil {
		return err
	}
	dacl, _, err := sd.DACL()
	if err != nil {
		return err
	}
	return windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION,
		admins, nil, dacl, nil)
}

func enablePrivileges(names ...string) {
	var token windows.Token
	if windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_ADJUST_PRIVILEGES|windows.TOKEN_QUERY, &token) != nil {
		return
	}
	defer token.Close()
	for _, name := range names {
		var luid windows.LUID
		if windows.LookupPrivilegeValue(nil, windows.StringToUTF16Ptr(name), &luid) != nil {
			continue
		}
		privileges := windows.Tokenprivileges{PrivilegeCount: 1}
		privileges.Privileges[0] = windows.LUIDAndAttributes{Luid: luid, Attributes: windows.SE_PRIVILEGE_ENABLED}
		windows.AdjustTokenPrivileges(token, false, &privileges, 0, nil, nil)
	}
}
