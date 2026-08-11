// Package metering records what a finished request cost and how its hop behaved. Both run on every
// admitted request, including one the client abandoned.
package metering

import (
	"context"
	"log/slog"
	"strings"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository"
)

// Report is one finished request, as the proxy saw it.
type Report struct {
	Prefix string // API Key doc name — which bucket this accrues to
	Model  string // from the request body; buckets per-model usage
	// Deployment is which placement actually served it. On a direct route the gateway already
	// knows; on an ingress route only the ingress does, and it says so in a response header.
	Deployment string
	// Usage is the raw JSON captured from the response — the final streaming frame or the whole
	// non-streaming body. May be empty.
	Usage string
	// How the hop went, for passive ejection: the upstream's status, and the X-Grove-Reason an
	// ingress sets when it is healthy but has no replica for this model.
	Target         string
	UpstreamStatus string
	Reason         string
}

type Service struct {
	usage  repository.Usage
	health repository.Health
	log    *slog.Logger
}

func New(usage repository.Usage, health repository.Health, log *slog.Logger) *Service {
	return &Service{usage: usage, health: health, log: log}
}

// Record writes the usage delta and moves the target's failure count. Errors are logged, never
// returned to the caller: the request is over, and failing it retroactively helps nobody.
func (s *Service) Record(ctx context.Context, rep Report) {
	s.recordOutcome(ctx, rep)
	if rep.Prefix == "" {
		// No bucket to accrue to. The outcome above still counted — how a target behaves is not
		// contingent on the usage record that happened to ride along with the report.
		return
	}
	fields := UsageFields(rep)
	if len(fields) == 0 {
		return
	}
	if err := s.usage.Add(ctx, rep.Prefix, fields); err != nil {
		s.log.Error("usage not recorded", "prefix", rep.Prefix, "model", rep.Model, "err", err)
	}
}

// UsageFields is the whole accounting rule, pure so it is testable without a store. Each metric is
// written flat and, when known, as m:<metric>:<model> and m:<metric>:<deployment> in the SAME hash,
// so one drain carries aggregate and breakdown. Zero values are skipped.
func UsageFields(rep Report) map[string]int64 {
	model := strings.TrimSpace(rep.Model)
	deployment := strings.TrimSpace(rep.Deployment)
	fields := map[string]int64{}

	bump := func(metric string, n int64) {
		if n == 0 {
			return
		}
		fields[metric] += n
		if model != "" {
			fields["m:"+metric+":"+model] += n
		}
		// Beside the per-model bucket, in the same hash. This is the only path by which usage
		// reaches a placement the gateway never chose — on an ingress route the replica was picked
		// a tier below.
		if deployment != "" && deployment != model {
			fields["m:"+metric+":"+deployment] += n
		}
	}

	bump("request_count", 1)
	if u, ok := domain.ParseUsage([]byte(rep.Usage)); ok {
		bump("prompt_tokens", int64(u.Prompt))
		bump("completion_tokens", int64(u.Completion))
		bump("total_tokens", int64(u.Total))
		// Cached ⊆ Prompt, already inside Total. Tracked separately so the control plane can
		// bill/rate-limit on total_tokens - cached_tokens.
		bump("cached_tokens", int64(u.Cached))
	}
	return fields
}

// recordOutcome moves one target's consecutive-failure count. A success clears it outright.
func (s *Service) recordOutcome(ctx context.Context, rep Report) {
	if rep.Target == "" {
		return
	}
	var err error
	switch {
	case domain.IsHopFailure(rep.UpstreamStatus, rep.Reason):
		err = s.health.RecordFailure(ctx, rep.Target)
		s.log.Debug("hop failed", "target", rep.Target, "status", rep.UpstreamStatus, "reason", rep.Reason)
	case domain.IsHopSuccess(rep.UpstreamStatus):
		err = s.health.RecordSuccess(ctx, rep.Target)
	default:
		return
	}
	if err != nil {
		s.log.Warn("health counter not moved", "target", rep.Target, "err", err)
	}
}
