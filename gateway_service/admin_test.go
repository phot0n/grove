package main

import "testing"

// The token is the only thing standing in front of /admin, which serves every key hash and every
// engine internal key. Anything that is not a real value has to stop the process, not degrade it.
func TestRequireAdminToken(t *testing.T) {
	for _, raw := range []string{"", " ", "\t\n"} {
		if _, err := requireAdminToken(raw); err == nil {
			t.Errorf("requireAdminToken(%q) started the agent with no admin token", raw)
		}
	}
	got, err := requireAdminToken("  s3cret  ")
	if err != nil {
		t.Fatalf("requireAdminToken rejected a real token: %v", err)
	}
	if got != "s3cret" {
		t.Errorf("requireAdminToken = %q, want %q", got, "s3cret")
	}
}
