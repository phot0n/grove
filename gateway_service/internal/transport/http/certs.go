package http

import (
	"crypto/tls"
	"fmt"
	"log/slog"
	"os"
	"sync"
	"time"
)

// certLoader serves the certificate from disk and picks up a replacement without a restart, which
// is what makes a renewal a file copy — the control plane pushes the fleet wildcard and the next
// handshake uses it. nginx needed a reload for the same thing.
type certLoader struct {
	certPath, keyPath string
	log               *slog.Logger

	mu       sync.RWMutex
	current  *tls.Certificate
	modTime  time.Time
	lastStat time.Time
}

// statInterval bounds how often the files are checked. A handshake is not a cheap operation, but it
// is far cheaper than a stat on every one of them under load.
const statInterval = time.Second

func newCertLoader(certPath, keyPath string, log *slog.Logger) (*certLoader, error) {
	loader := &certLoader{certPath: certPath, keyPath: keyPath, log: log}
	// Loaded once here so a bad path or an unreadable key is a startup failure, not a handshake
	// failure discovered by the first customer.
	if err := loader.reload(); err != nil {
		return nil, err
	}
	return loader, nil
}

// GetCertificate is the tls.Config hook. It never returns an error for a failed reload: the
// certificate already in hand is better than refusing the handshake, and a renewal that wrote a
// half-file is a case that resolves itself on the next stat.
func (c *certLoader) GetCertificate(*tls.ClientHelloInfo) (*tls.Certificate, error) {
	c.mu.RLock()
	current, lastStat := c.current, c.lastStat
	c.mu.RUnlock()

	if time.Since(lastStat) >= statInterval {
		if err := c.reloadIfChanged(); err != nil {
			c.log.Warn("keeping the certificate already loaded", "path", c.certPath, "err", err)
		}
		c.mu.RLock()
		current = c.current
		c.mu.RUnlock()
	}
	if current == nil {
		return nil, fmt.Errorf("no certificate loaded from %s", c.certPath)
	}
	return current, nil
}

func (c *certLoader) reloadIfChanged() error {
	info, err := os.Stat(c.certPath)
	if err != nil {
		c.markStatted()
		return err
	}
	c.mu.RLock()
	unchanged := info.ModTime().Equal(c.modTime)
	c.mu.RUnlock()
	if unchanged {
		c.markStatted()
		return nil
	}
	return c.reload()
}

func (c *certLoader) reload() error {
	pair, err := tls.LoadX509KeyPair(c.certPath, c.keyPath)
	if err != nil {
		c.markStatted()
		return err
	}
	info, err := os.Stat(c.certPath)
	if err != nil {
		c.markStatted()
		return err
	}
	c.mu.Lock()
	c.current, c.modTime, c.lastStat = &pair, info.ModTime(), time.Now()
	c.mu.Unlock()
	c.log.Info("certificate loaded", "path", c.certPath, "modified", info.ModTime())
	return nil
}

// markStatted records the attempt even when it failed, so a missing file is not re-stated on every
// handshake.
func (c *certLoader) markStatted() {
	c.mu.Lock()
	c.lastStat = time.Now()
	c.mu.Unlock()
}
