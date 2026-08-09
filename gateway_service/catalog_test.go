package main

import "testing"

func ids(objs []modelObj) []string {
	out := make([]string, 0, len(objs))
	for _, o := range objs {
		out = append(out, o.ID)
	}
	return out
}

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
func TestCatalogObjects(t *testing.T) {
	deployed := []string{"qwen3-4b", "qwen3.5-27b", "secret-internal"}
	public := modelSet("qwen3-4b,qwen3.5-27b")

	got := ids(catalogObjects(public, deployed, 1700000000))
	if want := []string{"qwen3-4b", "qwen3.5-27b"}; !equal(got, want) {
		t.Fatalf("catalog = %v, want %v", got, want)
	}
}

func TestCatalogHidesModelsNoGroupAdvertises(t *testing.T) {
	got := ids(catalogObjects(modelSet("qwen3-4b"), []string{"qwen3-4b", "secret-internal"}, 0))
	for _, id := range got {
		if id == "secret-internal" {
			t.Fatal("a model no public group names reached the anonymous catalogue")
		}
	}
}

func TestCatalogSkipsAnAdvertisedModelThatIsNotDeployed(t *testing.T) {
	// Advertising one is worse than omitting it: the caller signs up, calls it, and gets a 503
	// from a route that was never there.
	got := ids(catalogObjects(modelSet("qwen3-4b,retired-model"), []string{"qwen3-4b"}, 0))
	if want := []string{"qwen3-4b"}; !equal(got, want) {
		t.Fatalf("catalog = %v, want %v", got, want)
	}
}

func TestNoPublicGroupMeansAnEmptyCatalogue(t *testing.T) {
	// The default for every group. Blank parses to nil, which answers false to everything.
	if got := catalogObjects(modelSet(""), []string{"qwen3-4b"}, 0); len(got) != 0 {
		t.Fatalf("catalog = %v, want empty", ids(got))
	}
}

func TestCatalogKeepsTheDeployedOrder(t *testing.T) {
	// listModels sorts; the catalogue must not reshuffle it, or the anonymous list and the
	// key-gated one disagree about ordering for no reason.
	got := ids(catalogObjects(modelSet("b,a,c"), []string{"a", "b", "c"}, 0))
	if want := []string{"a", "b", "c"}; !equal(got, want) {
		t.Fatalf("catalog = %v, want %v", got, want)
	}
}

// The catalogue is a display list. It must not be reachable as a grant — canUse is the only
// access decision, and it has never heard of it.
func TestTheCatalogueGrantsNothing(t *testing.T) {
	rec, usr := KeyRecord{Status: "active"}, UserRecord{} // no group models, no allow
	if canUse(usr, GroupRecord{}, "qwen3-4b") {
		t.Fatal("a model on the public catalogue must still be refused without a grant")
	}
	if status, _ := evaluate(rec, usr, GroupRecord{}, "qwen3-4b"); status != 403 {
		t.Fatalf("evaluate = %d, want 403", status)
	}
}
