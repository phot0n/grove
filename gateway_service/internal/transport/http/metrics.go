package http

import (
	"bufio"
	"crypto/subtle"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"

	"golang.org/x/crypto/bcrypt"
)

// /metrics/node — the Monitoring Agent scrapes node_exporter through here rather than over :9100,
// so the box needs nothing open beyond 22/80/443.
//
// The credential is the same htpasswd file the nginx it replaces read. The control plane hashes it
// with bcrypt, which nginx also accepts through crypt(3) — so the inference boxes, which still run
// nginx for their own /metrics, verify the identical file.
type metricsProxy struct {
	username string
	hash     string
	upstream *httputil.ReverseProxy
}

func newMetricsProxy(htpasswdPath, target string) (*metricsProxy, error) {
	upstream, err := url.Parse(target)
	if err != nil {
		return nil, err
	}
	username, hash, err := readHtpasswd(htpasswdPath)
	if err != nil {
		return nil, err
	}
	return &metricsProxy{
		username: username,
		hash:     hash,
		upstream: httputil.NewSingleHostReverseProxy(upstream),
	}, nil
}

func (m *metricsProxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	user, password, ok := r.BasicAuth()
	if !ok || !m.authorized(user, password) {
		w.Header().Set("WWW-Authenticate", `Basic realm="grove metrics"`)
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}
	m.upstream.ServeHTTP(w, r)
}

// authorized compares the username in constant time and the password against its stored hash.
//
// A blank hash matches nothing, so a box whose htpasswd never rendered keeps metrics shut rather
// than opening up — bcrypt would reject it anyway, but relying on that would make the safe outcome
// an accident of the library.
func (m *metricsProxy) authorized(user, password string) bool {
	if m.hash == "" {
		return false
	}
	if subtle.ConstantTimeCompare([]byte(user), []byte(m.username)) != 1 {
		return false
	}
	return bcrypt.CompareHashAndPassword([]byte(m.hash), []byte(password)) == nil
}

// readHtpasswd reads the single user:hash line the control plane renders.
func readHtpasswd(path string) (username, hash string, err error) {
	file, err := os.Open(path)
	if err != nil {
		return "", "", err
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		user, digest, found := strings.Cut(line, ":")
		if !found {
			continue
		}
		return user, digest, nil
	}
	return "", "", scanner.Err()
}
