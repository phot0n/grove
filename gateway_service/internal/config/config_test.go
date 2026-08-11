package config

import (
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// write puts a tunables file on disk and answers its path.
func write(t *testing.T, body string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

// The token is the only thing standing in front of /admin, which serves every key hash and every
// engine internal key. Anything that is not a real value has to stop the process, not degrade it.
func TestRequireAdminToken(t *testing.T) {
	for _, raw := range []string{"", " ", "\t\n"} {
		if _, err := RequireAdminToken(raw); err == nil {
			t.Errorf("RequireAdminToken(%q) started the agent with no admin token", raw)
		}
	}
	got, err := RequireAdminToken("  s3cret  ")
	if err != nil {
		t.Fatalf("RequireAdminToken rejected a real token: %v", err)
	}
	if got != "s3cret" {
		t.Errorf("RequireAdminToken = %q, want %q", got, "s3cret")
	}
}

func TestABoxIsOnePlaneOrTheOther(t *testing.T) {
	if _, err := ResolveMode("gw-1", "ing-1"); err == nil {
		t.Error("a box given both ids was accepted")
	}
	if id, err := ResolveMode("", "ing-1"); err != nil || id != "ing-1" {
		t.Errorf("got %q/%v, want ing-1/nil", id, err)
	}
	for _, gateway := range []string{"gw-1", "", "  "} {
		if id, err := ResolveMode(gateway, ""); err != nil || id != "" {
			t.Errorf("gateway %q: got %q/%v, want the gateway plane", gateway, id, err)
		}
	}
}

// A box that nobody has tuned yet still has to serve.
func TestAMissingFileIsDefaults(t *testing.T) {
	live, err := Open(filepath.Join(t.TempDir(), "absent.json"), Defaults(), quiet())
	if err != nil {
		t.Fatalf("a missing tunables file refused to start: %v", err)
	}
	if got := live.Get(); !sameResolved(got, Defaults()) {
		t.Errorf("got %+v, want the defaults", got)
	}
}

// A file someone wrote ON PURPOSE that cannot be read must stop the process. Serving defaults would
// look exactly like the file was applied.
func TestABadFileRefusesToStart(t *testing.T) {
	for _, body := range []string{
		`{`,                              // not JSON
		`{"log_level":"lowd"}`,           // not a level
		`{"synthetic_session_ttl":"30"}`, // no unit — the old env var read this as "off"
		`{"drain_timeout":"-5m"}`,        // negative
		`{"max_body_bytes":-1}`,
		`{"lgo_level":"debug"}`, // a typo'd key, which is a knob someone thinks they turned
	} {
		if _, err := Open(write(t, body), Defaults(), quiet()); err == nil {
			t.Errorf("%s was accepted", body)
		}
	}
}

// Only what the file names moves. Everything else keeps its default, so a file holding one line is
// a legitimate file.
func TestOmittedFieldsKeepTheirDefault(t *testing.T) {
	live, err := Open(write(t, `{"log_level":"debug"}`), Defaults(), quiet())
	if err != nil {
		t.Fatal(err)
	}
	got := live.Get()
	if got.LogLevel != slog.LevelDebug {
		t.Errorf("log_level = %v, want debug", got.LogLevel)
	}
	if got.DrainTimeout != Defaults().DrainTimeout {
		t.Errorf("drain_timeout moved to %v without being named", got.DrainTimeout)
	}
	if got.MaxBodyBytes != Defaults().MaxBodyBytes {
		t.Errorf("max_body_bytes moved to %d without being named", got.MaxBodyBytes)
	}
}

// Grove Settings stores this field as the literal "0", not "0s". Go special-cases a bare zero, and
// the strict parser must keep letting it through — rejecting it would refuse to start every gateway
// in the fleet at once, since "0" is the default every box is provisioned with.
func TestABareZeroIsAValidDuration(t *testing.T) {
	live, err := Open(write(t, `{"synthetic_session_ttl":"0"}`), Defaults(), quiet())
	if err != nil {
		t.Fatalf(`"0" was refused, which is what every box is provisioned with: %v`, err)
	}
	if got := live.Get().SyntheticSessionTTL; got != 0 {
		t.Errorf("synthetic_session_ttl = %v, want 0", got)
	}
}

// The whole point of the file: turning a knob is an edit and a signal.
func TestReloadAppliesAndReportsWhatChanged(t *testing.T) {
	path := write(t, `{"log_level":"info","synthetic_session_ttl":"0s"}`)
	live, err := Open(path, Defaults(), quiet())
	if err != nil {
		t.Fatal(err)
	}

	if err := os.WriteFile(path, []byte(`{"log_level":"debug","synthetic_session_ttl":"30m"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	_, next, changed, err := live.Reload()
	if err != nil {
		t.Fatalf("Reload: %v", err)
	}
	if next.SyntheticSessionTTL != 30*time.Minute || next.LogLevel != slog.LevelDebug {
		t.Errorf("reload did not apply: %+v", next)
	}
	if len(changed) != 2 {
		t.Errorf("changed = %v, want both fields named", changed)
	}
	if live.Get().SyntheticSessionTTL != 30*time.Minute {
		t.Error("the live value was not swapped")
	}
}

// The rule the whole reload path exists to honour: a typo in a hand-edited file must be a log line,
// never a change and never an outage.
func TestARejectedReloadChangesNothing(t *testing.T) {
	path := write(t, `{"synthetic_session_ttl":"30m","max_body_bytes":4096}`)
	live, err := Open(path, Defaults(), quiet())
	if err != nil {
		t.Fatal(err)
	}
	before := live.Get()

	// Valid JSON, invalid value — the dangerous shape, because it parses.
	if err := os.WriteFile(path, []byte(`{"synthetic_session_ttl":"banana","max_body_bytes":1}`), 0o644); err != nil {
		t.Fatal(err)
	}
	_, _, changed, err := live.Reload()
	if err == nil {
		t.Fatal("a bad tunables file was accepted")
	}
	if len(changed) != 0 {
		t.Errorf("changed = %v, want nothing", changed)
	}
	if !sameResolved(live.Get(), before) {
		t.Errorf("the running configuration moved to %+v, want %+v", live.Get(), before)
	}
}

// All-or-nothing: half a config file is not a state anyone asked for.
func TestAFileIsAppliedWholeOrNotAtAll(t *testing.T) {
	path := write(t, `{"log_level":"info"}`)
	live, _ := Open(path, Defaults(), quiet())

	// The first field is fine, the second is not.
	if err := os.WriteFile(path, []byte(`{"log_level":"debug","lame_duck":"nope"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := live.Reload(); err == nil {
		t.Fatal("a file with one bad field was accepted")
	}
	if live.Get().LogLevel != slog.LevelInfo {
		t.Error("the valid half of a rejected file was applied")
	}
}

// A reload that changes nothing should say so rather than reporting a change.
func TestReloadingAnUnchangedFileReportsNoChange(t *testing.T) {
	live, _ := Open(write(t, `{"log_level":"warn"}`), Defaults(), quiet())
	if _, _, changed, err := live.Reload(); err != nil || len(changed) != 0 {
		t.Errorf("changed = %v, err = %v, want no change", changed, err)
	}
}

// An empty list and an absent one are different: absent keeps the default chain, empty is an
// explicit "use the built-in order" and must not be read as "run nothing".
func TestAnAbsentListKeepsTheDefault(t *testing.T) {
	live, err := Open(write(t, `{"log_level":"info"}`), Defaults(), quiet())
	if err != nil {
		t.Fatal(err)
	}
	if got := live.Get().Transforms; !SameList(got, Defaults().Transforms) {
		t.Errorf("transforms = %v, want the default %v", got, Defaults().Transforms)
	}
}

func sameResolved(a, b Resolved) bool {
	return a.LogLevel == b.LogLevel &&
		SameList(a.Middleware, b.Middleware) &&
		SameList(a.Transforms, b.Transforms) &&
		a.SyntheticSessionTTL == b.SyntheticSessionTTL &&
		a.MaxBodyBytes == b.MaxBodyBytes &&
		a.UpstreamReadTimeout == b.UpstreamReadTimeout &&
		a.UpstreamTLSVerify == b.UpstreamTLSVerify &&
		a.DrainTimeout == b.DrainTimeout &&
		a.LameDuck == b.LameDuck &&
		a.UpgradeTimeout == b.UpgradeTimeout
}
