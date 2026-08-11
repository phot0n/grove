package middleware

import (
	"context"
	"net/http"
	"time"

	"grove-gateway/internal/service/admission"
	"grove-gateway/internal/service/routing"
	"grove-gateway/internal/service/transform"
)

// State is what one request accumulates as it moves down the chain. One struct in the context
// rather than a context key per field: the stages are ordered and each reads what the ones above it
// wrote, so a single scoped record is what that actually is.
//
// Scoped to the request and never shared: nothing here outlives the handler, and no two requests
// see the same State.
type State struct {
	Started time.Time

	Identity admission.Identity
	Model    string
	Session  string
	Body     transform.Body
	// RawBody is the body as it arrived. Kept so a request no transform touched is forwarded
	// byte-for-byte rather than re-encoded for nothing.
	RawBody []byte

	Decision routing.Decision

	// Filled on the way back out, by the proxy.
	UpstreamStatus int
	Usage          string
	Deployment     string
	Reason         string
	// Denied is the status a stage refused with, for the access log. 0 means the request reached
	// an upstream.
	Denied       int
	DeniedReason string
}

type stateKey struct{}

// newState attaches a fresh State. Called once, by accesslog, which is the outermost stage that
// needs one.
func newState(r *http.Request) (*http.Request, *State) {
	state := &State{Started: time.Now()}
	return r.WithContext(context.WithValue(r.Context(), stateKey{}, state)), state
}

// FromContext returns the request's State. Never nil for a request that went through the chain;
// a zero value for anything else, so a caller outside the chain does not have to nil-check.
func FromContext(ctx context.Context) *State {
	if state, ok := ctx.Value(stateKey{}).(*State); ok {
		return state
	}
	return &State{}
}

// From is the request-shaped form of FromContext.
func From(r *http.Request) *State { return FromContext(r.Context()) }
