package domain

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

// BuildRequestID stamps gr-<gateway>-<deployment>-<key prefix>-<random>, sanitized so the only '-'
// is the separator. The target is the Model Deployment, not the box: one box can serve a model from
// two deployments. Falls back to the server, then a hash of the engine URL — each worse, none wrong.
func BuildRequestID(gatewayID string, route Route, keyPrefix string) string {
	target := route.Deployment
	if target == "" {
		target = route.Server
	}
	if target == "" {
		target = SHA256Hex(route.EngineURL)[:8]
	}
	return fmt.Sprintf("gr-%s-%s-%s-%s",
		CleanIDPart(gatewayID), CleanIDPart(target), CleanIDPart(keyPrefix), RandHex(16))
}

// CleanIDPart keeps a request-id part parseable: alnum stays, '-' becomes '_' (so the only
// '-' left is the separator), everything else is dropped.
func CleanIDPart(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '_':
			b.WriteRune(r)
		case r == '-':
			b.WriteByte('_')
		}
	}
	if b.Len() == 0 {
		return "x"
	}
	return b.String()
}

func RandHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return strings.Repeat("0", n*2)
	}
	return hex.EncodeToString(b)
}

// SHA256Hex is the fleet's one hashing rule: it turns an API secret into the meter id that names
// its Redis record, and a caller-chosen session into the opaque key an ingress is allowed to see.
func SHA256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// Bearer strips the scheme off an Authorization header. A bare token is accepted as-is: some
// clients send one, and refusing it would fail a request over a formatting detail.
func Bearer(header string) string {
	header = strings.TrimSpace(header)
	if strings.HasPrefix(strings.ToLower(header), "bearer ") {
		return strings.TrimSpace(header[7:])
	}
	return header
}
