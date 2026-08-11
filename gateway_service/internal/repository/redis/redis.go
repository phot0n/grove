// Package redis is the only place in the binary that knows Redis exists. Every key name, every
// hash field and every TTL lives here; the services above it see domain types and plain values.
package redis

import (
	"context"

	"github.com/redis/go-redis/v9"

	"grove-gateway/internal/repository"
)

// Client owns the connection. The repositories are thin values over it — separate types because
// three of them answer to Get and one struct cannot.
type Client struct {
	rdb *redis.Client
}

func New(addr string) *Client {
	return &Client{rdb: redis.NewClient(&redis.Options{Addr: addr})}
}

// Ping fails fast at startup: a gateway that cannot reach its store serves nothing, and finding
// out on the first customer request would report it as a routing fault.
func (c *Client) Ping(ctx context.Context) error { return c.rdb.Ping(ctx).Err() }

func (c *Client) Close() error { return c.rdb.Close() }

// Store hands out every repository over this one connection.
func (c *Client) Store() repository.Store {
	return repository.Store{
		Keys:     keys{c.rdb},
		Users:    users{c.rdb},
		Groups:   groups{c.rdb},
		Routes:   routes{c.rdb},
		Sessions: sessions{c.rdb},
		InFlight: inFlight{c.rdb},
		Health:   health{c.rdb},
		Usage:    usage{c.rdb},
		Catalog:  catalog{c.rdb},
	}
}
