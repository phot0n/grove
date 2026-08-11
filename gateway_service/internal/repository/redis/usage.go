package redis

import (
	"context"
	"errors"
	"strings"

	"github.com/redis/go-redis/v9"
)

// usage:<key prefix> — one hash per API key, holding both the flat metrics and their per-model and
// per-deployment twins, so a single drain carries the aggregate and the breakdown together. The
// month is NOT tracked here: the control plane stamps it from its own clock when it pulls.
type usage struct{ rdb *redis.Client }

// Add applies every field in one transaction, so a control-plane drain never sees a half-written
// request.
func (u usage) Add(ctx context.Context, prefix string, fields map[string]int64) error {
	if prefix == "" || len(fields) == 0 {
		return nil
	}
	key := "usage:" + prefix
	_, err := u.rdb.TxPipelined(ctx, func(p redis.Pipeliner) error {
		for field, n := range fields {
			p.HIncrBy(ctx, key, field, n)
		}
		return nil
	})
	return err
}

// drainScript atomically READS and DELETES one live counter — the snapshot is returned and the
// counter removed in one Redis call. Redis is single-threaded, so a request metered mid-pull lands
// either fully in the returned snapshot (before the DEL) or on a fresh key (after), never split.
// HGETALL on a missing key is empty and DEL is a no-op, so no EXISTS guard is needed.
var drainScript = redis.NewScript(`
local h = redis.call('HGETALL', KEYS[1])
redis.call('DEL', KEYS[1])
return h
`)

// Drain is 1-shot / no retry: once drained the delta lives only in the returned map, so a
// control-plane crash before it commits loses that cycle's delta — rare, bounded, and never a
// double count.
func (u usage) Drain(ctx context.Context) (map[string]map[string]string, error) {
	out := map[string]map[string]string{}
	var cursor uint64
	for {
		found, next, err := u.rdb.Scan(ctx, cursor, "usage:*", 200).Result()
		if err != nil {
			return nil, err
		}
		for _, live := range found {
			res, err := drainScript.Run(ctx, u.rdb, []string{live}).Result()
			if err != nil {
				continue
			}
			// SCAN can return a key more than once; the second drain finds it already deleted
			// (empty). Do not clobber the snapshot already captured for this prefix.
			if m := pairsToMap(res); len(m) > 0 {
				out[strings.TrimPrefix(live, "usage:")] = m
			}
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}
	return out, nil
}

// pairsToMap turns a HGETALL reply ([field, val, field, val, ...]) into a map.
func pairsToMap(res any) map[string]string {
	m := map[string]string{}
	arr, ok := res.([]any)
	if !ok {
		return m
	}
	for i := 0; i+1 < len(arr); i += 2 {
		field, _ := arr[i].(string)
		value, _ := arr[i+1].(string)
		m[field] = value
	}
	return m
}

// catalog:public — the comma list of models every group flagged Show in Public Catalogue names,
// assembled by the control plane and replaced whole on each groups push. Absent = no group is
// public, which is the default.
type catalog struct{ rdb *redis.Client }

func (c catalog) Get(ctx context.Context) (string, bool, error) {
	csv, err := c.rdb.Get(ctx, "catalog:public").Result()
	if errors.Is(err, redis.Nil) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	return csv, true, nil
}

func (c catalog) Set(ctx context.Context, csv string) error {
	return c.rdb.Set(ctx, "catalog:public", csv, 0).Err()
}

func (c catalog) Clear(ctx context.Context) error {
	return c.rdb.Del(ctx, "catalog:public").Err()
}
