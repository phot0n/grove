// Command grove-gateway is the Grove data plane. One binary, two planes: a gateway holds tenant
// state and picks a route, an ingress holds none and picks a replica inside one VPC — decided by
// which id it is given. Signals: SIGHUP upgrades, SIGUSR1 re-reads tunables, SIGTERM drains.
package main

import (
	"context"
	"crypto/tls"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"time"

	"grove-gateway/internal/config"
	"grove-gateway/internal/observability"
	redisstore "grove-gateway/internal/repository/redis"
	"grove-gateway/internal/service/admission"
	"grove-gateway/internal/service/catalog"
	"grove-gateway/internal/service/metering"
	"grove-gateway/internal/service/provisioning"
	"grove-gateway/internal/service/routing"
	"grove-gateway/internal/service/transform"
	gatewayhttp "grove-gateway/internal/transport/http"
	"grove-gateway/internal/transport/http/proxy"
)

func main() {
	if err := run(); err != nil {
		slog.Error("grove-gateway stopped", "err", err)
		os.Exit(1)
	}
}

func run() error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}

	// A LevelVar, so a reload can move the level under a running process: slog reads it on every
	// record, which is the whole reason the type exists.
	level := new(slog.LevelVar)
	log, closeLogs, err := observability.New(observability.Options{
		Level:         level,
		AccessLogPath: cfg.AccessLogPath,
		ErrorLogPath:  cfg.ErrorLogPath,
	})
	if err != nil {
		return err
	}
	defer closeLogs()
	slog.SetDefault(log.Process)

	live, err := config.Open(cfg.ConfigPath, config.Defaults(), log.Process)
	if err != nil {
		return err
	}

	client := redisstore.New(cfg.RedisAddr)
	if err := client.Ping(context.Background()); err != nil {
		return err
	}
	defer client.Close()
	store := client.Store()

	// built is the state a reload has to REBUILD. Everything else — the body cap, the synthetic
	// session, the drain windows — is a function call at the point of use and needs nothing here.
	rebuilt := &built{log: log.Process, live: live, level: level, proxy: proxy.New(proxy.Options{}, log.Process)}
	if err := rebuilt.transforms(live.Get().Transforms); err != nil {
		return err
	}

	lifecycle, err := gatewayhttp.NewLifecycle(gatewayhttp.LifecycleOptions{
		// Read when a drain BEGINS, not when the process started — so an operator about to restart
		// a busy gateway can lengthen the drain and have it apply to that very restart.
		Windows: func() gatewayhttp.DrainWindows {
			current := live.Get()
			return gatewayhttp.DrainWindows{Total: current.DrainTimeout, LameDuck: current.LameDuck}
		},
		UpgradeTimeout: live.Get().UpgradeTimeout,
		PIDFile:        cfg.PIDFile,
		OnReload:       rebuilt.reload,
	}, log.Process)
	if err != nil {
		return err
	}

	server := gatewayhttp.New(cfg, gatewayhttp.Services{
		Admission: admission.New(store.Keys, store.Users, store.Groups),
		Routing: routing.New(store, log.Process, routing.Options{
			GatewayID:    cfg.GatewayID,
			Region:       cfg.Region,
			SyntheticTTL: func() time.Duration { return live.Get().SyntheticSessionTTL },
		}),
		Metering:     metering.New(store.Usage, store.Health, log.Process),
		Catalog:      catalog.New(store.Routes, store.Catalog),
		Provisioning: provisioning.New(store, log.Process),
		Transform:    rebuilt.chain,
		Proxy:        rebuilt.proxy,
		Drain:        lifecycle,
		Access:       log.Access,
		MaxBodyBytes: func() int64 { return live.Get().MaxBodyBytes },
	}, log.Process)
	rebuilt.server = server
	rebuilt.applyScalars(live.Get())

	log.Process.Info("starting",
		"plane", plane(cfg),
		"redis", cfg.RedisAddr,
		"config", live.Path(),
		"log_level", live.Get().LogLevel,
		"transforms", rebuilt.chain.Names(),
	)

	if !cfg.ServesData() {
		return errors.New("neither GROVE_LISTEN_HTTP nor GROVE_LISTEN_HTTPS is set — this process " +
			"owns the data path now and has nothing to serve on")
	}
	if err := serveData(lifecycle, server, cfg, live.Get().Middleware); err != nil {
		return err
	}

	return lifecycle.Run()
}

// built holds what a reload cannot express as a plain read: the log level, the transform chain, the
// upstream transports and the middleware chain.
type built struct {
	log   *slog.Logger
	live  *config.Live
	level *slog.LevelVar

	proxy  *proxy.Proxy
	chain  *transform.Chain
	server *gatewayhttp.Server
}

// reload is SIGUSR1. Each part is applied independently and fails loudly on its own: a bad
// middleware list must not also stop the log level from moving, and none of them may take the
// gateway down.
func (b *built) reload() {
	previous, next, changed, err := b.live.Reload()
	if err != nil {
		b.log.Error("tunables rejected, keeping the running configuration",
			"path", b.live.Path(), "err", err)
		return
	}
	if len(changed) == 0 {
		b.log.Info("tunables reloaded, nothing changed", "path", b.live.Path())
		return
	}

	b.applyScalars(next)
	if !config.SameList(previous.Transforms, next.Transforms) {
		if err := b.transforms(next.Transforms); err != nil {
			b.log.Error("transform list rejected, keeping the running one", "err", err)
		}
	}
	if !config.SameList(previous.Middleware, next.Middleware) {
		if err := b.server.SetChain(next.Middleware); err != nil {
			b.log.Error("middleware list rejected, keeping the running chain", "err", err)
		}
	}
	b.log.Info("tunables reloaded", "path", b.live.Path(), "changed", changed)
}

// applyScalars moves the settings that are held inside something already built.
func (b *built) applyScalars(next config.Resolved) {
	b.level.Set(next.LogLevel)
	b.proxy.Reconfigure(proxy.Options{
		ReadTimeout:    next.UpstreamReadTimeout,
		VerifyUpstream: next.UpstreamTLSVerify,
	})
}

// transforms rebuilds the request-transform chain IN PLACE, so the middleware holding a pointer to
// it does not have to be rebuilt as well.
func (b *built) transforms(names []string) error {
	if b.chain == nil {
		chain, err := transform.NewChain(names)
		if err != nil {
			return err
		}
		b.chain = chain
		return nil
	}
	return b.chain.Replace(names)
}

// serveData brings up the customer-facing listeners: :443 with the fleet certificate, and :80 as
// either a redirect or, on a fleet with no zone, the whole surface in the clear.
func serveData(lifecycle *gatewayhttp.Lifecycle, server *gatewayhttp.Server, cfg config.Config, chain []string) error {
	// Handlers, not DataHandler: the TLS listener carries /healthz, /grove-admin and the scrape as
	// well as /v1, each under the name it belongs to.
	handler, err := server.Handlers(cfg, chain)
	if err != nil {
		return err
	}
	if !cfg.ServesTLS() {
		// No certificate configured: everything answers in the clear, which is what a box with no
		// Fleet Zone always served.
		return listen(lifecycle, cfg.ListenHTTP, handler, nil)
	}

	tlsConfig, err := server.TLSConfig(cfg)
	if err != nil {
		return err
	}
	if err := listen(lifecycle, cfg.ListenHTTPS, handler, tlsConfig); err != nil {
		return err
	}
	if cfg.ListenHTTP == "" {
		return nil
	}
	return listen(lifecycle, cfg.ListenHTTP, server.RedirectHandler(), nil)
}

// listen binds through the lifecycle, so the socket is inherited across an upgrade rather than
// rebound — which is what keeps a connection from being refused during one.
func listen(lifecycle *gatewayhttp.Lifecycle, addr string, handler http.Handler, tlsConfig *tls.Config) error {
	if addr == "" {
		return nil
	}
	listener, err := lifecycle.Listen(addr)
	if err != nil {
		return err
	}
	if tlsConfig != nil {
		listener = tls.NewListener(listener, tlsConfig)
	}
	lifecycle.Serve(gatewayhttp.NewHTTPServer(addr, handler, tlsConfig), listener)
	slog.Info("listening", "addr", addr, "tls", tlsConfig != nil)
	return nil
}

func plane(cfg config.Config) string {
	if cfg.IsIngress() {
		return "ingress " + cfg.IngressID
	}
	return "gateway " + cfg.GatewayID
}
