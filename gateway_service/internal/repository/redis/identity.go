package redis

import (
	"context"
	"strings"

	"github.com/redis/go-redis/v9"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository"
)

// key:<sha256(secret)>, user:<Grove User name>, group:<Grove User Group name>. Three records
// rather than one projection on the credential, so one leaked key dies without touching the rest
// and a budget flip is one write however many keys the holder has.

type keys struct{ rdb *redis.Client }

func (k keys) Get(ctx context.Context, meterID string) (domain.KeyRecord, bool, error) {
	h, err := k.rdb.HGetAll(ctx, "key:"+meterID).Result()
	if err != nil {
		return domain.KeyRecord{}, false, err
	}
	if len(h) == 0 {
		return domain.KeyRecord{}, false, nil
	}
	group, hasGroup := h["group"] // present-but-blank = ungrouped; absent = a pre-group record
	rec := domain.KeyRecord{
		Status:    h["status"],
		User:      h["user"],
		KeyPrefix: h["prefix"],
		Legacy: domain.LegacyKey{
			HasGroup: hasGroup,
			Group:    strings.TrimSpace(group),
			Allow:    domain.ModelSet(h["allow"]),
			Deny:     domain.ModelSet(h["deny"]),
			Models:   domain.ModelSet(h["models"]),
		},
	}
	// Status used to carry the holder's budget flag as a third value. Lift it off here, so Status
	// means only "is this credential live" — which is all a current record puts there.
	if rec.Status == "rate_limited" {
		rec.Status, rec.Legacy.Limited = "active", true
	}
	return rec, true, nil
}

func (k keys) Upsert(ctx context.Context, records []repository.KeyUpsert) error {
	for _, rec := range records {
		if rec.MeterID == "" {
			continue
		}
		redisKey := "key:" + rec.MeterID
		// One transaction, so a reader never sees this record half-updated: the write and the strip
		// below land together or not at all. Two commands left a window showing new fields beside
		// stale legacy ones — inert today, a torn read regardless, and free to fix.
		_, err := k.rdb.TxPipelined(ctx, func(p redis.Pipeliner) error {
			p.HSet(ctx, redisKey, map[string]any{
				"status": rec.Status,
				"user":   rec.User,
				"prefix": rec.Prefix,
			})
			// A pre-group control plane flattened access onto the key; that set is stale the moment a
			// group is pushed. `group`/`allow`/`deny` stay: this cannot tell a current push from an
			// older plane still writing them, and dropping them there would leave no access at all.
			p.HDel(ctx, redisKey, "models", "priority")
			return nil
		})
		if err != nil {
			return err
		}
	}
	return nil
}

func (k keys) Delete(ctx context.Context, ids []string) (int, error) {
	return deletePrefixed(ctx, k.rdb, "key:", ids)
}

type users struct{ rdb *redis.Client }

func (u users) Get(ctx context.Context, name string) (domain.UserRecord, bool, error) {
	h, err := u.rdb.HGetAll(ctx, "user:"+name).Result()
	if err != nil {
		return domain.UserRecord{}, false, err
	}
	if len(h) == 0 {
		return domain.UserRecord{}, false, nil
	}
	return domain.UserRecord{
		Email:   h["email"],
		Group:   strings.TrimSpace(h["group"]),
		Allow:   domain.ModelSet(h["allow"]),
		Deny:    domain.ModelSet(h["deny"]),
		Limited: strings.TrimSpace(h["limited"]) == "1",
	}, true, nil
}

func (u users) Upsert(ctx context.Context, records []repository.UserUpsert) error {
	for _, rec := range records {
		if rec.Name == "" {
			continue
		}
		limited := "0"
		if rec.Limited {
			limited = "1"
		}
		if err := u.rdb.HSet(ctx, "user:"+rec.Name, map[string]any{
			"email":   rec.Email,
			"group":   rec.Group,
			"allow":   rec.Allow,
			"deny":    rec.Deny,
			"limited": limited,
		}).Err(); err != nil {
			return err
		}
	}
	return nil
}

func (u users) Delete(ctx context.Context, ids []string) (int, error) {
	return deletePrefixed(ctx, u.rdb, "user:", ids)
}

type groups struct{ rdb *redis.Client }

// Get answers the zero value for a group that was never pushed or has been deleted, rather than an
// error, so a key pointing at one falls back to its own Allow list instead of 503ing.
func (g groups) Get(ctx context.Context, name string) (domain.GroupRecord, error) {
	if name == "" {
		return domain.GroupRecord{}, nil
	}
	h, err := g.rdb.HGetAll(ctx, "group:"+name).Result()
	if err != nil {
		return domain.GroupRecord{}, err
	}
	return domain.GroupRecord{Models: domain.ModelSet(h["models"])}, nil
}

func (g groups) Upsert(ctx context.Context, records []repository.GroupUpsert) error {
	for _, rec := range records {
		if rec.Name == "" {
			continue
		}
		if err := g.rdb.HSet(ctx, "group:"+rec.Name, map[string]any{
			"models": rec.Models,
		}).Err(); err != nil {
			return err
		}
	}
	return nil
}

// deletePrefixed is the only pruning path the admin API has — every other push upserts — so a
// revoked key keeps working on this box until one of these lands. A blank id would name the prefix
// itself, and DEL on "key:" is a different record.
func deletePrefixed(ctx context.Context, rdb *redis.Client, prefix string, ids []string) (int, error) {
	redisKeys := make([]string, 0, len(ids))
	for _, id := range ids {
		if id != "" {
			redisKeys = append(redisKeys, prefix+id)
		}
	}
	if len(redisKeys) == 0 {
		return 0, nil
	}
	return len(redisKeys), rdb.Del(ctx, redisKeys...).Err()
}
