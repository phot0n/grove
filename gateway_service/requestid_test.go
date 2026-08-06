package main

import (
	"regexp"
	"strings"
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
	// Every part is cleanIDPart'd, so the gateway's own '-' becomes '_' too and the only '-' left
	// is the separator. Case is preserved: a target reads as the doc name it came from. The tail
	// is left unsized — its length is a tunable, and pinning it here only breaks this test when
	// someone deliberately changes the entropy.
	shape := regexp.MustCompile(`^gr-proxy_sg-[A-Za-z0-9_]+-u4j55nfboc-[0-9a-f]+$`)

	// Deployment present → used verbatim (sanitized), even though a server id is there too.
	// The box cannot name the engine: it serves both deployments below.
	rid := s.buildRequestID(
		Route{Deployment: "MD-00007", Server: "inf-blackwell", EngineURL: "https://x/e/md-00007"},
		"u4j55nfboc",
	)
	if !shape.MatchString(rid) {
		t.Errorf("with deployment: %q does not match %v", rid, shape)
	}
	if !strings.Contains(rid, "MD_00007") {
		t.Errorf("with deployment: %q should name the deployment, not the box", rid)
	}

	// Two deployments of one model on one box: same server, different ids.
	other := s.buildRequestID(
		Route{Deployment: "MD-00008", Server: "inf-blackwell", EngineURL: "https://x/e/md-00008"},
		"u4j55nfboc",
	)
	if strings.Contains(other, "MD_00007") {
		t.Errorf("second deployment: %q leaked the first one's id", other)
	}

	// Route pushed before the deployment field existed → falls back to the server id.
	legacy := s.buildRequestID(Route{Server: "inf-blackwell", EngineURL: "http://x:8080"}, "u4j55nfboc")
	if !shape.MatchString(legacy) || !strings.Contains(legacy, "inf_blackwell") {
		t.Errorf("legacy route: %q should fall back to the server", legacy)
	}

	// Neither → a short hash of the engine URL (still matches the shape).
	rid2 := s.buildRequestID(Route{EngineURL: "http://x:8080"}, "u4j55nfboc")
	if !shape.MatchString(rid2) {
		t.Errorf("fallback: %q does not match %v", rid2, shape)
	}
	if rid == rid2 {
		t.Error("random tail should differ between calls")
	}
}
