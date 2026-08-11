package catalog

import (
	"testing"

	"grove-gateway/internal/domain"
)

func equal(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// The public catalogue is served to anyone, so what it does NOT say matters as much as what it
// does. Two rules: it never names a model no engine is serving, and it never widens access.
func TestIntersect(t *testing.T) {
	deployed := []string{"qwen3-4b", "qwen3.5-27b", "secret-internal"}

	got := Intersect(domain.ModelSet("qwen3-4b,qwen3.5-27b"), deployed)
	if want := []string{"qwen3-4b", "qwen3.5-27b"}; !equal(got, want) {
		t.Fatalf("catalog = %v, want %v", got, want)
	}
}

func TestCatalogHidesModelsNoGroupAdvertises(t *testing.T) {
	got := Intersect(domain.ModelSet("qwen3-4b"), []string{"qwen3-4b", "secret-internal"})
	for _, id := range got {
		if id == "secret-internal" {
			t.Fatal("a model no public group names reached the anonymous catalogue")
		}
	}
}

func TestCatalogSkipsAnAdvertisedModelThatIsNotDeployed(t *testing.T) {
	// Advertising one is worse than omitting it: the caller signs up, calls it, and gets a 503
	// from a route that was never there.
	got := Intersect(domain.ModelSet("qwen3-4b,retired-model"), []string{"qwen3-4b"})
	if want := []string{"qwen3-4b"}; !equal(got, want) {
		t.Fatalf("catalog = %v, want %v", got, want)
	}
}

func TestNoPublicGroupMeansAnEmptyCatalogue(t *testing.T) {
	// The default for every group. Blank parses to nil, which answers false to everything.
	if got := Intersect(domain.ModelSet(""), []string{"qwen3-4b"}); len(got) != 0 {
		t.Fatalf("catalog = %v, want empty", got)
	}
}

func TestCatalogKeepsTheDeployedOrder(t *testing.T) {
	// Routes.Models sorts; the catalogue must not reshuffle it, or the anonymous list and the
	// key-gated one disagree about ordering for no reason.
	got := Intersect(domain.ModelSet("b,a,c"), []string{"a", "b", "c"})
	if want := []string{"a", "b", "c"}; !equal(got, want) {
		t.Fatalf("catalog = %v, want %v", got, want)
	}
}

// The catalogue is a display list. It must not be reachable as a grant — CanUse is the only access
// decision, and it has never heard of it.
func TestTheCatalogueGrantsNothing(t *testing.T) {
	rec, usr := domain.KeyRecord{Status: "active"}, domain.UserRecord{} // no group models, no allow
	if domain.CanUse(usr, domain.GroupRecord{}, "qwen3-4b") {
		t.Fatal("a model on the public catalogue must still be refused without a grant")
	}
	if status, _ := domain.Evaluate(rec, usr, domain.GroupRecord{}, "qwen3-4b"); status != 403 {
		t.Fatalf("Evaluate = %d, want 403", status)
	}
}
