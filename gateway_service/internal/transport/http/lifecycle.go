package http

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/cloudflare/tableflip"
)

// Lifecycle owns the listeners, the drain flag and the upgrade handshake.
//
// Three separate events, deliberately not one mechanism:
//
//   - SIGTERM drains: stop accepting, let live handlers finish, then exit. This traffic is long — a
//     streaming completion runs for minutes — so the drain window is minutes too.
//   - The drain flag flips first, which takes the box out of any health check in front of it before
//     the socket goes anywhere, and gives a new request on an already-open connection a real
//     message instead of a reset.
//   - SIGHUP upgrades: a child process inherits the listening sockets and starts accepting on them
//     immediately, while the parent drains behind it. No connection is refused and none is queued
//     unanswered, which a stop/start cannot manage when the drain takes minutes.
type Lifecycle struct {
	log      *slog.Logger
	upgrader *tableflip.Upgrader
	opts     LifecycleOptions

	draining atomic.Bool

	mu      sync.Mutex
	servers []*http.Server
}

type LifecycleOptions struct {
	// Windows is read when a drain BEGINS, not when the process started — so an operator who is
	// about to restart a busy gateway can lengthen the drain first and have it apply to that very
	// restart. Nil takes the defaults.
	Windows func() DrainWindows
	// UpgradeTimeout bounds how long the parent waits for a child to say it is serving. On expiry
	// the parent keeps serving on the old binary — a bad deploy does nothing rather than causing an
	// outage. Read once: tableflip takes it at construction.
	UpgradeTimeout time.Duration
	// PIDFile lets a second process find the running one. Optional.
	PIDFile string
	// OnReload runs on SIGUSR1. Three signals, one meaning each: SIGHUP swaps the binary, SIGUSR1
	// re-reads the tunables, SIGTERM drains. Nothing here shares a signal with anything else,
	// because a signal that means two things is the one somebody sends at 3am for the other reason.
	OnReload func()
}

// DrainWindows are the two clocks a shutdown runs on.
type DrainWindows struct {
	// Total bounds how long live handlers get after the socket stops accepting. Longer than the
	// upstream read timeout, so a stream the engine would have finished is never cut here first.
	Total time.Duration
	// LameDuck is how long the process keeps ACCEPTING connections after it has begun shutting
	// down, answering them 503.
	//
	// Without it the drain flag is nearly unobservable: Server.Shutdown closes the listener at
	// once, so a health checker or a client opening a NEW connection gets connection-refused and
	// only an already-open keep-alive connection ever sees the flag. Refused is not wrong — a
	// checker marks the box down either way — but it is a reset rather than an answer, and it gives
	// a client nothing to retry on. One poll interval of honest 503s is what makes the box leave
	// rotation before its socket does.
	LameDuck time.Duration
}

func (w DrainWindows) withDefaults() DrainWindows {
	if w.Total <= 0 {
		w.Total = 630 * time.Second
	}
	if w.LameDuck < 0 {
		w.LameDuck = 0
	}
	return w
}

func (l *Lifecycle) windows() DrainWindows {
	if l.opts.Windows == nil {
		return DrainWindows{}.withDefaults()
	}
	return l.opts.Windows().withDefaults()
}

func (o LifecycleOptions) withDefaults() LifecycleOptions {
	if o.UpgradeTimeout <= 0 {
		o.UpgradeTimeout = 30 * time.Second
	}
	return o
}

func NewLifecycle(opts LifecycleOptions, log *slog.Logger) (*Lifecycle, error) {
	opts = opts.withDefaults()
	// tableflip spawns the child from os.Args[0], resolved fresh at exec time — which is what makes
	// an upgrade pick up a binary Ansible replaced underneath a running process. A relative argv[0]
	// resolves against the working directory instead, so the upgrade would fail later for a reason
	// nobody would connect back to how the process was started. Said once, at startup, rather than
	// discovered during a deploy.
	if !filepath.IsAbs(os.Args[0]) {
		log.Warn("started with a relative path — SIGHUP upgrades will resolve it against the working "+
			"directory and may fail; systemd's ExecStart gives an absolute one",
			"argv0", os.Args[0])
	}
	upgrader, err := tableflip.New(tableflip.Options{
		UpgradeTimeout: opts.UpgradeTimeout,
		PIDFile:        opts.PIDFile,
	})
	if err != nil {
		return nil, err
	}
	return &Lifecycle{log: log, upgrader: upgrader, opts: opts}, nil
}

// Draining reports whether the process has begun shutting down. Read by the drain middleware and by
// /healthz.
func (l *Lifecycle) Draining() bool { return l.draining.Load() }

// Listen takes a listening socket, inherited from the parent across an upgrade when there is one.
// The name is what pairs a child's socket with the parent's, so it must be stable across versions.
func (l *Lifecycle) Listen(addr string) (net.Listener, error) {
	return l.upgrader.Listen("tcp", addr)
}

// Serve runs a server on a listener and keeps it for the shutdown. Non-blocking.
func (l *Lifecycle) Serve(server *http.Server, listener net.Listener) {
	l.mu.Lock()
	l.servers = append(l.servers, server)
	l.mu.Unlock()

	go func() {
		if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			l.log.Error("listener stopped", "addr", server.Addr, "err", err)
		}
	}()
}

// Run blocks until the process is told to stop, then drains.
//
// Ready() does two things that matter: it releases the parent across an upgrade, and it writes the
// PID file. That file is how systemd follows the handover — it re-reads it when the main process
// exits and adopts whatever pid is in it, which is why the unit needs no sd_notify handshake and no
// NotifyAccess=all. Verified against systemd directly: three consecutive upgrades, MainPID tracking
// the file each time.
func (l *Lifecycle) Run() error {
	defer l.upgrader.Stop()

	if err := l.upgrader.Ready(); err != nil {
		return err
	}
	l.log.Info("serving", "upgrade_timeout", l.opts.UpgradeTimeout)

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGHUP, syscall.SIGUSR1, syscall.SIGTERM, syscall.SIGINT)

	for {
		select {
		case sig := <-signals:
			switch sig {
			case syscall.SIGHUP:
				l.upgrade()
			case syscall.SIGUSR1:
				if l.opts.OnReload != nil {
					l.opts.OnReload()
				}
			default:
				l.log.Info("shutdown requested", "signal", sig.String())
				// Lame-duck first: this process is going away and nothing is taking its place, so
				// whatever is in front of it needs a chance to notice.
				l.drain(l.windows().LameDuck)
				return nil
			}
		case <-l.upgrader.Exit():
			// A child took the sockets and is serving. Drain what is still running here and go.
			l.log.Info("handing over to the upgraded process")
			// No lame-duck on a handover: the child is already accepting on the same socket, so
			// answering 503 here would refuse requests a healthy process was ready to serve.
			l.drain(0)
			return nil
		}
	}
}

// upgrade forks a child on the current binary and hands it the listening sockets. A failure leaves
// this process serving exactly as it was, which is the property the whole mechanism is bought for:
// a bad binary does nothing.
func (l *Lifecycle) upgrade() {
	l.log.Info("upgrade requested", "exec", os.Args[0])
	if err := l.upgrader.Upgrade(); err != nil {
		l.log.Error("upgrade failed — still serving on the current binary", "err", err)
		return
	}
}

// drain flips the flag, waits for live handlers, then stops hard.
//
// The flag goes first and on its own: a health check in front of this box sees 503 and stops
// sending new work here before the socket closes, which is what makes an upgrade invisible on a
// fleet with more than one gateway.
func (l *Lifecycle) drain(lameDuck time.Duration) {
	l.draining.Store(true)
	if lameDuck > 0 {
		// Still accepting, now answering 503 with a Retry-After. Long enough for a health check to
		// poll once and deregister the box.
		l.log.Info("draining: answering 503 while anything in front notices", "lame_duck", lameDuck)
		time.Sleep(lameDuck)
	}

	ctx, cancel := context.WithTimeout(context.Background(), l.windows().Total)
	defer cancel()

	l.mu.Lock()
	servers := append([]*http.Server(nil), l.servers...)
	l.mu.Unlock()

	var wg sync.WaitGroup
	for _, server := range servers {
		wg.Add(1)
		go func(s *http.Server) {
			defer wg.Done()
			if err := s.Shutdown(ctx); err != nil {
				// The deadline passed with work still running. Close returns those handlers with an
				// error — their defers still run, so every in-flight slot is still released and
				// whatever usage was captured is still recorded.
				l.log.Warn("drain deadline passed, closing", "addr", s.Addr, "err", err)
				_ = s.Close()
			}
		}(server)
	}
	wg.Wait()
	l.log.Info("drained")
}
