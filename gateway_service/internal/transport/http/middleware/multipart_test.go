package middleware

import (
	"bytes"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// What the capture does when the model field sits behind a large file: it has to hold everything it
// walked past, and where it holds it is the difference between a temp file and the heap.

func discardLog() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func form(t *testing.T, fields ...string) (body []byte, contentType string) {
	t.Helper()
	var buf bytes.Buffer
	writer := multipart.NewWriter(&buf)
	for i := 0; i+1 < len(fields); i += 2 {
		name, value := fields[i], fields[i+1]
		var (
			part io.Writer
			err  error
		)
		if strings.HasPrefix(name, "@") {
			part, err = writer.CreateFormFile(strings.TrimPrefix(name, "@"), "sample.wav")
		} else {
			part, err = writer.CreateFormField(name)
		}
		if err != nil {
			t.Fatalf("multipart field %q: %v", name, err)
		}
		if _, err := io.WriteString(part, value); err != nil {
			t.Fatalf("multipart write %q: %v", name, err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("multipart close: %v", err)
	}
	return buf.Bytes(), writer.FormDataContentType()
}

// parse runs one body through readMultipart and returns the model plus what would reach the engine.
func parse(t *testing.T, body []byte, contentType string, limit int64) (string, []byte) {
	t.Helper()
	r := httptest.NewRequest(http.MethodPost, "/v1/audio/transcriptions", bytes.NewReader(body))
	r.Header.Set("Content-Type", contentType)
	boundary := multipartBoundary(r)
	if boundary == "" {
		t.Fatal("content type was not read as multipart")
	}
	model, _, err := readMultipart(httptest.NewRecorder(), r, boundary, limit, discardLog())
	if err != nil {
		t.Fatalf("readMultipart: %v", err)
	}
	forwarded, err := io.ReadAll(r.Body)
	if err != nil {
		t.Fatalf("reading the forwarded body: %v", err)
	}
	if err := r.Body.Close(); err != nil {
		t.Fatalf("closing the forwarded body: %v", err)
	}
	return model, forwarded
}

func lowerThreshold(t *testing.T, to int) {
	t.Helper()
	previous := spillThreshold
	spillThreshold = to
	t.Cleanup(func() { spillThreshold = previous })
}

func openDescriptors(t *testing.T) int {
	t.Helper()
	entries, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Skip("no /proc on this platform, cannot count descriptors")
	}
	return len(entries)
}

// The buffer itself: nothing on disk below the threshold, a file above it, and a replay that puts
// the memory half back in front of the disk half. Everything below leans on this being true.
func TestTheBufferOpensAFileOnlyOncePastTheThreshold(t *testing.T) {
	lowerThreshold(t, 16)
	buffer := &spillBuffer{log: discardLog()}
	t.Cleanup(func() { _ = buffer.Close() })

	if _, err := buffer.Write([]byte("small")); err != nil {
		t.Fatalf("write below the threshold: %v", err)
	}
	if buffer.file != nil {
		t.Fatal("opened a file for 5 bytes with a 16 byte threshold")
	}

	tail := bytes.Repeat([]byte("x"), 64)
	if _, err := buffer.Write(tail); err != nil {
		t.Fatalf("write past the threshold: %v", err)
	}
	if buffer.file == nil {
		t.Fatal("never opened a file for 69 bytes with a 16 byte threshold")
	}

	replay, err := buffer.Reader()
	if err != nil {
		t.Fatalf("Reader: %v", err)
	}
	got, err := io.ReadAll(replay)
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if want := append([]byte("small"), tail...); !bytes.Equal(got, want) {
		t.Errorf("replay = %q, want %q — memory and disk halves are out of order", got, want)
	}
}

// The bytes that went to disk have to come back in front of the ones still unread, in order, or the
// engine gets a form with a hole in it.
func TestACaptureTooBigForMemorySpillsAndReplaysExactly(t *testing.T) {
	lowerThreshold(t, 512)
	body, contentType := form(t, "@file", strings.Repeat("audio", 4096), "model", "qwen3-4b")

	model, forwarded := parse(t, body, contentType, 1<<20)

	if model != "qwen3-4b" {
		t.Errorf("model = %q, want qwen3-4b", model)
	}
	if !bytes.Equal(forwarded, body) {
		t.Errorf("forwarded %d bytes, sent %d — the spill did not replay in order",
			len(forwarded), len(body))
	}
}

// The file is unlinked as soon as it is created, so the descriptor is the only thing holding the
// space. Leaking one leaks the disk with it.
func TestASpilledUploadReleasesItsTempFile(t *testing.T) {
	lowerThreshold(t, 512)
	before := openDescriptors(t)

	for range 20 {
		body, contentType := form(t, "@file", strings.Repeat("x", 8192), "model", "qwen3-4b")
		if _, forwarded := parse(t, body, contentType, 1<<20); !bytes.Equal(forwarded, body) {
			t.Fatal("body was not replayed")
		}
	}

	// A couple of descriptors of slack: the runtime opens and closes its own during the loop.
	if after := openDescriptors(t); after > before+2 {
		t.Errorf("descriptors went %d → %d over 20 spilled uploads, a temp file was not closed",
			before, after)
	}
}

// An unwritable spill directory must cost memory, not uploads. `max_body_bytes` still bounds it, so
// falling back is safe — refusing the request would not be.
func TestAnUnwritableSpillDirectoryFallsBackToMemory(t *testing.T) {
	lowerThreshold(t, 512)
	t.Setenv("TMPDIR", filepath.Join(t.TempDir(), "not-a-directory"))
	body, contentType := form(t, "@file", strings.Repeat("x", 8192), "model", "qwen3-4b")

	model, forwarded := parse(t, body, contentType, 1<<20)

	if model != "qwen3-4b" {
		t.Errorf("model = %q, want qwen3-4b", model)
	}
	if !bytes.Equal(forwarded, body) {
		t.Error("the fallback did not replay the body")
	}
}

// Nothing touches disk when the model is where a sensible client puts it.
func TestAModelFirstFormNeverSpills(t *testing.T) {
	lowerThreshold(t, 512)
	before := openDescriptors(t)
	body, contentType := form(t, "model", "qwen3-4b", "@file", strings.Repeat("x", 65536))

	_, forwarded := parse(t, body, contentType, 1<<20)

	if !bytes.Equal(forwarded, body) {
		t.Error("body was not replayed")
	}
	if after := openDescriptors(t); after > before {
		t.Errorf("descriptors went %d → %d — a model-first form should never open a file",
			before, after)
	}
}
