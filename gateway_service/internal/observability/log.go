// Package observability sets up the two loggers this process writes: diagnostics, and the durable
// per-request record.
package observability

import (
	"io"
	"log/slog"
	"os"
	"path/filepath"
)

// Loggers are deliberately two.
//
// Process is diagnostics — why a route was chosen, why a request was denied, what an upstream did.
// JSON on stdout, which systemd captures into journald and the shipper reads from there.
//
// Access is one line per request, in its own file. Separate because it is a RECORD rather than a
// diagnostic: it is what someone greps months later for a request id, it must not move when the log
// level does, and mixing it into stdout would bury it under debug output at exactly the times
// anyone turns debug on.
type Loggers struct {
	Process *slog.Logger
	Access  *slog.Logger
}

type Options struct {
	// Level is a LevelVar rather than a Level so a reload can move it under a running process:
	// slog reads it on every record, which is the whole reason it exists.
	Level *slog.LevelVar
	// AccessLogPath is the file the per-request line goes to. Blank sends it to stdout alongside
	// the diagnostics, which is what a local run wants and what a box gets before the directory
	// exists.
	AccessLogPath string
}

// New builds both loggers and returns a close for the access-log file.
func New(opts Options) (Loggers, func(), error) {
	level := opts.Level
	if level == nil {
		level = new(slog.LevelVar)
	}
	process := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level}))
	noop := func() {}

	if opts.AccessLogPath == "" {
		return Loggers{Process: process, Access: process}, noop, nil
	}
	if err := os.MkdirAll(filepath.Dir(opts.AccessLogPath), 0o755); err != nil {
		return Loggers{}, noop, err
	}
	file, err := os.OpenFile(opts.AccessLogPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o640)
	if err != nil {
		return Loggers{}, noop, err
	}
	// Always at Info: the access line is the record, and a level change is about diagnostics.
	access := slog.New(slog.NewJSONHandler(file, &slog.HandlerOptions{Level: slog.LevelInfo}))
	return Loggers{Process: process, Access: access}, func() { _ = file.Close() }, nil
}

// Discard is for tests, which want neither file.
func Discard() Loggers {
	quiet := slog.New(slog.NewJSONHandler(io.Discard, nil))
	return Loggers{Process: quiet, Access: quiet}
}
