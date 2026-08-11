package http

import (
	"bufio"
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"grove-gateway/internal/config"
	"grove-gateway/internal/domain"
	"grove-gateway/internal/repository/memory"
)

// A realtime session is a WebSocket upgrade: a GET with no body, so the model cannot come from
// where every other request puts it. It comes from the query string, which is where the OpenAI
// realtime API puts it — and the connection then has to survive being handed to the engine.

// echoEngine accepts an upgrade, answers 101, and echoes one line back so the test can prove bytes
// move in both directions after the handover.
func echoEngine(t *testing.T) *httptest.Server {
	t.Helper()
	engine := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
			http.Error(w, "the upgrade headers did not survive the hop", http.StatusBadRequest)
			return
		}
		conn, buf, err := http.NewResponseController(w).Hijack()
		if err != nil {
			t.Errorf("engine could not hijack: %v", err)
			return
		}
		defer conn.Close()
		_, _ = buf.WriteString("HTTP/1.1 101 Switching Protocols\r\n" +
			"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
		_ = buf.Flush()

		line, err := buf.ReadString('\n')
		if err != nil {
			return
		}
		_, _ = buf.WriteString("echo:" + line)
		_ = buf.Flush()
	}))
	t.Cleanup(engine.Close)
	return engine
}

func realtimeStore(t *testing.T, engineURL, modality string) *memory.Store {
	t.Helper()
	store := memory.New()
	store.Keys[domain.SHA256Hex(secret)] = domain.KeyRecord{
		Status: "active", User: "ritwik", KeyPrefix: "abc123",
	}
	store.Users["ritwik"] = domain.UserRecord{Group: "acme"}
	store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("nemotron-asr")}
	store.Routes["nemotron-asr"] = []domain.Route{{
		EngineURL: engineURL, Healthy: true, Deployment: "pod-1",
		Kind: "direct", Modality: modality,
	}}
	return store
}

// upgrade dials the gateway directly and speaks raw HTTP, because an upgrade is exactly the thing
// an http.Client will not hand back.
func upgrade(t *testing.T, front *httptest.Server, target string) (*http.Response, *bufio.ReadWriter, net.Conn) {
	t.Helper()
	conn, err := net.Dial("tcp", strings.TrimPrefix(front.URL, "http://"))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
	request := "GET " + target + " HTTP/1.1\r\n" +
		"Host: gateway\r\n" +
		"Authorization: Bearer " + secret + "\r\n" +
		"Connection: Upgrade\r\nUpgrade: websocket\r\n" +
		"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n"
	if _, err := conn.Write([]byte(request)); err != nil {
		t.Fatalf("write request: %v", err)
	}
	buf := bufio.NewReadWriter(bufio.NewReader(conn), bufio.NewWriter(conn))
	resp, err := http.ReadResponse(buf.Reader, nil)
	if err != nil {
		conn.Close()
		t.Fatalf("read response: %v", err)
	}
	return resp, buf, conn
}

// The whole point: the model is read from the query, the request routes, the 101 comes back, and
// the connection carries bytes both ways afterwards.
func TestARealtimeUpgradeReachesTheEngineAndCarriesBytes(t *testing.T) {
	engine := echoEngine(t)
	store := realtimeStore(t, engine.URL, "audio")
	front := httptest.NewServer(buildHandler(t, store, config.Config{}, 0))
	t.Cleanup(front.Close)

	resp, buf, conn := upgrade(t, front, "/v1/realtime?model=nemotron-asr")
	defer conn.Close()

	if resp.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("status = %d, want 101 — the upgrade did not survive the gateway", resp.StatusCode)
	}
	if _, err := buf.WriteString("hello\n"); err != nil {
		t.Fatalf("write after upgrade: %v", err)
	}
	if err := buf.Flush(); err != nil {
		t.Fatalf("flush: %v", err)
	}
	line, err := buf.ReadString('\n')
	if err != nil {
		t.Fatalf("read echo: %v", err)
	}
	if strings.TrimSpace(line) != "echo:hello" {
		t.Errorf("echo = %q, want %q", strings.TrimSpace(line), "echo:hello")
	}

	// Metering is deferred, and for a hijacked connection the handler does not return until the
	// session ends — so usage lands at DISCONNECT, not at connect, and an open session is unbilled
	// for as long as it stays open. Nothing waits on a hijacked handler (httptest.Close does not
	// either), so poll through Drain, which takes the store's lock and so cannot race the write.
	conn.Close()
	var drained map[string]map[string]string
	for deadline := time.Now().Add(3 * time.Second); time.Now().Before(deadline); {
		snapshot, err := store.Repositories().Usage.Drain(context.Background())
		if err != nil {
			t.Fatalf("Drain: %v", err)
		}
		if len(snapshot) > 0 {
			drained = snapshot
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if got := drained["abc123"]["request_count"]; got != "1" {
		t.Errorf("request_count = %q, want 1 once the session closed", got)
	}
}

// No model in the query is the same refusal a body with no model gets. It must not reach an engine.
func TestARealtimeUpgradeWithNoModelIsRefused(t *testing.T) {
	engine := echoEngine(t)
	store := realtimeStore(t, engine.URL, "audio")
	front := httptest.NewServer(buildHandler(t, store, config.Config{}, 0))
	t.Cleanup(front.Close)

	resp, _, conn := upgrade(t, front, "/v1/realtime")
	defer conn.Close()

	if resp.StatusCode != http.StatusForbidden {
		t.Errorf("status = %d, want 403", resp.StatusCode)
	}
}

// The grant check still applies — an upgrade is not a way around it.
func TestARealtimeUpgradeStillNeedsTheGrant(t *testing.T) {
	engine := echoEngine(t)
	store := realtimeStore(t, engine.URL, "audio")
	store.Groups["acme"] = domain.GroupRecord{Models: domain.ModelSet("something-else")}
	front := httptest.NewServer(buildHandler(t, store, config.Config{}, 0))
	t.Cleanup(front.Close)

	resp, _, conn := upgrade(t, front, "/v1/realtime?model=nemotron-asr")
	defer conn.Close()

	if resp.StatusCode != http.StatusForbidden {
		t.Errorf("status = %d, want 403", resp.StatusCode)
	}
}
