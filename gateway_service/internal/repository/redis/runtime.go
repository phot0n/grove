package redis

import (
	"context"
	"errors"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

// Gateway-local runtime state: who is pinned where, what is running right now, and which targets
// have stopped answering. None of it is pushed — the control plane has no view of any of it.

const (
	// inflightWindow bounds how long an admitted request counts against its engine — longer than
	// any real generation, short enough that a lost release clears on its own.
	inflightWindow = 15 * time.Minute
	// healthTTL is how long a target stays retired with nothing further said about it. The counter
	// is what holds it out, so this is really "how long before we let traffic try again" — short,
	// because the only way back in is for a request to succeed, and no request is sent while it is
	// out.
	healthTTL = 60 * time.Second
)

type sessions struct{ rdb *redis.Client }

// Engine answers "" for a session with no pin. redis.Nil is that case, not a failure.
func (s sessions) Engine(ctx context.Context, session string) (string, error) {
	engineURL, err := s.rdb.Get(ctx, "sticky:"+session).Result()
	if errors.Is(err, redis.Nil) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return engineURL, nil
}

func (s sessions) Pin(ctx context.Context, session, engineURL string, ttl time.Duration) error {
	return s.rdb.Set(ctx, "sticky:"+session, engineURL, ttl).Err()
}

type inFlight struct{ rdb *redis.Client }

func inflightKey(engineURL string) string { return "inflight:" + engineURL }

// Counts trims entries older than the window on the way past, then counts what is left.
//
// A sorted set of request ids scored by admit time, not a counter: metering is best effort, and a
// counter that missed one release would count that request forever and retire the engine. Here the
// entry ages out instead.
func (f inFlight) Counts(ctx context.Context, engineURLs []string) ([]int, error) {
	cutoff := strconv.FormatInt(time.Now().Add(-inflightWindow).Unix(), 10)
	cards := make([]*redis.IntCmd, len(engineURLs))
	_, err := f.rdb.Pipelined(ctx, func(p redis.Pipeliner) error {
		for i, engineURL := range engineURLs {
			key := inflightKey(engineURL)
			p.ZRemRangeByScore(ctx, key, "-inf", "("+cutoff)
			cards[i] = p.ZCard(ctx, key)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	counts := make([]int, len(engineURLs))
	for i := range engineURLs {
		counts[i] = int(cards[i].Val())
	}
	return counts, nil
}

func (f inFlight) Claim(ctx context.Context, engineURL, requestID string) error {
	key := inflightKey(engineURL)
	// One round trip, not two: this sits on the request's critical path, unlike the release.
	// Transactional so the claim and its expiry cannot land apart — a set that kept its members
	// but lost its TTL would linger after the engine was retired.
	_, err := f.rdb.TxPipelined(ctx, func(p redis.Pipeliner) error {
		p.ZAdd(ctx, key, redis.Z{Score: float64(time.Now().Unix()), Member: requestID})
		p.Expire(ctx, key, 2*inflightWindow) // a retired engine's set disappears on its own
		return nil
	})
	return err
}

// Release crosses a finished request off its engine. Silent for a request that claimed nothing — a
// denial never reaches the metering path at all.
func (f inFlight) Release(ctx context.Context, engineURL, requestID string) error {
	if engineURL == "" || requestID == "" {
		return nil
	}
	return f.rdb.ZRem(ctx, inflightKey(engineURL), requestID).Err()
}

type health struct{ rdb *redis.Client }

func healthKey(target string) string { return "health:" + target }

// Failures answers one consecutive-failure count per target. A missing or unparseable value is 0,
// which reads as healthy — ejection is an optimisation on top of a table that is already correct.
func (h health) Failures(ctx context.Context, targets []string) ([]int, error) {
	if len(targets) == 0 {
		return nil, nil
	}
	redisKeys := make([]string, len(targets))
	for i, target := range targets {
		redisKeys[i] = healthKey(target)
	}
	values, err := h.rdb.MGet(ctx, redisKeys...).Result()
	if err != nil {
		return nil, err
	}
	counts := make([]int, len(targets))
	for i, value := range values {
		text, ok := value.(string)
		if !ok {
			continue
		}
		if failures, err := strconv.Atoi(text); err == nil {
			counts[i] = failures
		}
	}
	return counts, nil
}

func (h health) RecordFailure(ctx context.Context, target string) error {
	key := healthKey(target)
	// Transactional, and this is the one that matters. The TTL is the ONLY way an ejected target
	// comes back: it is out of rotation, so it receives no traffic, so no success ever arrives to
	// clear the counter and no further failure ever arrives to re-set the expiry. An INCR that
	// landed without its EXPIRE would eject a healthy engine permanently.
	_, err := h.rdb.TxPipelined(ctx, func(p redis.Pipeliner) error {
		p.Incr(ctx, key)
		p.Expire(ctx, key, healthTTL)
		return nil
	})
	return err
}

// RecordSuccess clears the count outright, so a target has to fail EjectAfter times in a row — not
// EjectAfter times ever — before it is dropped.
func (h health) RecordSuccess(ctx context.Context, target string) error {
	return h.rdb.Del(ctx, healthKey(target)).Err()
}
