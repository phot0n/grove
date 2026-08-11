// Package repository declares what the services need out of storage, and nothing about how it is
// stored. Every method takes and returns domain types or plain Go values — no Redis vocabulary
// crosses this line, which is what lets the whole store be replaced by a new folder beside redis/.
package repository

import (
	"context"
	"time"

	"grove-gateway/internal/domain"
)

// Keys holds credentials. A key's only fact of its own is whether it has been revoked.
type Keys interface {
	Get(ctx context.Context, meterID string) (domain.KeyRecord, bool, error)
	Upsert(ctx context.Context, records []KeyUpsert) error
	Delete(ctx context.Context, ids []string) (int, error)
}

// Users holds the access and budget state behind a person — one record however many keys.
type Users interface {
	Get(ctx context.Context, name string) (domain.UserRecord, bool, error)
	Upsert(ctx context.Context, records []UserUpsert) error
	Delete(ctx context.Context, ids []string) (int, error)
}

// Groups holds what a Grove User Group grants everyone in it.
type Groups interface {
	// Get answers the zero value for a group that was never pushed, so a key pointing at one falls
	// back to its own Allow list instead of erroring.
	Get(ctx context.Context, name string) (domain.GroupRecord, error)
	Upsert(ctx context.Context, records []GroupUpsert) error
}

// Routes is the model → placements table the control plane pushes.
type Routes interface {
	Get(ctx context.Context, model string) ([]domain.Route, error)
	// Models lists every model with at least one placement, deduped and sorted.
	Models(ctx context.Context) ([]string, error)
	Replace(ctx context.Context, model string, routes []domain.Route) error
	DeleteModels(ctx context.Context, models []string) error
}

// Sessions is the affinity pin: a caller keeps its engine so its prefix cache stays warm.
type Sessions interface {
	// Engine answers "" when the session has no pin, which is not an error.
	Engine(ctx context.Context, session string) (string, error)
	Pin(ctx context.Context, session, engineURL string, ttl time.Duration) error
}

// InFlight counts what is running on each engine right now. Gateway-local: the control plane has
// no view of it.
type InFlight interface {
	// Counts answers one number per engine, in the order given. A store failure returns an error
	// and the caller degrades to taking the first healthy route rather than refusing the request.
	Counts(ctx context.Context, engineURLs []string) ([]int, error)
	Claim(ctx context.Context, engineURL, requestID string) error
	Release(ctx context.Context, engineURL, requestID string) error
}

// Health is the consecutive-failure count behind passive ejection.
type Health interface {
	// Failures answers one count per target, in the order given.
	Failures(ctx context.Context, targets []string) ([]int, error)
	RecordFailure(ctx context.Context, target string) error
	RecordSuccess(ctx context.Context, target string) error
}

// Usage accrues token counters per API key prefix. The field names are the service's business —
// this only adds numbers to them.
type Usage interface {
	// Add applies every field in one atomic step, so a drain never sees half a request.
	Add(ctx context.Context, prefix string, fields map[string]int64) error
	// Drain atomically reads and deletes every live counter, keyed by bare prefix. Read-and-delete
	// in one step: the snapshot is the only copy once it returns, which never double-counts.
	Drain(ctx context.Context) (map[string]map[string]string, error)
}

// Catalog holds the pooled public model list, replaced whole on each groups push.
type Catalog interface {
	// Get answers ok=false when no group is public, which is the default.
	Get(ctx context.Context) (string, bool, error)
	Set(ctx context.Context, csv string) error
	Clear(ctx context.Context) error
}

// Store is every repository at once, for wiring. Services take only the interfaces they use.
type Store struct {
	Keys     Keys
	Users    Users
	Groups   Groups
	Routes   Routes
	Sessions Sessions
	InFlight InFlight
	Health   Health
	Usage    Usage
	Catalog  Catalog
}

// The upsert shapes the control plane pushes. Deliberately flat strings, matching the wire: the
// comma lists are parsed on read, where a stale record from an older control plane is still
// readable.

type KeyUpsert struct {
	MeterID string // sha256(secret) hex — the record id
	Prefix  string
	User    string
	Status  string
}

type UserUpsert struct {
	Name    string
	Email   string
	Group   string
	Allow   string // comma list
	Deny    string // comma list
	Limited bool
}

type GroupUpsert struct {
	Name   string
	Models string // comma list
}
