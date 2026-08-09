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

// pruneRoutes decides which deploy:<model> keys survive a complete push. The rule is the whole
// reason an ingress can stop naming every model in the catalogue on every sync.
func TestPruneKeepsOnlyWhatThePayloadNames(t *testing.T) {
	for _, c := range []struct {
		name    string
		held    []string
		payload []string
		stale   []string
	}{
		{"a model that moved away is dropped",
			[]string{"a", "b", "c"}, []string{"a"}, []string{"deploy:b", "deploy:c"}},
		{"nothing to do when the payload matches",
			[]string{"a", "b"}, []string{"a", "b"}, nil},
		{"a payload naming more than is held drops nothing",
			[]string{"a"}, []string{"a", "b"}, nil},
		{"an empty payload retires the whole table",
			[]string{"a", "b"}, nil, []string{"deploy:a", "deploy:b"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			keep := map[string][]Route{}
			for _, m := range c.payload {
				keep[m] = nil
			}
			var stale []string
			for _, model := range c.held {
				if _, ok := keep[model]; !ok {
					stale = append(stale, "deploy:"+model)
				}
			}
			if len(stale) != len(c.stale) {
				t.Fatalf("stale = %v, want %v", stale, c.stale)
			}
			for i := range stale {
				if stale[i] != c.stale[i] {
					t.Errorf("stale[%d] = %q, want %q", i, stale[i], c.stale[i])
				}
			}
		})
	}
}
