package admission

import (
	"context"
	"errors"
	"testing"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository/memory"
)

const secret = "gr_sk_test"

func meterID() string { return domain.SHA256Hex(secret) }

func serviceOver(store *memory.Store) *Service {
	repos := store.Repositories()
	return New(repos.Keys, repos.Users, repos.Groups)
}

// The happy path is three hops — key → user → group — and the whole reason the records are split.
func TestIdentifyFollowsKeyToUserToGroup(t *testing.T) {
	store := memory.New()
	store.Keys[meterID()] = domain.KeyRecord{Status: "active", User: "ritwik", KeyPrefix: "abc123"}
	store.Users["ritwik"] = domain.UserRecord{Group: "acme", Email: "r@example.com"}
	store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("qwen3-4b")}

	id, err := serviceOver(store).Identify(context.Background(), "Bearer "+secret)
	if err != nil {
		t.Fatalf("Identify: %v", err)
	}
	if id.Prefix() != "abc123" {
		t.Errorf("prefix = %q, want abc123 — usage would accrue to the wrong bucket", id.Prefix())
	}
}

func TestIdentifyRefusals(t *testing.T) {
	store := memory.New()
	store.Keys[meterID()] = domain.KeyRecord{Status: "active", User: "ritwik"}
	svc := serviceOver(store)

	for _, c := range []struct {
		name, header string
		want         int
	}{
		{"no header at all", "", 401},
		{"a bearer with nothing after it", "Bearer ", 401},
		{"a key that was never pushed", "Bearer nope", 401},
	} {
		t.Run(c.name, func(t *testing.T) {
			_, err := svc.Identify(context.Background(), c.header)
			assertStatus(t, err, c.want)
		})
	}
}

// A store that cannot be read is a 503, never a 401: answering "unknown api key" would send the
// caller to rotate a credential that was fine.
func TestAnUnreadableStoreIsNotAnAuthFailure(t *testing.T) {
	store := memory.New()
	store.Keys[meterID()] = domain.KeyRecord{Status: "active", User: "ritwik"}
	store.Fail["keys"] = true

	_, err := serviceOver(store).Identify(context.Background(), "Bearer "+secret)
	assertStatus(t, err, 503)
}

// A key whose user record has not landed yet falls back to what the key itself carries. This is
// what makes the control plane and the agent deployable in either order.
func TestAPreSplitKeyFallsBackToItsOwnProjection(t *testing.T) {
	store := memory.New()
	store.Keys[meterID()] = domain.KeyRecord{
		Status: "active", User: "ritwik",
		Legacy: domain.LegacyKey{Models: domain.ModelSet("qwen3-4b")},
	}
	// No user:ritwik record.

	id, err := serviceOver(store).Identify(context.Background(), "Bearer "+secret)
	if err != nil {
		t.Fatalf("Identify: %v", err)
	}
	if err := serviceOver(store).Authorize(id, "qwen3-4b"); err != nil {
		t.Errorf("a pre-split key lost the access its own record granted: %v", err)
	}
}

// A CURRENT key with no user record must reach nothing. It carries no projection to fall back to,
// and inventing one would be the difference between failing closed and failing open.
func TestACurrentKeyWithNoUserRecordReachesNothing(t *testing.T) {
	store := memory.New()
	store.Keys[meterID()] = domain.KeyRecord{Status: "active", User: "ritwik"}
	store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("qwen3-4b")}

	id, err := serviceOver(store).Identify(context.Background(), "Bearer "+secret)
	if err != nil {
		t.Fatalf("Identify: %v", err)
	}
	assertStatus(t, serviceOver(store).Authorize(id, "qwen3-4b"), 403)
}

// Order matters: a revoked key is 401 even for a holder who is also over budget, because the key
// is the thing that is wrong.
func TestAuthorizeChecksTheCredentialBeforeTheBudget(t *testing.T) {
	store := memory.New()
	store.Keys[meterID()] = domain.KeyRecord{Status: "revoked", User: "ritwik"}
	store.Users["ritwik"] = domain.UserRecord{Group: "acme", Limited: true}
	store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("qwen3-4b")}

	svc := serviceOver(store)
	id, err := svc.Identify(context.Background(), "Bearer "+secret)
	if err != nil {
		t.Fatalf("Identify: %v", err)
	}
	assertStatus(t, svc.Authorize(id, "qwen3-4b"), 401)
}

func TestAuthorizeGates(t *testing.T) {
	for _, c := range []struct {
		name  string
		user  domain.UserRecord
		model string
		want  int
	}{
		{"over budget", domain.UserRecord{Group: "acme", Limited: true}, "qwen3-4b", 429},
		{"model the group does not grant", domain.UserRecord{Group: "acme"}, "secret-model", 403},
		{"deny beats the group's grant",
			domain.UserRecord{Group: "acme", Deny: domain.ModelSet("qwen3-4b")}, "qwen3-4b", 403},
		{"granted", domain.UserRecord{Group: "acme"}, "qwen3-4b", 0},
		{"the user's own allow, on top of the group",
			domain.UserRecord{Group: "acme", Allow: domain.ModelSet("extra")}, "extra", 0},
	} {
		t.Run(c.name, func(t *testing.T) {
			store := memory.New()
			store.Keys[meterID()] = domain.KeyRecord{Status: "active", User: "ritwik"}
			store.Users["ritwik"] = c.user
			store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("qwen3-4b")}

			svc := serviceOver(store)
			id, err := svc.Identify(context.Background(), "Bearer "+secret)
			if err != nil {
				t.Fatalf("Identify: %v", err)
			}
			assertStatus(t, svc.Authorize(id, c.model), c.want)
		})
	}
}

// assertStatus checks the refusal status, with want 0 meaning "should have been admitted".
func assertStatus(t *testing.T, err error, want int) {
	t.Helper()
	if want == 0 {
		if err != nil {
			t.Fatalf("refused with %v, want admitted", err)
		}
		return
	}
	var denial domain.Denial
	if !errors.As(err, &denial) {
		t.Fatalf("err = %v, want a Denial with status %d", err, want)
	}
	if denial.Status != want {
		t.Errorf("status = %d (%s), want %d", denial.Status, denial.Reason, want)
	}
}
