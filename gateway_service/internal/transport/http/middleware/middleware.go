// Package middleware is the extension point. Every stage of the request path is one of these,
// registered by name and ordered by configuration, so adding behaviour is a new file plus a name in
// a list — never an edit to a handler.
package middleware

import (
	"fmt"
	"log/slog"
	"net/http"
	"sort"
	"sync"

	"grove-gateway/internal/service/admission"
	"grove-gateway/internal/service/metering"
	"grove-gateway/internal/service/routing"
	"grove-gateway/internal/service/transform"
)

// Middleware is the only shape a component must satisfy — plain net/http, so anything written
// against the standard library composes without an adapter.
type Middleware func(http.Handler) http.Handler

// Deps is everything a middleware may reach. A middleware that needs something not here is asking
// for a dependency the request path does not have, which is the check this struct exists to be.
type Deps struct {
	Admission *admission.Service
	Routing   *routing.Service
	Metering  *metering.Service
	Transform *transform.Chain
	Drain     DrainState
	Log       *slog.Logger
	// Access is the per-request record. Separate from Log because it is a record, not a
	// diagnostic, and must not move when the log level does.
	Access *slog.Logger

	// MaxBodyBytes bounds a request body. Beyond it the caller gets a 413 rather than the process
	// growing to hold whatever was sent. Read per request so a reload moves it.
	MaxBodyBytes func() int64
	// IngressToken is the bearer a gateway must present on an ingress. Blank on a gateway.
	IngressToken string
}

// DrainState reports whether the process is shutting down. An interface so the lifecycle owns the
// flag and the middleware only reads it.
type DrainState interface {
	Draining() bool
}

// Factory builds a middleware from the shared deps. Registered at init; resolved at startup.
type Factory func(Deps) (Middleware, error)

var (
	mu       sync.RWMutex
	registry = map[string]Factory{}
)

// Register adds a middleware under its name.
func Register(name string, factory Factory) {
	mu.Lock()
	defer mu.Unlock()
	registry[name] = factory
}

// Registered is every middleware the binary knows about, sorted — for the startup log, so a
// rejected name in the configured list can be compared against what was available.
func Registered() []string {
	mu.RLock()
	defer mu.RUnlock()
	return sortedLocked()
}

// GatewayChain is the tenant plane's order. It is not arbitrary:
//
//   - recover is outermost so a panic anywhere below is still answered.
//   - accesslog wraps everything it needs to time, including the refusals above auth.
//   - drain sits above auth: a restarting gateway answers the same way whether or not the caller's
//     key is any good.
//   - meter sits directly below route, because route claims an in-flight slot and everything below
//     it must give that slot back, whatever happens.
var GatewayChain = []string{
	"recover", "accesslog", "drain",
	"auth", "quota", "body", "modelaccess",
	"route", "meter", "transform", "upstreamauth",
}

// IngressChain is the same machinery with the tenant stages absent — not disabled, absent. An
// ingress holds no keys, so no stage on that box could read a key store even if one were pushed.
var IngressChain = []string{
	"recover", "accesslog", "drain",
	"ingressauth", "pick", "upstreamauth",
}

// Chain resolves names to middleware and folds them into one, applied in the order given.
//
// An unknown name is a startup error rather than a warning: a misspelt `quota` would silently stop
// enforcing the monthly budget, and nothing downstream would look wrong.
func Chain(deps Deps, names []string) (Middleware, error) {
	mu.RLock()
	defer mu.RUnlock()

	built := make([]Middleware, 0, len(names))
	for _, name := range names {
		factory, ok := registry[name]
		if !ok {
			return nil, fmt.Errorf("unknown middleware %q; registered: %v", name, sortedLocked())
		}
		m, err := factory(deps)
		if err != nil {
			return nil, fmt.Errorf("middleware %s: %w", name, err)
		}
		built = append(built, m)
	}
	return func(next http.Handler) http.Handler {
		// Applied in reverse so the first name is the outermost wrapper, which is the order the
		// list reads in.
		for i := len(built) - 1; i >= 0; i-- {
			next = built[i](next)
		}
		return next
	}, nil
}

func sortedLocked() []string {
	names := make([]string, 0, len(registry))
	for name := range registry {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}
