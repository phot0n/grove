// Package memory is a repository implementation held in maps. Test-only, and the reason the
// services can be tested at all — none of them has ever had a test that reached storage, because
// reaching storage meant reaching Redis.
//
// Deliberately not a mock: it behaves, so a test says what the store held and what the service
// should therefore answer, rather than which calls it expected.
package memory

import (
	"context"
	"errors"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository"
)

// Store is every repository over one set of maps. Fail makes any call return an error, which is
// how the degrade-rather-than-refuse paths get tested.
type Store struct {
	mu sync.Mutex

	Keys     map[string]domain.KeyRecord
	Users    map[string]domain.UserRecord
	Groups   map[string]domain.GroupRecord
	Routes   map[string][]domain.Route
	Sticky   map[string]string
	InFlight map[string]map[string]bool // engine → request ids
	Failures map[string]int
	Usage    map[string]map[string]int64
	Public   *string

	// Fail names the repositories that should error, by interface name ("routes", "inflight",
	// "health", "sessions", "keys", "users", "groups", "usage", "catalog").
	Fail map[string]bool
}

var errStore = errors.New("store unavailable")

func New() *Store {
	return &Store{
		Keys: map[string]domain.KeyRecord{}, Users: map[string]domain.UserRecord{},
		Groups: map[string]domain.GroupRecord{}, Routes: map[string][]domain.Route{},
		Sticky: map[string]string{}, InFlight: map[string]map[string]bool{},
		Failures: map[string]int{}, Usage: map[string]map[string]int64{},
		Fail: map[string]bool{},
	}
}

// Repositories hands this store out behind the interfaces.
func (s *Store) Repositories() repository.Store {
	return repository.Store{
		Keys: keys{s}, Users: users{s}, Groups: groups{s}, Routes: routes{s},
		Sessions: sessions{s}, InFlight: inFlight{s}, Health: health{s},
		Usage: usage{s}, Catalog: catalog{s},
	}
}

func (s *Store) failed(name string) error {
	if s.Fail[name] {
		return errStore
	}
	return nil
}

type keys struct{ s *Store }

func (k keys) Get(_ context.Context, meterID string) (domain.KeyRecord, bool, error) {
	k.s.mu.Lock()
	defer k.s.mu.Unlock()
	if err := k.s.failed("keys"); err != nil {
		return domain.KeyRecord{}, false, err
	}
	rec, ok := k.s.Keys[meterID]
	return rec, ok, nil
}

func (k keys) Upsert(_ context.Context, records []repository.KeyUpsert) error {
	k.s.mu.Lock()
	defer k.s.mu.Unlock()
	if err := k.s.failed("keys"); err != nil {
		return err
	}
	for _, rec := range records {
		if rec.MeterID == "" {
			continue
		}
		k.s.Keys[rec.MeterID] = domain.KeyRecord{
			Status: rec.Status, User: rec.User, KeyPrefix: rec.Prefix,
		}
	}
	return nil
}

func (k keys) Delete(_ context.Context, ids []string) (int, error) {
	k.s.mu.Lock()
	defer k.s.mu.Unlock()
	return deleteFrom(ids, func(id string) { delete(k.s.Keys, id) }), k.s.failed("keys")
}

type users struct{ s *Store }

func (u users) Get(_ context.Context, name string) (domain.UserRecord, bool, error) {
	u.s.mu.Lock()
	defer u.s.mu.Unlock()
	if err := u.s.failed("users"); err != nil {
		return domain.UserRecord{}, false, err
	}
	rec, ok := u.s.Users[name]
	return rec, ok, nil
}

func (u users) Upsert(_ context.Context, records []repository.UserUpsert) error {
	u.s.mu.Lock()
	defer u.s.mu.Unlock()
	if err := u.s.failed("users"); err != nil {
		return err
	}
	for _, rec := range records {
		if rec.Name == "" {
			continue
		}
		u.s.Users[rec.Name] = domain.UserRecord{
			Email: rec.Email, Group: rec.Group,
			Allow: domain.ModelSet(rec.Allow), Deny: domain.ModelSet(rec.Deny),
			Limited: rec.Limited,
		}
	}
	return nil
}

func (u users) Delete(_ context.Context, ids []string) (int, error) {
	u.s.mu.Lock()
	defer u.s.mu.Unlock()
	return deleteFrom(ids, func(id string) { delete(u.s.Users, id) }), u.s.failed("users")
}

type groups struct{ s *Store }

func (g groups) Get(_ context.Context, name string) (domain.GroupRecord, error) {
	g.s.mu.Lock()
	defer g.s.mu.Unlock()
	if err := g.s.failed("groups"); err != nil {
		return domain.GroupRecord{}, err
	}
	return g.s.Groups[name], nil // a group never pushed is the zero value, not an error
}

func (g groups) Upsert(_ context.Context, records []repository.GroupUpsert) error {
	g.s.mu.Lock()
	defer g.s.mu.Unlock()
	if err := g.s.failed("groups"); err != nil {
		return err
	}
	for _, rec := range records {
		if rec.Name == "" {
			continue
		}
		g.s.Groups[rec.Name] = domain.GroupRecord{
			Models: domain.ModelSet(rec.Models),
		}
	}
	return nil
}

type routes struct{ s *Store }

func (r routes) Get(_ context.Context, model string) ([]domain.Route, error) {
	r.s.mu.Lock()
	defer r.s.mu.Unlock()
	if err := r.s.failed("routes"); err != nil {
		return nil, err
	}
	// Copied: PickRoute's caller fills InFlight in place, and a test that ran twice would
	// otherwise see the first run's counts.
	return append([]domain.Route(nil), r.s.Routes[model]...), nil
}

func (r routes) Models(_ context.Context) ([]string, error) {
	r.s.mu.Lock()
	defer r.s.mu.Unlock()
	if err := r.s.failed("routes"); err != nil {
		return nil, err
	}
	out := make([]string, 0, len(r.s.Routes))
	for model := range r.s.Routes {
		out = append(out, model)
	}
	sort.Strings(out)
	return out, nil
}

func (r routes) Replace(_ context.Context, model string, table []domain.Route) error {
	r.s.mu.Lock()
	defer r.s.mu.Unlock()
	if err := r.s.failed("routes"); err != nil {
		return err
	}
	r.s.Routes[model] = table
	return nil
}

func (r routes) DeleteModels(_ context.Context, models []string) error {
	r.s.mu.Lock()
	defer r.s.mu.Unlock()
	if err := r.s.failed("routes"); err != nil {
		return err
	}
	for _, model := range models {
		delete(r.s.Routes, model)
	}
	return nil
}

type sessions struct{ s *Store }

func (s sessions) Engine(_ context.Context, session string) (string, error) {
	s.s.mu.Lock()
	defer s.s.mu.Unlock()
	if err := s.s.failed("sessions"); err != nil {
		return "", err
	}
	return s.s.Sticky[session], nil
}

func (s sessions) Pin(_ context.Context, session, engineURL string, _ time.Duration) error {
	s.s.mu.Lock()
	defer s.s.mu.Unlock()
	if err := s.s.failed("sessions"); err != nil {
		return err
	}
	s.s.Sticky[session] = engineURL
	return nil
}

type inFlight struct{ s *Store }

func (f inFlight) Counts(_ context.Context, engineURLs []string) ([]int, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	if err := f.s.failed("inflight"); err != nil {
		return nil, err
	}
	counts := make([]int, len(engineURLs))
	for i, url := range engineURLs {
		counts[i] = len(f.s.InFlight[url])
	}
	return counts, nil
}

func (f inFlight) Claim(_ context.Context, engineURL, requestID string) error {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	if err := f.s.failed("inflight"); err != nil {
		return err
	}
	if f.s.InFlight[engineURL] == nil {
		f.s.InFlight[engineURL] = map[string]bool{}
	}
	f.s.InFlight[engineURL][requestID] = true
	return nil
}

func (f inFlight) Release(_ context.Context, engineURL, requestID string) error {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	if err := f.s.failed("inflight"); err != nil {
		return err
	}
	delete(f.s.InFlight[engineURL], requestID)
	return nil
}

type health struct{ s *Store }

func (h health) Failures(_ context.Context, targets []string) ([]int, error) {
	h.s.mu.Lock()
	defer h.s.mu.Unlock()
	if err := h.s.failed("health"); err != nil {
		return nil, err
	}
	counts := make([]int, len(targets))
	for i, target := range targets {
		counts[i] = h.s.Failures[target]
	}
	return counts, nil
}

func (h health) RecordFailure(_ context.Context, target string) error {
	h.s.mu.Lock()
	defer h.s.mu.Unlock()
	if err := h.s.failed("health"); err != nil {
		return err
	}
	h.s.Failures[target]++
	return nil
}

func (h health) RecordSuccess(_ context.Context, target string) error {
	h.s.mu.Lock()
	defer h.s.mu.Unlock()
	if err := h.s.failed("health"); err != nil {
		return err
	}
	delete(h.s.Failures, target)
	return nil
}

type usage struct{ s *Store }

func (u usage) Add(_ context.Context, prefix string, fields map[string]int64) error {
	u.s.mu.Lock()
	defer u.s.mu.Unlock()
	if err := u.s.failed("usage"); err != nil {
		return err
	}
	if u.s.Usage[prefix] == nil {
		u.s.Usage[prefix] = map[string]int64{}
	}
	for field, n := range fields {
		u.s.Usage[prefix][field] += n
	}
	return nil
}

// Drain matches the real one: read and delete in one step, so a second call sees nothing.
func (u usage) Drain(_ context.Context) (map[string]map[string]string, error) {
	u.s.mu.Lock()
	defer u.s.mu.Unlock()
	if err := u.s.failed("usage"); err != nil {
		return nil, err
	}
	out := map[string]map[string]string{}
	for prefix, fields := range u.s.Usage {
		snapshot := map[string]string{}
		for field, n := range fields {
			snapshot[field] = strconv.FormatInt(n, 10)
		}
		out[prefix] = snapshot
	}
	u.s.Usage = map[string]map[string]int64{}
	return out, nil
}

type catalog struct{ s *Store }

func (c catalog) Get(_ context.Context) (string, bool, error) {
	c.s.mu.Lock()
	defer c.s.mu.Unlock()
	if err := c.s.failed("catalog"); err != nil {
		return "", false, err
	}
	if c.s.Public == nil {
		return "", false, nil
	}
	return *c.s.Public, true, nil
}

func (c catalog) Set(_ context.Context, csv string) error {
	c.s.mu.Lock()
	defer c.s.mu.Unlock()
	if err := c.s.failed("catalog"); err != nil {
		return err
	}
	c.s.Public = &csv
	return nil
}

func (c catalog) Clear(_ context.Context) error {
	c.s.mu.Lock()
	defer c.s.mu.Unlock()
	if err := c.s.failed("catalog"); err != nil {
		return err
	}
	c.s.Public = nil
	return nil
}

// deleteFrom skips blank ids, matching the real store: a blank one would name the key prefix
// itself, which is a different record.
func deleteFrom(ids []string, remove func(string)) int {
	count := 0
	for _, id := range ids {
		if strings.TrimSpace(id) == "" {
			continue
		}
		remove(id)
		count++
	}
	return count
}
