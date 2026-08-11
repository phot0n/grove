package redis

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strings"

	"github.com/redis/go-redis/v9"

	"grove-gateway/internal/domain"
)

// deploy:<model> — a JSON array of placements, replaced whole on each push and never expiring.
// The absence of the key is what a fully drained model looks like, and what makes it 503.
type routes struct{ rdb *redis.Client }

func (r routes) Get(ctx context.Context, model string) ([]domain.Route, error) {
	raw, err := r.rdb.Get(ctx, "deploy:"+model).Result()
	if errors.Is(err, redis.Nil) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []domain.Route
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return nil, err
	}
	return out, nil
}

// Models returns every currently-deployed model (a deploy:<model> key exists = ≥1 placement; empty
// sets are deleted on push). Deduped and sorted for stable output. /v1/models is low-frequency, so
// a SCAN per call is fine.
func (r routes) Models(ctx context.Context) ([]string, error) {
	seen := map[string]bool{}
	var cursor uint64
	for {
		found, next, err := r.rdb.Scan(ctx, cursor, "deploy:*", 200).Result()
		if err != nil {
			return nil, err
		}
		for _, k := range found {
			seen[strings.TrimPrefix(k, "deploy:")] = true
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}
	out := make([]string, 0, len(seen))
	for m := range seen {
		out = append(out, m)
	}
	sort.Strings(out)
	return out, nil
}

func (r routes) Replace(ctx context.Context, model string, table []domain.Route) error {
	encoded, err := json.Marshal(table)
	if err != nil {
		return err
	}
	return r.rdb.Set(ctx, "deploy:"+model, encoded, 0).Err()
}

func (r routes) DeleteModels(ctx context.Context, models []string) error {
	if len(models) == 0 {
		return nil
	}
	redisKeys := make([]string, 0, len(models))
	for _, m := range models {
		redisKeys = append(redisKeys, "deploy:"+m)
	}
	return r.rdb.Del(ctx, redisKeys...).Err()
}
