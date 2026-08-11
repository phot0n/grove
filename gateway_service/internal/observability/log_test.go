package observability

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The error log exists so a failure is greppable without journalctl, and so it keeps saying the
// same thing when someone moves the process log level while hunting one.

func read(t *testing.T, path string) string {
	t.Helper()
	body, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(body)
}

func TestTheErrorLogTakesWarningsAndWorseOnly(t *testing.T) {
	dir := t.TempDir()
	errorLog := filepath.Join(dir, "error.log")
	logs, closeAll, err := New(Options{ErrorLogPath: errorLog, AccessLogPath: filepath.Join(dir, "access.log")})
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	logs.Process.Debug("a debug line")
	logs.Process.Info("an info line")
	logs.Process.Warn("a warn line")
	logs.Process.Error("an error line")
	closeAll()

	body := read(t, errorLog)
	for _, want := range []string{"a warn line", "an error line"} {
		if !strings.Contains(body, want) {
			t.Errorf("error.log is missing %q: %s", want, body)
		}
	}
	for _, unwanted := range []string{"a debug line", "an info line"} {
		if strings.Contains(body, unwanted) {
			t.Errorf("error.log picked up %q, which belongs on stdout only", unwanted)
		}
	}
}

// The level knob is for diagnostics. Turning it to Error must not empty the file a failure hunt
// reads, and turning it to Debug must not fill it with noise.
func TestTheErrorLogIgnoresTheProcessLevel(t *testing.T) {
	for _, level := range []slog.Level{slog.LevelDebug, slog.LevelError} {
		dir := t.TempDir()
		errorLog := filepath.Join(dir, "error.log")
		var configured slog.LevelVar
		configured.Set(level)

		logs, closeAll, err := New(Options{Level: &configured, ErrorLogPath: errorLog})
		if err != nil {
			t.Fatalf("New: %v", err)
		}
		logs.Process.Warn("a warn line")
		logs.Process.Debug("a debug line")
		closeAll()

		body := read(t, errorLog)
		if !strings.Contains(body, "a warn line") {
			t.Errorf("level %v: warning missing from error.log: %s", level, body)
		}
		if strings.Contains(body, "a debug line") {
			t.Errorf("level %v: debug leaked into error.log", level)
		}
	}
}

// Blank means stdout only, which is what a local run and an unprovisioned box both get.
func TestNoErrorLogPathWritesNoFile(t *testing.T) {
	dir := t.TempDir()
	logs, closeAll, err := New(Options{})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	logs.Process.Error("an error line")
	closeAll()

	if entries, _ := os.ReadDir(dir); len(entries) != 0 {
		t.Errorf("wrote %d files with no paths configured", len(entries))
	}
}
