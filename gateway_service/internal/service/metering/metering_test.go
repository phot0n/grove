package metering

import (
	"context"
	"io"
	"log/slog"
	"testing"

	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository/memory"
)

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

const openAIUsage = `{"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,` +
	`"prompt_tokens_details":{"cached_tokens":80}}}`

// Every metric is written flat and twinned per model and per deployment in the same hash, so one
// drain carries the aggregate and the breakdown together.
func TestUsageFieldsTwinsEveryMetric(t *testing.T) {
	fields := UsageFields(Report{Model: "qwen3-4b", Deployment: "MD-00007", Usage: openAIUsage})

	for field, want := range map[string]int64{
		"request_count":                1,
		"prompt_tokens":                100,
		"completion_tokens":            20,
		"total_tokens":                 120,
		"cached_tokens":                80,
		"m:total_tokens:qwen3-4b":      120,
		"m:total_tokens:MD-00007":      120,
		"m:cached_tokens:qwen3-4b":     80,
		"m:request_count:qwen3-4b":     1,
		"m:completion_tokens:MD-00007": 20,
		"m:prompt_tokens:qwen3-4b":     100,
		"m:completion_tokens:qwen3-4b": 20,
		"m:cached_tokens:MD-00007":     80,
		"m:prompt_tokens:MD-00007":     100,
		"m:request_count:MD-00007":     1,
	} {
		if fields[field] != want {
			t.Errorf("%s = %d, want %d", field, fields[field], want)
		}
	}
}

// A field that never moved should not appear at all — this is the case the control plane's
// billable() clamp exists for, and writing an explicit zero would defeat it.
func TestZeroMetricsAreNotWritten(t *testing.T) {
	fields := UsageFields(Report{
		Model: "qwen3-4b",
		Usage: `{"usage":{"prompt_tokens":10,"completion_tokens":0,"total_tokens":10}}`,
	})
	if _, present := fields["completion_tokens"]; present {
		t.Error("a zero completion count was written")
	}
	if _, present := fields["cached_tokens"]; present {
		t.Error("a zero cached count was written — vLLM without --enable-prompt-tokens-details")
	}
}

// m:<metric>:<deployment> shares a namespace with m:<metric>:<model>. When a Model is named the
// same as its Model Deployment, writing both would bill that request twice.
func TestAModelNamedLikeItsDeploymentIsNotDoubleCounted(t *testing.T) {
	fields := UsageFields(Report{Model: "same", Deployment: "same", Usage: openAIUsage})
	if got := fields["m:total_tokens:same"]; got != 120 {
		t.Errorf("m:total_tokens:same = %d, want 120", got)
	}
}

// A request that produced no usage frame still counts as a request. Dropping it would make the
// request count disagree with the access log for every streaming client that hung up early.
func TestARequestWithNoUsageStillCounts(t *testing.T) {
	fields := UsageFields(Report{Model: "qwen3-4b", Usage: ""})
	if fields["request_count"] != 1 {
		t.Errorf("request_count = %d, want 1", fields["request_count"])
	}
	if _, present := fields["total_tokens"]; present {
		t.Error("tokens were invented for a request that reported none")
	}
}

// The usage frame arrives SSE-wrapped on a streaming response and bare on a non-streaming one.
// Both have to parse, or every streaming request bills as zero.
func TestAStreamingFrameParses(t *testing.T) {
	streamed := UsageFields(Report{Model: "qwen3-4b", Usage: "data: " + openAIUsage})
	if streamed["total_tokens"] != 120 {
		t.Errorf("total_tokens = %d from an SSE frame, want 120", streamed["total_tokens"])
	}
}

// A report with no prefix has no bucket to accrue to, but how its target behaved is not contingent
// on that — a broken engine must still be counted as broken.
func TestOutcomeIsRecordedWithoutAPrefix(t *testing.T) {
	store := memory.New()
	New(store.Repositories().Usage, store.Repositories().Health, quiet()).Record(
		context.Background(),
		Report{Target: "https://a", UpstreamStatus: "502"},
	)
	if store.Failures["https://a"] != 1 {
		t.Errorf("failures = %d, want 1", store.Failures["https://a"])
	}
	if len(store.Usage) != 0 {
		t.Errorf("usage accrued to %v with no prefix", store.Usage)
	}
}

// A success clears the count outright, so a target has to fail EjectAfter times in a ROW.
func TestOneSuccessClearsTheFailureStreak(t *testing.T) {
	store := memory.New()
	store.Failures["https://a"] = domain.EjectAfter - 1
	svc := New(store.Repositories().Usage, store.Repositories().Health, quiet())

	svc.Record(context.Background(), Report{Prefix: "abc", Target: "https://a", UpstreamStatus: "200"})
	if store.Failures["https://a"] != 0 {
		t.Errorf("failures = %d after a success, want 0", store.Failures["https://a"])
	}
}

// One unplaced model must not take an ingress out of rotation for every other model on it.
func TestANoReplica503DoesNotCountAgainstTheTarget(t *testing.T) {
	store := memory.New()
	New(store.Repositories().Usage, store.Repositories().Health, quiet()).Record(
		context.Background(),
		Report{Target: "https://ingress", UpstreamStatus: "503", Reason: "no-replica"},
	)
	if store.Failures["https://ingress"] != 0 {
		t.Errorf("failures = %d, want 0 — the ingress answered correctly", store.Failures["https://ingress"])
	}
}
