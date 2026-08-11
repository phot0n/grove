package observability

import (
	"context"
	"log/slog"
)

// fanout sends one record to several handlers, each keeping its own level. That is what lets the
// process log go to stdout at whatever level is configured while the same record, if it is a
// warning or worse, also lands in a file that never moves with that level.
type fanout struct{ handlers []slog.Handler }

func (f fanout) Enabled(ctx context.Context, level slog.Level) bool {
	for _, h := range f.handlers {
		if h.Enabled(ctx, level) {
			return true
		}
	}
	return false
}

// Handle clones the record per handler: slog's contract says a handler may not retain one, and two
// sharing a record is exactly the aliasing that rule exists to prevent.
func (f fanout) Handle(ctx context.Context, record slog.Record) error {
	var firstErr error
	for _, h := range f.handlers {
		if !h.Enabled(ctx, record.Level) {
			continue
		}
		if err := h.Handle(ctx, record.Clone()); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	return firstErr
}

func (f fanout) WithAttrs(attrs []slog.Attr) slog.Handler {
	next := make([]slog.Handler, len(f.handlers))
	for i, h := range f.handlers {
		next[i] = h.WithAttrs(attrs)
	}
	return fanout{handlers: next}
}

func (f fanout) WithGroup(name string) slog.Handler {
	next := make([]slog.Handler, len(f.handlers))
	for i, h := range f.handlers {
		next[i] = h.WithGroup(name)
	}
	return fanout{handlers: next}
}
