package middleware

import (
	"context"
	"net"
	"net/http"
	"strconv"
)

func or(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func itoa(n int) string { return strconv.Itoa(n) }

// statusText renders an upstream status for the health rule, which reads a blank string as "the hop
// never produced one" — a dial failure, a timeout, or a client that vanished first.
func statusText(status int) string {
	if status == 0 {
		return ""
	}
	return strconv.Itoa(status)
}

// clientIP is the peer's address. Deliberately not X-Forwarded-For: this is the edge, so a
// forwarding header is a claim by whoever connected, not a fact.
func clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// withoutCancel keeps a store call alive past the client hanging up. Releasing the in-flight slot
// and recording usage are exactly the work a cancelled request still owes.
func withoutCancel(ctx context.Context) context.Context { return context.WithoutCancel(ctx) }
