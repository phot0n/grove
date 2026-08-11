package main

import (
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"strings"
	"testing"
)

// The layering is only worth having if it is enforced. Every rule below is one an import statement
// can break in a single line, with nothing failing and nothing looking wrong — the service would go
// on working while the property that made it testable quietly went away.
//
// Lives beside main because this is the one package that legitimately knows every ring exists.

const internalDir = "../../internal"

// imports walks a package tree and answers every import path in it, keyed by the file that has it.
func imports(t *testing.T, root string, skip func(path string) bool) map[string][]string {
	t.Helper()
	found := map[string][]string{}
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() || !strings.HasSuffix(path, ".go") {
			return err
		}
		if skip != nil && skip(path) {
			return nil
		}
		file, err := parser.ParseFile(token.NewFileSet(), path, nil, parser.ImportsOnly)
		if err != nil {
			return err
		}
		for _, spec := range file.Imports {
			found[path] = append(found[path], strings.Trim(spec.Path.Value, `"`))
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walking %s: %v", root, err)
	}
	return found
}

// Swapping net/http for another router is meant to be a rewrite of transport/http and nothing else.
// The moment a service imports it, that stops being true and nobody finds out until they try.
func TestOnlyTheTransportLayerKnowsAboutHTTP(t *testing.T) {
	for file, paths := range imports(t, internalDir, nil) {
		if strings.Contains(filepath.ToSlash(file), "/transport/") {
			continue
		}
		for _, path := range paths {
			if path == "net/http" || strings.HasPrefix(path, "net/http/") {
				t.Errorf("%s imports %s — only internal/transport/http may", file, path)
			}
		}
	}
}

// Swapping the store is meant to be a new folder beside repository/redis. A go-redis import
// anywhere else is a Redis assumption leaking into logic that claims not to have one.
func TestOnlyTheRedisRepositoryKnowsAboutRedis(t *testing.T) {
	for file, paths := range imports(t, internalDir, nil) {
		if strings.Contains(filepath.ToSlash(file), "/repository/redis/") {
			continue
		}
		for _, path := range paths {
			if strings.Contains(path, "go-redis") {
				t.Errorf("%s imports %s — only internal/repository/redis may", file, path)
			}
		}
	}
}

// domain holds every hard decision this service makes, and holds them as pure functions. That is
// what lets a different process — an ingress today, a remote router tomorrow — run the identical
// rule. It only stays true while nothing in here can reach the outside world.
func TestDomainIsPure(t *testing.T) {
	banned := []string{"net", "net/http", "os", "database/sql", "log/slog"}
	for file, paths := range imports(t, filepath.Join(internalDir, "domain"), nil) {
		for _, path := range paths {
			if strings.HasPrefix(path, "grove-gateway/") {
				t.Errorf("%s imports %s — domain depends on nothing in this module", file, path)
			}
			for _, bad := range banned {
				if path == bad {
					t.Errorf("%s imports %s — domain does no I/O", file, path)
				}
			}
		}
	}
}

// The services orchestrate; they must not reach past the interfaces to a concrete store. This is
// the inversion the whole arrangement rests on, and the one an autocomplete import undoes silently.
//
// Test files are exempt, and only here: reaching for repository/memory is exactly what the
// inversion bought, so a service test naming an implementation is the rule working rather than
// breaking. Every other rule below applies to tests too — a test that needed a live Redis, or a
// domain test that reached the network, would be the same mistake as the production code doing it.
func TestServicesDependOnInterfacesNotImplementations(t *testing.T) {
	skipTests := func(path string) bool { return strings.HasSuffix(path, "_test.go") }
	for file, paths := range imports(t, filepath.Join(internalDir, "service"), skipTests) {
		for _, path := range paths {
			if strings.HasPrefix(path, "grove-gateway/internal/repository/") {
				t.Errorf("%s imports %s — services take repository interfaces, not implementations", file, path)
			}
			if strings.HasPrefix(path, "grove-gateway/internal/transport/") {
				t.Errorf("%s imports %s — a service must not know how it is being called", file, path)
			}
		}
	}
}

// The point of the ring diagram: imports go inward. A domain that imported a service, or a
// repository that imported one, would be a cycle the compiler catches — but a repository importing
// a service's types is not a cycle and would still be wrong.
func TestRepositoriesDependOnlyOnDomain(t *testing.T) {
	for file, paths := range imports(t, filepath.Join(internalDir, "repository"), nil) {
		for _, path := range paths {
			if !strings.HasPrefix(path, "grove-gateway/") {
				continue
			}
			switch {
			case path == "grove-gateway/internal/domain",
				path == "grove-gateway/internal/repository":
			default:
				t.Errorf("%s imports %s — a repository knows domain types and nothing else", file, path)
			}
		}
	}
}
