// Package provisioning is the control plane's push/pull surface, one layer below the HTTP handlers
// that expose it. Grove is the source of truth; these calls project its state into this box's store.
package provisioning

import (
	"context"
	"log/slog"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository"
)

type Service struct {
	store repository.Store
	log   *slog.Logger
}

func New(store repository.Store, log *slog.Logger) *Service {
	return &Service{store: store, log: log}
}

func (s *Service) UpsertKeys(ctx context.Context, records []repository.KeyUpsert) error {
	return s.store.Keys.Upsert(ctx, records)
}

func (s *Service) DeleteKeys(ctx context.Context, ids []string) (int, error) {
	return s.store.Keys.Delete(ctx, ids)
}

func (s *Service) UpsertUsers(ctx context.Context, records []repository.UserUpsert) error {
	return s.store.Users.Upsert(ctx, records)
}

func (s *Service) DeleteUsers(ctx context.Context, ids []string) (int, error) {
	return s.store.Users.Delete(ctx, ids)
}

// UpsertGroups also replaces the pooled public catalogue when given one. Replaced whole, never
// merged — that is how a deleted group stops being advertised. A nil pointer predates the catalogue
// and leaves the current one alone rather than clearing it.
func (s *Service) UpsertGroups(ctx context.Context, records []repository.GroupUpsert, publicCatalog *string) error {
	if err := s.store.Groups.Upsert(ctx, records); err != nil {
		return err
	}
	if publicCatalog == nil {
		return nil
	}
	if *publicCatalog == "" {
		return s.store.Catalog.Clear(ctx)
	}
	return s.store.Catalog.Set(ctx, *publicCatalog)
}

// ReplaceRoutes replaces the table per model named; an empty list retires that model, which is what
// makes it 503 rather than keep a stale placement. `prune` says the payload is the COMPLETE table,
// so anything unnamed is deleted — otherwise retiring one model means naming all of them.
func (s *Service) ReplaceRoutes(ctx context.Context, table map[string][]domain.Route, prune bool) (models, pruned int, err error) {
	var retired []string
	for model, routes := range table {
		if len(routes) == 0 {
			retired = append(retired, model)
			continue
		}
		if err := s.store.Routes.Replace(ctx, model, routes); err != nil {
			return 0, 0, err
		}
	}
	if err := s.store.Routes.DeleteModels(ctx, retired); err != nil {
		return 0, 0, err
	}
	if !prune {
		return len(table), 0, nil
	}
	stale, err := s.staleModels(ctx, table)
	if err != nil {
		// The writes above already landed, so the table is correct and merely wider than it should
		// be. Saying so beats failing a push that did its real work.
		s.log.Warn("routes pruned partially", "err", err)
		return len(table), 0, nil
	}
	if err := s.store.Routes.DeleteModels(ctx, stale); err != nil {
		s.log.Warn("routes pruned partially", "err", err)
		return len(table), 0, nil
	}
	return len(table), len(stale), nil
}

// staleModels is every held model the payload did not name.
func (s *Service) staleModels(ctx context.Context, keep map[string][]domain.Route) ([]string, error) {
	held, err := s.store.Routes.Models(ctx)
	if err != nil {
		return nil, err
	}
	var stale []string
	for _, model := range held {
		if _, named := keep[model]; !named {
			stale = append(stale, model)
		}
	}
	return stale, nil
}

// DrainUsage atomically reads and deletes every live counter, so the snapshot returned is the only
// copy. No second round trip: a failed insert control-plane-side drops that cycle rather than
// double-counting it.
func (s *Service) DrainUsage(ctx context.Context) (map[string]map[string]string, error) {
	return s.store.Usage.Drain(ctx)
}
