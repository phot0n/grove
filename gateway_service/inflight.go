package main

import (
	"context"
	"log"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

// inflightWindow bounds how long an admitted request counts against its engine — longer than any
// real generation, short enough that a lost release clears on its own.
const inflightWindow = 15 * time.Minute

func inflightKey(engineURL string) string { return "inflight:" + engineURL }

// claim records that a request is running on an engine. A sorted set of request ids scored by
// admit time, not a counter: /meter is best effort (log.lua logs a failed POST and does not
// retry), and a counter that missed one release would count that request forever and retire the
// engine. Here the entry ages out instead.
func (s *server) claim(ctx context.Context, engineURL, requestID string) {
	key := inflightKey(engineURL)
	// One round trip, not two: this sits on the request's critical path, unlike the release.
	_, _ = s.rdb.Pipelined(ctx, func(p redis.Pipeliner) error {
		p.ZAdd(ctx, key, redis.Z{Score: float64(time.Now().Unix()), Member: requestID})
		p.Expire(ctx, key, 2*inflightWindow) // a retired engine's set disappears on its own
		return nil
	})
}

// release crosses a finished request off its engine. Silent for a request that claimed nothing —
// a denial never reaches /meter, and an older Lua sends neither field.
func (s *server) release(ctx context.Context, engineURL, requestID string) {
	if engineURL == "" || requestID == "" {
		return
	}
	s.rdb.ZRem(ctx, inflightKey(engineURL), requestID)
}

// fillInFlight sets each route's InFlight to what is running on its engine right now, dropping
// entries older than the window on the way past.
//
// A Redis failure leaves every count at zero, which degrades to taking the first healthy route.
// Refusing the request instead would 503 a working engine over an unreadable counter.
func (s *server) fillInFlight(ctx context.Context, routes []Route) {
	cutoff := strconv.FormatInt(time.Now().Add(-inflightWindow).Unix(), 10)
	counts := make([]*redis.IntCmd, len(routes))
	_, err := s.rdb.Pipelined(ctx, func(p redis.Pipeliner) error {
		for i, r := range routes {
			key := inflightKey(r.EngineURL)
			p.ZRemRangeByScore(ctx, key, "-inf", "("+cutoff)
			counts[i] = p.ZCard(ctx, key)
		}
		return nil
	})
	if err != nil {
		log.Printf("in-flight counts unavailable, falling back to the first healthy route: %v", err)
		return
	}
	for i := range routes {
		routes[i].InFlight = int(counts[i].Val())
	}
}
