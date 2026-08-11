package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"
)

// Dynamic is the half of the configuration that is TUNED rather than declared: the knobs an
// operator reaches for while the fleet is running. It lives in a JSON file, re-read on SIGUSR1, so
// changing one is an edit and a signal rather than a deploy.
//
// The split is by lifetime, not by importance. Identity, secrets, sockets and paths stay in the
// environment: changing any of them means the process is a different process, and a restart is the
// honest way to say so. Everything here can change under a live request without the request
// noticing.
//
// Durations are strings ("30m", "600s") so the file reads the way the documentation does.
type Dynamic struct {
	LogLevel string `json:"log_level"`

	// Middleware is the data-path chain, in order. Empty means the built-in order for this plane.
	// A name that is not registered is REFUSED and the running chain is kept.
	Middleware []string `json:"middleware"`
	Transforms []string `json:"transforms"`

	// SyntheticSessionTTL is how long a caller that names no session is pinned to one engine.
	// "0" balances every such request; "30m" is the pre-2026-08 behaviour. The reason this file
	// exists at all: it was already documented as a knob and still needed a re-provision to turn.
	SyntheticSessionTTL string `json:"synthetic_session_ttl"`

	MaxBodyBytes        int64  `json:"max_body_bytes"`
	UpstreamReadTimeout string `json:"upstream_read_timeout"`
	UpstreamTLSVerify   *bool  `json:"upstream_tls_verify"`

	DrainTimeout   string `json:"drain_timeout"`
	LameDuck       string `json:"lame_duck"`
	UpgradeTimeout string `json:"upgrade_timeout"`
}

// Resolved is Dynamic with every string parsed once, so the request path never parses a duration.
type Resolved struct {
	LogLevel   slog.Level
	Middleware []string
	Transforms []string

	SyntheticSessionTTL time.Duration
	MaxBodyBytes        int64
	UpstreamReadTimeout time.Duration
	UpstreamTLSVerify   bool

	DrainTimeout   time.Duration
	LameDuck       time.Duration
	UpgradeTimeout time.Duration
}

// Defaults are what a box with no config file runs, and what any field the file omits falls back
// to. A missing file is not an error: a freshly provisioned box serves before anyone has tuned it.
func Defaults() Resolved {
	return Resolved{
		LogLevel:            slog.LevelInfo,
		Transforms:          []string{"streamusage"},
		SyntheticSessionTTL: 0,
		MaxBodyBytes:        32 << 20,
		UpstreamReadTimeout: 600 * time.Second,
		UpstreamTLSVerify:   false,
		DrainTimeout:        630 * time.Second,
		LameDuck:            5 * time.Second,
		UpgradeTimeout:      30 * time.Second,
	}
}

// Resolve parses a Dynamic over the given base, returning the first error rather than a partly
// applied config. All-or-nothing on purpose: half a config file is not a state anyone asked for,
// and the caller keeps what it already had.
func (d Dynamic) Resolve(base Resolved) (Resolved, error) {
	out := base
	var err error

	if raw := strings.TrimSpace(d.LogLevel); raw != "" {
		if out.LogLevel, err = parseLevelStrict(raw); err != nil {
			return Resolved{}, err
		}
	}
	if d.Middleware != nil {
		out.Middleware = d.Middleware
	}
	if d.Transforms != nil {
		out.Transforms = d.Transforms
	}
	for _, field := range []struct {
		name   string
		raw    string
		target *time.Duration
	}{
		{"synthetic_session_ttl", d.SyntheticSessionTTL, &out.SyntheticSessionTTL},
		{"upstream_read_timeout", d.UpstreamReadTimeout, &out.UpstreamReadTimeout},
		{"drain_timeout", d.DrainTimeout, &out.DrainTimeout},
		{"lame_duck", d.LameDuck, &out.LameDuck},
		{"upgrade_timeout", d.UpgradeTimeout, &out.UpgradeTimeout},
	} {
		if strings.TrimSpace(field.raw) == "" {
			continue
		}
		value, err := parseDurationStrict(field.raw)
		if err != nil {
			return Resolved{}, fmt.Errorf("%s: %w", field.name, err)
		}
		*field.target = value
	}
	if d.MaxBodyBytes != 0 {
		if d.MaxBodyBytes < 0 {
			return Resolved{}, errors.New("max_body_bytes must be positive")
		}
		out.MaxBodyBytes = d.MaxBodyBytes
	}
	if d.UpstreamTLSVerify != nil {
		out.UpstreamTLSVerify = *d.UpstreamTLSVerify
	}
	return out, nil
}

// ReadFile parses the tunables file. A missing file is the zero Dynamic and no error — every field
// then falls back to its default.
func ReadFile(path string) (Dynamic, error) {
	raw, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return Dynamic{}, nil
	}
	if err != nil {
		return Dynamic{}, err
	}
	var dynamic Dynamic
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	// An unknown key is a typo, and a silently ignored typo is a knob someone believes they turned.
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&dynamic); err != nil {
		return Dynamic{}, err
	}
	return dynamic, nil
}
