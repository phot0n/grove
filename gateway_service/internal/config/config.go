// Package config splits what this process was told into two halves by LIFETIME.
//
//	Config  — environment. Identity, secrets, sockets, paths. Changing one makes this a different
//	          process, so it takes a restart and says so.
//	Dynamic — a JSON file, re-read when it changes. Tunables an operator turns while the fleet runs.
//
// Nothing appears in both. A value that lives in the environment is not overridable by the file and
// the other way round, so there is never a question of which one won.
package config

import (
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"
)

// DefaultPath is where the tunables live. Beside agent.env, which holds the other half.
const DefaultPath = "/etc/grove-gateway/config.json"

// Config is the bootstrap half: what this box IS, and what it is allowed to talk to.
type Config struct {
	ConfigPath string // the tunables file this process watches

	RedisAddr string

	AdminToken   string
	IngressToken string
	GatewayID    string // set → tenant plane
	IngressID    string // set → infra plane; both set is a refusal
	Region       string

	// The data path. Blank ListenHTTPS keeps the new listeners down entirely, which is what lets
	// this binary ship to a box still fronted by OpenResty.
	ListenHTTP  string
	ListenHTTPS string
	// PublicHost is the shared name customer traffic arrives on; SelfHost is this box's own name,
	// which the control plane pushes to and the scrape asks for. Blank means "answer on any name",
	// which is what a fleet with no zone always did.
	PublicHost  string
	SelfHost    string
	TLSCertPath string
	TLSKeyPath  string

	HtpasswdPath    string
	NodeExporterURL string

	AccessLogPath string
	PIDFile       string
}

// ServesData reports whether this process owns the customer-facing ports. False while OpenResty
// still does, which is the shape every box is in before its cutover.
func (c Config) ServesData() bool { return c.ListenHTTPS != "" || c.ListenHTTP != "" }

// ServesTLS reports whether the HTTPS listener should come up.
func (c Config) ServesTLS() bool {
	return c.ListenHTTPS != "" && c.TLSCertPath != "" && c.TLSKeyPath != ""
}

func (c Config) IsIngress() bool { return c.IngressID != "" }

// Load reads the environment and refuses to return a Config that would run wrong.
func Load() (Config, error) {
	cfg := Config{
		ConfigPath: env("GROVE_CONFIG", DefaultPath),
		RedisAddr:  env("GROVE_REDIS_ADDR", "127.0.0.1:6379"),

		IngressToken: strings.TrimSpace(os.Getenv("GROVE_INGRESS_TOKEN")),
		Region:       strings.TrimSpace(os.Getenv("GROVE_GATEWAY_REGION")),

		ListenHTTP:  strings.TrimSpace(os.Getenv("GROVE_LISTEN_HTTP")),
		ListenHTTPS: strings.TrimSpace(os.Getenv("GROVE_LISTEN_HTTPS")),
		PublicHost:  strings.TrimSpace(os.Getenv("GROVE_PUBLIC_HOST")),
		SelfHost:    strings.TrimSpace(os.Getenv("GROVE_SELF_HOST")),
		TLSCertPath: strings.TrimSpace(os.Getenv("GROVE_TLS_CERT")),
		TLSKeyPath:  strings.TrimSpace(os.Getenv("GROVE_TLS_KEY")),

		HtpasswdPath:    env("GROVE_HTPASSWD", "/etc/grove/nginx/metrics.htpasswd"),
		NodeExporterURL: env("GROVE_NODE_EXPORTER_URL", "http://127.0.0.1:9100/metrics"),

		AccessLogPath: strings.TrimSpace(os.Getenv("GROVE_ACCESS_LOG")),
		PIDFile:       strings.TrimSpace(os.Getenv("GROVE_PID_FILE")),
	}

	// Before Redis: a missing token is a config fault, and needing a reachable Redis to hear
	// about it would report the wrong one.
	token, err := RequireAdminToken(os.Getenv("GROVE_ADMIN_TOKEN"))
	if err != nil {
		return Config{}, err
	}
	cfg.AdminToken = token

	ingressID, err := ResolveMode(os.Getenv("GROVE_GATEWAY_ID"), os.Getenv("GROVE_INGRESS_ID"))
	if err != nil {
		return Config{}, err
	}
	cfg.IngressID = ingressID
	cfg.GatewayID = GatewayID()

	if cfg.IsIngress() && cfg.IngressToken == "" {
		return Config{}, errors.New("GROVE_INGRESS_TOKEN is empty — refusing to start an ingress " +
			"that would refuse every gateway, which reads as a routing outage rather than a config fault")
	}
	return cfg, nil
}

// ResolveMode reads the two id variables and returns the ingress id, or "" for a gateway.
//
// One binary, two planes, and which one this box is is decided entirely by what it was given. Both
// ids set is refused rather than resolved by precedence: it would be a box serving customer keys
// from inside a VPC, and the whole point of the split is that no box does both. Neither set is a
// gateway, which is what every box was before ingresses existed.
func ResolveMode(gatewayID, ingressID string) (string, error) {
	gatewayID, ingressID = strings.TrimSpace(gatewayID), strings.TrimSpace(ingressID)
	if gatewayID != "" && ingressID != "" {
		return "", errors.New(
			"GROVE_GATEWAY_ID and GROVE_INGRESS_ID are both set — a box is one plane or the other, " +
				"and one serving tenant keys from inside a VPC is what this split exists to prevent")
	}
	return ingressID, nil
}

// RequireAdminToken reads GROVE_ADMIN_TOKEN, which the agent refuses to start without. /admin is
// the only path the control plane has to this box, and treating a blank token as "admin off" left
// a box serving the keys and routes it last had, with nothing but one startup line to say why the
// pushes stopped landing. Whitespace is blank: an env file written from an empty template renders
// the value as spaces, and that is the same missing token.
func RequireAdminToken(raw string) (string, error) {
	token := strings.TrimSpace(raw)
	if token == "" {
		return "", errors.New("GROVE_ADMIN_TOKEN is empty — refusing to start with an unguarded admin plane")
	}
	return token, nil
}

// GatewayID names this gateway for request-ids: GROVE_GATEWAY_ID (set at deploy to the Gateway
// Server name) else the host's short name, else "gw".
func GatewayID() string {
	if v := strings.TrimSpace(os.Getenv("GROVE_GATEWAY_ID")); v != "" {
		return v
	}
	if h, err := os.Hostname(); err == nil && h != "" {
		return strings.SplitN(h, ".", 2)[0]
	}
	return "gw"
}

// parseDurationStrict rejects what it cannot read rather than falling back.
//
// The opposite of what the environment variables used to do, and deliberately. A tunable is edited
// by hand and read back by nobody, so a value that silently became its default is a knob the
// operator believes they turned — exactly the old GROVE_SYNTHETIC_SESSION_TTL, where a typo meant
// "balance everything" while the person who typed it thought it meant "pin everything". Here the
// file is refused whole and the running value is kept.
func parseDurationStrict(raw string) (time.Duration, error) {
	value, err := time.ParseDuration(strings.TrimSpace(raw))
	if err != nil {
		return 0, fmt.Errorf("%q is not a duration (try \"30m\", \"600s\", \"0s\")", raw)
	}
	if value < 0 {
		return 0, fmt.Errorf("%q is negative", raw)
	}
	return value, nil
}

func parseLevelStrict(raw string) (slog.Level, error) {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "debug":
		return slog.LevelDebug, nil
	case "info":
		return slog.LevelInfo, nil
	case "warn":
		return slog.LevelWarn, nil
	case "error":
		return slog.LevelError, nil
	default:
		return 0, fmt.Errorf("%q is not a log level (debug, info, warn, error)", raw)
	}
}

func env(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}
