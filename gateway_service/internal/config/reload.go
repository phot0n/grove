package config

import (
	"log/slog"
	"sync/atomic"
)

// Live holds the current tunables and swaps them atomically. Readers take a copy and never lock; a
// request that started under the old values finishes under them, which is the correct behaviour for
// every knob here.
type Live struct {
	path    string
	base    Resolved
	log     *slog.Logger
	current atomic.Pointer[Resolved]
}

// Open reads the tunables file once and returns the holder.
//
// A file that is present but WRONG is a startup error: someone wrote it on purpose, and serving
// defaults that look like it was applied is worse than refusing to start. A file that is ABSENT is
// fine — that is an unprovisioned box, and defaults are the honest answer.
func Open(path string, base Resolved, log *slog.Logger) (*Live, error) {
	dynamic, err := ReadFile(path)
	if err != nil {
		return nil, err
	}
	resolved, err := dynamic.Resolve(base)
	if err != nil {
		return nil, err
	}
	live := &Live{path: path, base: base, log: log}
	live.current.Store(&resolved)
	return live, nil
}

// Get is the read every consumer does, once per request or once per drain. Cheap enough for either.
func (l *Live) Get() Resolved { return *l.current.Load() }

// Reload re-reads the file and, if it is valid, swaps it in — returning what changed.
//
// Nothing about a FAILED reload changes what is running: the file is malformed, or names a
// middleware that does not exist, and the gateway keeps serving exactly as it was with one error
// line saying why. The alternative — applying half of it, or refusing to serve — turns a typo into
// an outage, and a typo in a file edited by hand is the expected case rather than the unlucky one.
//
// Driven by SIGUSR1 rather than by watching the file. SIGHUP already means "upgrade", so this is
// the second of three signals with one meaning each, and an explicit signal says exactly when the
// change lands — where a watcher would apply a half-written file whenever the editor happened to
// flush.
func (l *Live) Reload() (previous, next Resolved, changed []string, err error) {
	previous = l.Get()

	dynamic, err := ReadFile(l.path)
	if err != nil {
		return previous, previous, nil, err
	}
	next, err = dynamic.Resolve(l.base)
	if err != nil {
		return previous, previous, nil, err
	}
	l.current.Store(&next)
	return previous, next, diff(previous, next), nil
}

// Path is where the tunables are read from, for the startup log.
func (l *Live) Path() string { return l.path }

// diff names what actually moved, so the log line says what someone changed rather than repeating
// the whole configuration on every reload.
func diff(previous, next Resolved) []string {
	var changed []string
	add := func(name string, differs bool) {
		if differs {
			changed = append(changed, name)
		}
	}
	add("log_level", previous.LogLevel != next.LogLevel)
	add("middleware", !SameList(previous.Middleware, next.Middleware))
	add("transforms", !SameList(previous.Transforms, next.Transforms))
	add("synthetic_session_ttl", previous.SyntheticSessionTTL != next.SyntheticSessionTTL)
	add("max_body_bytes", previous.MaxBodyBytes != next.MaxBodyBytes)
	add("upstream_read_timeout", previous.UpstreamReadTimeout != next.UpstreamReadTimeout)
	add("upstream_tls_verify", previous.UpstreamTLSVerify != next.UpstreamTLSVerify)
	add("drain_timeout", previous.DrainTimeout != next.DrainTimeout)
	add("lame_duck", previous.LameDuck != next.LameDuck)
	add("upgrade_timeout", previous.UpgradeTimeout != next.UpgradeTimeout)
	return changed
}

// SameList is exported because the reload path and its callers both need it to decide whether a
// list-shaped setting is worth rebuilding for.
func SameList(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
