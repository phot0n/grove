package main

import (
	"testing"
	"time"
)

// The knob that decides whether a caller naming no session is balanced or pinned. Anything that
// is not a positive duration means off, including a typo: a fleet reading a misspelt value as
// "pin everything" would silently serve one engine of two.
func TestParseSyntheticTTL(t *testing.T) {
	cases := []struct {
		raw  string
		want time.Duration
	}{
		{"", 0},
		{"  ", 0},
		{"0", 0},
		{"30m", 30 * time.Minute},
		{" 2m ", 2 * time.Minute},
		{"-5m", 0},
		{"banana", 0},
		{"30", 0}, // no unit is not a duration
	}
	for _, c := range cases {
		if got := parseSyntheticTTL(c.raw); got != c.want {
			t.Errorf("parseSyntheticTTL(%q) = %v, want %v", c.raw, got, c.want)
		}
	}
}
