// Package observability sets up the two loggers this process writes: diagnostics, and the durable
// per-request record.
package observability

import (
	"io"
	"log/slog"
	"os"
	"path/filepath"
)

// Two loggers on purpose. Process is diagnostics on stdout for journald, mirrored at Warn and above
// into error.log so a failure is greppable without journalctl. Access is one line per request in its
// own file: a RECORD, not a diagnostic, so it must not move when the log level does.
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
	// ErrorLogPath receives Warn and above, in addition to stdout. Blank keeps stdout alone.
	ErrorLogPath string
}

// New builds both loggers and returns a close for the files it opened.
func New(opts Options) (Loggers, func(), error) {
	level := opts.Level
	if level == nil {
		level = new(slog.LevelVar)
	}
	var open []*os.File
	closeAll := func() {
		for _, f := range open {
			_ = f.Close()
		}
	}

	handlers := []slog.Handler{slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level})}
	if opts.ErrorLogPath != "" {
		file, err := openLog(opts.ErrorLogPath)
		if err != nil {
			closeAll()
			return Loggers{}, func() {}, err
		}
		open = append(open, file)
		// Pinned at Warn: the point of a separate file is that turning the process log down to Error
		// or up to Debug does not change what a failure hunt finds in it.
		handlers = append(handlers, slog.NewJSONHandler(file, &slog.HandlerOptions{Level: slog.LevelWarn}))
	}
	process := slog.New(fanout{handlers: handlers})

	if opts.AccessLogPath == "" {
		return Loggers{Process: process, Access: process}, closeAll, nil
	}
	file, err := openLog(opts.AccessLogPath)
	if err != nil {
		closeAll()
		return Loggers{}, func() {}, err
	}
	open = append(open, file)
	// Always at Info: the access line is the record, and a level change is about diagnostics.
	access := slog.New(slog.NewJSONHandler(file, &slog.HandlerOptions{Level: slog.LevelInfo}))
	return Loggers{Process: process, Access: access}, closeAll, nil
}

func openLog(path string) (*os.File, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	return os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o640)
}

// Discard is for tests, which want neither file.
func Discard() Loggers {
	quiet := slog.New(slog.NewJSONHandler(io.Discard, nil))
	return Loggers{Process: quiet, Access: quiet}
}
