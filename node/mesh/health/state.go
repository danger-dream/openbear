package health

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

var stateMu sync.Mutex

// UpdateState also serializes the engine's endpoint writes with attachment commits.
func UpdateState(path, field string, value any) error {
	stateMu.Lock()
	defer stateMu.Unlock()
	return updateState(path, field, value)
}

func updateState(path, field string, value any) error {
	all := map[string]json.RawMessage{}
	raw, err := os.ReadFile(path)
	if err == nil {
		if err = json.Unmarshal(raw, &all); err != nil {
			return err
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if all == nil {
		return errors.New("mesh state is null")
	}
	b, err := json.Marshal(value)
	if err != nil {
		return err
	}
	all[field] = b
	all["schema"] = json.RawMessage("1")
	b, err = json.MarshalIndent(all, "", " ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".mesh-state-*")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	if err = f.Chmod(0600); err == nil {
		_, err = f.Write(b)
	}
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return err
	}
	return os.Rename(name, path)
}

// epochField is where state.json keeps the epoch of this node's last
// session (direct.go).
const epochField = "sessionEpoch"

// nextEpoch is the epoch of a new session, greater than every one before
// it, kept before the session signs anything. The clock only raises it, for
// a node that lost its state.
func nextEpoch(path string, now time.Time) (uint64, error) {
	epoch := uint64(max(now.Unix(), 0))
	if path == "" {
		return epoch, nil
	}
	stateMu.Lock()
	defer stateMu.Unlock()
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	var all map[string]json.RawMessage
	if err = json.Unmarshal(raw, &all); err != nil {
		return 0, err
	}
	var last uint64
	if v, ok := all[epochField]; ok {
		if err = json.Unmarshal(v, &last); err != nil {
			return 0, fmt.Errorf("invalid session epoch: %w", err)
		}
	}
	if last == ^uint64(0) {
		return 0, errors.New("session epochs exhausted")
	}
	epoch = max(epoch, last+1)
	return epoch, updateState(path, epochField, epoch)
}

type claim struct {
	Owner      string `json:"owner"`
	Seq        uint64 `json:"seq"`
	Refresh    uint64 `json:"refresh"`
	Generation int64  `json:"generation"`
	Conflict   bool   `json:"conflict,omitempty"`
	// The lease and the member's and owner's signatures of the notice the
	// claim came in (signing.go); a claim stored without them, or not under
	// the keys of the current config, is dropped.
	LeaseMs int64  `json:"leaseMs,omitempty"`
	MSig    string `json:"msig,omitempty"`
	OSig    string `json:"osig,omitempty"`
}

type persistent struct {
	Network    string              `json:"network"`
	Self       string              `json:"self"`
	Revision   int64               `json:"revision"`
	Sequence   uint64              `json:"sequence"`
	Claims     map[string]claim    `json:"claims"`
	Revoked    map[string]int64    `json:"revoked"`
	Authorized map[string][]string `json:"authorized"`
}

type store struct {
	path  string
	state persistent
	save  func(persistent) error
	// dirty: state holds what stage took and no save has written yet;
	// written: the last write, or attempt by flush.
	dirty   bool
	written time.Time
}

func openStore(dir, network, self string) (*store, error) {
	s := &store{state: persistent{Network: network, Self: self, Claims: map[string]claim{}, Revoked: map[string]int64{}, Authorized: map[string][]string{}}}
	if dir == "" {
		s.save = func(persistent) error { return nil }
		return s, nil
	}
	s.path = filepath.Join(dir, "state.json")
	marker := filepath.Join(dir, "health.initialized")
	stateMu.Lock()
	defer stateMu.Unlock()
	raw, err := os.ReadFile(s.path)
	var all map[string]json.RawMessage
	if err == nil {
		if err = json.Unmarshal(raw, &all); err != nil {
			return nil, fmt.Errorf("invalid mesh state: %w", err)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	if h, ok := all["health"]; ok {
		if err = json.Unmarshal(h, &s.state); err != nil {
			return nil, err
		}
		if s.state.Network != network || s.state.Self != self {
			return nil, errors.New("attachment state belongs to another identity")
		}
		if s.state.Claims == nil || s.state.Revoked == nil || s.state.Authorized == nil {
			return nil, errors.New("incomplete attachment state")
		}
	} else {
		if _, err = os.Stat(marker); err == nil {
			return nil, errors.New("attachment state lost; controller registration recovery required")
		} else if !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		if err = updateState(s.path, "health", s.state); err != nil {
			return nil, err
		}
	}
	if err = os.WriteFile(marker, []byte(network+"\n"+self+"\n"), 0600); err != nil {
		return nil, err
	}
	s.save = func(p persistent) error { return UpdateState(s.path, "health", p) }
	return s, nil
}

func (s *store) commit(p persistent) error {
	if err := s.save(p); err != nil {
		return err
	}
	s.state, s.dirty, s.written = p, false, time.Now()
	return nil
}

// stage makes p the state at once and leaves the write to flush, which the
// manager calls once a second: the ownership claims a relay keeps, as
// notices come by the second from every linked relay, so that they share
// one write. A crash may lose the last of them; they come again with the
// next refresh, and restored claims have no lease anyway. The member's own
// sequence is committed: it must be on disk before a commit carries it.
func (s *store) stage(p persistent) { s.state, s.dirty = p, true }

func (s *store) flush() error {
	s.written = time.Now()
	if !s.dirty {
		return nil
	}
	return s.commit(s.state)
}

func (s *store) copy() persistent {
	p := s.state
	p.Claims = make(map[string]claim, len(s.state.Claims))
	for k, v := range s.state.Claims {
		p.Claims[k] = v
	}
	p.Revoked = make(map[string]int64, len(s.state.Revoked))
	for k, v := range s.state.Revoked {
		p.Revoked[k] = v
	}
	p.Authorized = make(map[string][]string, len(s.state.Authorized))
	for k, v := range s.state.Authorized {
		p.Authorized[k] = append([]string(nil), v...)
	}
	return p
}

func (s *store) nextSequence() (uint64, error) {
	p := s.copy()
	if p.Sequence == ^uint64(0) {
		return 0, errors.New("attachment sequence exhausted")
	}
	p.Sequence++
	if err := s.commit(p); err != nil {
		return 0, err
	}
	return p.Sequence, nil
}
