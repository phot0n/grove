package middleware

import (
	"bytes"
	"io"
	"log/slog"
	"mime"
	"mime/multipart"
	"net/http"
	"os"
	"strings"
)

// The OpenAI endpoints that carry a file — audio/transcriptions, audio/translations, images/edits,
// files — need the same two facts as any other request: the model, and optionally a session hint.
// The difference is that the body must not be materialised to get them.

const (
	modelField   = "model"
	sessionField = "user"
	// maxFieldBytes bounds a form value this reads. A model name is tens of bytes; the limit is here
	// so a part *named* `model` cannot make the gateway hold a large one.
	maxFieldBytes = 4 << 10
)

// spillThreshold is how much of a capture is held in memory before it goes to a file — nginx's
// client_body_buffer_size, and the same reasoning. A var so a test can lower it.
var spillThreshold = 1 << 20

// spillBuffer captures what the parse consumed: in memory up to the threshold, on disk past it. The
// file lands in os.TempDir(), so a box that wants it elsewhere sets TMPDIR on the unit.
type spillBuffer struct {
	mem     bytes.Buffer
	file    *os.File
	log     *slog.Logger
	noSpill bool // CreateTemp failed once — stop trying it on every write
}

func (s *spillBuffer) Write(p []byte) (int, error) {
	if s.file == nil {
		if s.noSpill || s.mem.Len()+len(p) <= spillThreshold {
			return s.mem.Write(p)
		}
		file, err := os.CreateTemp("", "grove-upload-*")
		if err != nil {
			// Degrade rather than refuse: staying in memory is still bounded by max_body_bytes, so
			// an unwritable spill directory costs memory, not uploads.
			s.log.Warn("could not spill an upload to disk, holding it in memory",
				"dir", os.TempDir(), "err", err)
			s.noSpill = true
			return s.mem.Write(p)
		}
		// Unlinked while open: the descriptor is the only handle left, so the space comes back when
		// it closes and nothing is stranded if the process dies first.
		_ = os.Remove(file.Name())
		s.file = file
	}
	return s.file.Write(p)
}

// Reader replays the capture in order — what stayed in memory, then what went to disk.
func (s *spillBuffer) Reader() (io.Reader, error) {
	if s.file == nil {
		return &s.mem, nil
	}
	if _, err := s.file.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	return io.MultiReader(&s.mem, s.file), nil
}

func (s *spillBuffer) Close() error {
	if s.file == nil {
		return nil
	}
	file := s.file
	s.file = nil // Close runs twice: once from the proxy, once from the server finishing the request
	return file.Close()
}

// readCloser replaces a request body while net/http keeps closing the connection's real one, and
// takes the spill file with it so a temp file cannot outlive its request.
type readCloser struct {
	io.Reader
	spill  *spillBuffer
	origin io.Closer
}

func (rc readCloser) Close() error {
	_ = rc.spill.Close()
	return rc.origin.Close()
}

// multipartBoundary answers "" for anything that is not a multipart body, which is the signal to
// take the JSON path.
func multipartBoundary(r *http.Request) string {
	mediaType, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || !strings.HasPrefix(mediaType, "multipart/") {
		return ""
	}
	return params["boundary"]
}

// readMultipart takes the routing fields and hands the body on byte-exact, replaying what the tee
// captured (including read-ahead) before the unread rest. It stops at `model`, so a form leading
// with it captures almost nothing. No model part is refused later by the access check, as with JSON.
func readMultipart(w http.ResponseWriter, r *http.Request, boundary string, limit int64, log *slog.Logger) (model, session string, err error) {
	original := r.Body
	limited := http.MaxBytesReader(w, original, limit)

	seen := &spillBuffer{log: log}
	reader := multipart.NewReader(io.TeeReader(limited, seen), boundary)

	for {
		part, perr := reader.NextPart()
		if perr == io.EOF {
			break
		}
		if perr != nil {
			err = perr
			break
		}
		name := part.FormName()
		if part.FileName() == "" && (name == modelField || name == sessionField) {
			value, verr := readFormValue(part)
			part.Close()
			if verr != nil {
				err = verr
				break
			}
			if name == modelField {
				model = value
				break
			}
			session = value
			continue
		}
		// Everything else, every file part included, is walked past and never held. The tee already
		// has these bytes for the upstream; this only advances the reader to the next boundary.
		_, cerr := io.Copy(io.Discard, part)
		part.Close()
		if cerr != nil {
			err = cerr
			break
		}
	}

	replay, rerr := seen.Reader()
	if rerr != nil {
		// The capture is unreadable, so the body cannot be put back together. Refusing is the only
		// honest answer — forwarding what is left would send the engine a truncated form.
		_ = seen.Close()
		return "", "", rerr
	}
	r.Body = readCloser{Reader: io.MultiReader(replay, limited), spill: seen, origin: original}
	return model, session, err
}

// readFormValue takes a bounded prefix of a part and drains the rest, so the reader still lands on
// the next boundary when a value was longer than the gateway will hold.
func readFormValue(part *multipart.Part) (string, error) {
	value, err := io.ReadAll(io.LimitReader(part, maxFieldBytes))
	if err != nil {
		return "", err
	}
	if _, err := io.Copy(io.Discard, part); err != nil {
		return "", err
	}
	return strings.TrimSpace(string(value)), nil
}
