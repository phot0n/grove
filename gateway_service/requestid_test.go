package main

import (
	"regexp"
	"testing"
)

func TestCleanIDPart(t *testing.T) {
	cases := map[string]string{
		"inf-blackwell": "inf_blackwell", // '-' → '_' so the separator stays unambiguous
		"u4j55nfboc":    "u4j55nfboc",
		"a b:c/d":       "abcd", // spaces + punctuation dropped
		"":              "x",    // never empty (would collapse the id)
	}
	for in, want := range cases {
		if got := cleanIDPart(in); got != want {
			t.Errorf("cleanIDPart(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestBuildRequestID(t *testing.T) {
	s := &server{gatewayID: "proxy-sg"}
	shape := regexp.MustCompile(`^gr-proxy-sg-[a-z0-9_]+-u4j55nfboc-[0-9a-f]{12}$`)

	// Server id present → used verbatim (sanitized).
	rid := s.buildRequestID(Route{Server: "inf-blackwell", EngineURL: "http://x:8080"}, "u4j55nfboc")
	if !shape.MatchString(rid) {
		t.Errorf("with server: %q does not match %v", rid, shape)
	}
	// No server id → falls back to a short hash of the engine URL (still matches the shape).
	rid2 := s.buildRequestID(Route{EngineURL: "http://x:8080"}, "u4j55nfboc")
	if !shape.MatchString(rid2) {
		t.Errorf("fallback: %q does not match %v", rid2, shape)
	}
	if rid == rid2 {
		t.Error("random tail should differ between calls")
	}
}
