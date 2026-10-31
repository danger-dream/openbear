//go:build linux || darwin || windows

package node

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"time"
)

type meshChange struct {
	ID         string          `json:"id"`
	State      string          `json:"state"`
	Revision   int64           `json:"revision"`
	Deadline   int64           `json:"deadline"`
	ActivateAt int64           `json:"activateAt,omitempty"`
	Config     json.RawMessage `json:"config"`
	Previous   json.RawMessage `json:"previous,omitempty"`
	WasRunning bool            `json:"wasRunning"`
	URL        string          `json:"url"`
	Backup     string          `json:"backup"`
	OldURL     string          `json:"oldUrl"`
	OldBackup  string          `json:"oldBackup"`
	Error      string          `json:"error,omitempty"`
}

func (c *meshChange) pending() bool {
	return c != nil && c.State != "confirmed" && c.State != "aborted"
}

func (h *meshHost) readChange() (*meshChange, error) {
	data, err := os.ReadFile(h.path("change.json"))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var c meshChange
	if err = json.Unmarshal(data, &c); err != nil || c.ID == "" || c.Deadline <= 0 {
		return nil, errors.New("mesh_change_unreadable")
	}
	return &c, nil
}

func (h *meshHost) saveChange(c *meshChange) error {
	data, err := json.Marshal(c)
	if err != nil {
		return err
	}
	return replaceFile(h.path("change.json"), bytes.NewReader(data), 0o600)
}

func (h *meshHost) changeView() map[string]any {
	h.mu.Lock()
	defer h.mu.Unlock()
	c, err := h.readChange()
	if err != nil {
		return map[string]any{"error": "mesh_change_unreadable"}
	}
	if c == nil {
		return nil
	}
	return map[string]any{"id": c.ID, "state": c.State, "revision": c.Revision, "deadline": c.Deadline, "error": c.Error}
}

// The journal and timer belong to the node, not to a controller connection.
// A restart resumes it before dialing; losing the control session cannot cancel rollback.
func (a *Agent) resumeMeshChange() {
	h := a.meshNode()
	h.mu.Lock()
	defer h.mu.Unlock()
	c, err := h.readChange()
	if err != nil {
		a.Logger.Printf("mesh change recovery: %v", err)
		return
	}
	if c.pending() {
		a.tickMeshChangeLocked(h, c)
	}
}

func (a *Agent) scheduleMeshChangeLocked(h *meshHost, c *meshChange) {
	if h.changeTimer != nil {
		h.changeTimer.Stop()
		h.changeTimer = nil
	}
	if !c.pending() {
		return
	}
	at := c.Deadline
	if c.State == "scheduled" && c.ActivateAt < at {
		at = c.ActivateAt
	}
	if c.State == "activating" || c.State == "rolling_back" || c.State == "restoring" {
		at = time.Now().Add(time.Second).UnixMilli()
	}
	delay := max(time.Millisecond, time.Until(time.UnixMilli(at)))
	h.changeTimer = time.AfterFunc(delay, a.resumeMeshChange)
}

func (a *Agent) changeAddresses(url, backup string) error {
	cfg := a.config()
	if cfg.URL == url && cfg.Backup == backup {
		return nil
	}
	cfg.URL, cfg.Backup = url, backup
	if a.ConfigPath != "" {
		if err := SaveConfig(a.ConfigPath, cfg); err != nil {
			return err
		}
	}
	a.mu.Lock()
	a.Config.URL, a.Config.Backup, a.backup = url, backup, false
	a.mu.Unlock()
	a.finish(errReconnect)
	return nil
}

func (a *Agent) restoreMeshChangeLocked(h *meshHost, c *meshChange) error {
	if c.State == "restoring" {
		return h.finishMeshRestore(c)
	}
	c.State = "rolling_back"
	if err := h.saveChange(c); err != nil {
		return err
	}
	if len(c.Previous) > 0 {
		if err := replaceFile(h.path("config.json"), bytes.NewReader(c.Previous), 0o600); err != nil {
			return err
		}
	} else {
		if err := h.service.stop(); err != nil {
			return err
		}
		for _, name := range []string{"config.json", "applied.json"} {
			if err := os.Remove(h.path(name)); err != nil && !errors.Is(err, os.ErrNotExist) {
				return err
			}
		}
	}
	if err := a.changeAddresses(c.OldURL, c.OldBackup); err != nil {
		return err
	}
	if c.WasRunning && h.service.state() != "running" {
		if err := h.service.start(); err != nil {
			return err
		}
	}
	c.State, c.Error = "restoring", ""
	if err := h.saveChange(c); err != nil {
		return err
	}
	return h.finishMeshRestore(c)
}

func (h *meshHost) finishMeshRestore(c *meshChange) error {
	if len(c.Previous) > 0 && c.WasRunning {
		var head struct {
			Revision int64 `json:"revision"`
		}
		var status meshStatus
		applied, _ := os.ReadFile(h.path("applied.json"))
		data, err := os.ReadFile(h.path("status.json"))
		if !bytes.Equal(applied, c.Previous) || h.service.state() != "running" || json.Unmarshal(c.Previous, &head) != nil || err != nil || json.Unmarshal(data, &status) != nil || status.AppliedRevision != head.Revision || status.Error != "" || !status.Ready {
			return nil
		}
	}
	c.State = "aborted"
	return h.saveChange(c)
}

func (a *Agent) tickMeshChangeLocked(h *meshHost, c *meshChange) {
	now := time.Now().UnixMilli()
	var err error
	if c.State == "rolling_back" || c.State == "restoring" || now >= c.Deadline {
		err = a.restoreMeshChangeLocked(h, c)
	} else if c.State == "activating" || c.State == "scheduled" && now >= c.ActivateAt {
		c.State = "activating"
		err = h.saveChange(c)
		if err == nil {
			err = replaceFile(h.path("config.json"), bytes.NewReader(c.Config), 0o600)
		}
		if err == nil {
			err = a.changeAddresses(c.URL, c.Backup)
		}
		if err == nil && h.service.state() != "running" {
			err = h.service.start()
		}
		if err == nil {
			c.State = "active"
			err = h.saveChange(c)
		}
	}
	if err != nil {
		c.Error = err.Error()
		// Keep retrying a partial restoration, including a failed journal write.
		c.State = "rolling_back"
		_ = h.saveChange(c)
		a.Logger.Printf("mesh change recovery: %v", err)
	}
	a.scheduleMeshChangeLocked(h, c)
}

func (a *Agent) meshChangeCall(method string, raw json.RawMessage) (any, error) {
	h := a.meshNode()
	var p struct {
		ID             string          `json:"id"`
		Config         json.RawMessage `json:"config"`
		URL            string          `json:"url"`
		Backup         string          `json:"backup"`
		TimeoutSeconds int             `json:"timeoutSeconds"`
		DelayMs        int64           `json:"delayMs"`
	}
	if json.Unmarshal(raw, &p) != nil || p.ID == "" || len(p.ID) > 128 {
		return nil, errors.New("invalid_params")
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	c, err := h.readChange()
	if err != nil {
		return nil, err
	}
	if c.pending() && (time.Now().UnixMilli() >= c.Deadline || c.State == "rolling_back" || c.State == "restoring") {
		a.tickMeshChangeLocked(h, c)
	}
	if method == "mesh.prepare" {
		var head struct {
			Revision int64 `json:"revision"`
		}
		if json.Unmarshal(p.Config, &head) != nil || head.Revision <= 0 || p.TimeoutSeconds < 30 || p.TimeoutSeconds > 600 {
			return nil, errors.New("invalid_params")
		}
		if err = h.validate(p.Config); err != nil {
			return nil, errors.New("invalid_config: " + err.Error())
		}
		if _, err = ControllerURL(p.URL); err != nil {
			return nil, errors.New("invalid_url")
		}
		if p.Backup != "" {
			if _, err = ControllerURL(p.Backup); err != nil {
				return nil, errors.New("invalid_url")
			}
		}
		if h.record() == nil || h.service.state() == "absent" {
			return nil, errors.New("mesh_not_installed")
		}
		if c != nil && c.ID == p.ID {
			if !bytes.Equal(c.Config, p.Config) || c.URL != p.URL || c.Backup != p.Backup {
				return nil, errors.New("mesh_change_conflict")
			}
			return map[string]any{"id": c.ID, "state": c.State}, nil
		}
		if c.pending() {
			return nil, errors.New("mesh_change_pending")
		}
		previous, err := os.ReadFile(h.path("applied.json"))
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, errors.New("mesh_previous_config_missing")
		}
		if len(previous) > 0 {
			if err = h.validate(previous); err != nil {
				return nil, errors.New("mesh_previous_config_invalid")
			}
		} else if h.service.state() == "running" {
			return nil, errors.New("mesh_previous_config_missing")
		}
		cfg := a.config()
		c = &meshChange{ID: p.ID, State: "prepared", Revision: head.Revision, Deadline: time.Now().Add(time.Duration(p.TimeoutSeconds) * time.Second).UnixMilli(),
			Config: p.Config, Previous: previous, WasRunning: h.service.state() == "running", URL: p.URL, Backup: p.Backup, OldURL: cfg.URL, OldBackup: cfg.Backup}
		if err = h.saveChange(c); err != nil {
			return nil, err
		}
	} else {
		if c == nil {
			if method == "mesh.abort" {
				c = &meshChange{ID: p.ID, State: "aborted", Deadline: time.Now().UnixMilli()}
				if err = h.saveChange(c); err != nil {
					return nil, err
				}
				return map[string]any{"id": p.ID, "state": "aborted"}, nil
			}
			return nil, errors.New("mesh_change_missing")
		}
		if c.ID != p.ID {
			return nil, errors.New("mesh_change_conflict")
		}
		switch method {
		case "mesh.activate":
			if p.DelayMs < 0 || p.DelayMs > 10000 {
				return nil, errors.New("invalid_params")
			}
			if c.State == "prepared" {
				c.State, c.ActivateAt = "scheduled", time.Now().Add(time.Duration(p.DelayMs)*time.Millisecond).UnixMilli()
				if err = h.saveChange(c); err != nil {
					return nil, err
				}
			} else if c.State != "scheduled" && c.State != "active" {
				return nil, errors.New("mesh_change_not_prepared")
			}
		case "mesh.confirm":
			if c.State != "confirmed" {
				var status meshStatus
				data, err := os.ReadFile(h.path("status.json"))
				info, statErr := os.Stat(h.path("status.json"))
				if c.State != "active" || h.service.state() != "running" || statErr != nil || time.Since(info.ModTime()) > meshStale || err != nil || json.Unmarshal(data, &status) != nil || status.AppliedRevision != c.Revision || status.Error != "" || !status.Ready {
					return nil, errors.New("mesh_change_not_applied")
				}
				c.State = "confirmed"
				if err = h.saveChange(c); err != nil {
					return nil, err
				}
			}
		case "mesh.abort":
			if c.State != "aborted" {
				if err = a.restoreMeshChangeLocked(h, c); err != nil {
					a.scheduleMeshChangeLocked(h, c)
					return nil, err
				}
			}
		default:
			return nil, errors.New("unsupported_method")
		}
	}
	a.scheduleMeshChangeLocked(h, c)
	return map[string]any{"id": c.ID, "state": c.State}, nil
}

func (a *Agent) lockMeshAddresses() (func(), error) {
	h := a.meshNode()
	h.mu.Lock()
	if err := h.allowMeshMutation(); err != nil {
		h.mu.Unlock()
		return nil, err
	}
	return h.mu.Unlock, nil
}

func (h *meshHost) allowMeshMutation() error {
	c, err := h.readChange()
	if err != nil {
		return err
	}
	if c.pending() {
		return errors.New("mesh_change_pending")
	}
	if c != nil {
		return os.Remove(h.path("change.json"))
	}
	return nil
}
