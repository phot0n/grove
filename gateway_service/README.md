# grove-gateway

The Grove data plane. One Go binary that terminates TLS, authenticates callers, picks an engine,
proxies the request, meters what it cost, and upgrades itself without dropping a connection.

**One binary, two planes**, decided entirely by which id it is given:

| | Gateway Server | Ingress Server |
|---|---|---|
| env | `GROVE_GATEWAY_ID` | `GROVE_INGRESS_ID` |
| holds | keys, users, groups, usage | nothing tenant-shaped |
| picks | a route (network or engine) | a replica in its own VPC |
| meters | yes | no — usage belongs to a tenant it cannot see |

Both ids set is a startup refusal. The tenant stages are not *disabled* on an ingress, they are
never registered, so no handler on that box could read a key store even if one were pushed to it.

## What it does

1. Terminates TLS on `:443` with the fleet wildcard, reloading it from disk when it changes, and
   redirects `:80`.
2. Resolves the caller — bearer → key → user → group — and refuses on the credential, the monthly
   budget, or the model grant.
3. Picks an engine: region tier, capacity gate, session stickiness, least in flight.
4. Rewrites the request body where the endpoint's schema allows it, and swaps the client's key for
   the engine's own.
5. Proxies it, streaming the response through untouched, and reads the usage frame out on the way.
6. Records tokens and the hop's outcome, on every request including the ones that were abandoned.
7. Answers `/v1/models` itself, and `/metrics/node` behind basic auth.
8. Upgrades its own binary and reloads its own configuration without dropping a connection.

Requirements: Redis on loopback, a certificate on disk, and an admin token. It refuses to start
without the last one, and refuses to start as an ingress without a data token.

**Reading order** if you are new to it: *Where it sits* → *Layers* → *The request path*. If you are
about to change something, skip to *How to do things* and *Things worth knowing*.

---

## Where it sits

```
                    ┌──────────────────────────────────────────────┐
                    │  Grove (Frappe control plane)                │
                    │  keys · users · groups · models · placements │
                    └───────┬──────────────────────────▲───────────┘
      PUT /grove-admin/*    │                          │  GET /grove-admin/usage
      every 2 min (dirty)   │                          │  every 5 min (drain)
                            ▼                          │
   client ──► latency DNS ──► ┌───────────────────────────────────┐
              api.<zone>      │  GATEWAY SERVER   (tenant plane)  │
                              │  TLS · auth · quota · route · meter│
                              │  local Redis                       │
                              └────┬──────────────────────┬────────┘
                        direct     │                      │  ingress
                                   ▼                      ▼
                    ┌──────────────────────┐   ┌────────────────────────────┐
                    │  engine box          │   │  INGRESS SERVER (infra)    │
                    │  nginx :80           │   │  picks a replica in its VPC│
                    │  └─ vLLM             │   │  holds NO tenant state     │
                    └──────────────────────┘   └────────┬───────────────────┘
                                                        ▼  private IP
                                               ┌──────────────────────┐
                                               │  engine box · vLLM   │
                                               └──────────────────────┘
```

**The control plane pushes; the gateway never calls back.** Grove projects its state into each
box's local Redis over `/grove-admin/*` and pulls usage counters back out. Between syncs the
gateway is autonomous — if Grove is down, traffic keeps flowing on the last table it was given.

**Two route kinds.** A `direct` row names an engine the gateway dials itself. An `ingress` row names
an Ingress Server that will pick a replica of its own, so replica topology never leaves its VPC and
a pod restarting in one region is invisible to a gateway in another. Which kind a model gets is the
control plane's decision; the gateway just reads `kind`. An empty `kind` is `direct` — that is what
every route pushed before the split carried.

**Everything on this box is local.** Redis is on loopback, and its contents are either pushed
(keys, users, groups, routes) or derived (sticky, in-flight, health, usage). Nothing is shared
between gateways, which is why a restart costs at most a round of cold prefix caches.

---

## Design

### The shape, and why

The whole service is four rings, and **imports only ever point inward**:

```
        ┌──────────────────────────────────────────────────────────┐
        │  transport/http        net/http · httputil · crypto/tls   │
        │  ┌────────────────────────────────────────────────────┐   │
        │  │  service          admission · routing · metering    │   │
        │  │                   catalog · transform · provisioning│   │
        │  │  ┌──────────────────────────────────────────────┐   │   │
        │  │  │  domain      PickRoute · Evaluate · ParseUsage│   │   │
        │  │  │              stdlib only. No I/O. No net/http.│   │   │
        │  │  └──────────────────────────────────────────────┘   │   │
        │  └───────────────────▲────────────────────────────────┘   │
        └──────────────────────┼─────────────────────────────────────┘
                               │ interfaces declared here
                    ┌──────────┴──────────┐
                    │  repository         │  ← redis/ and memory/ implement it
                    └─────────────────────┘

        cmd/grove-gateway   the only place that knows all four exist
```

`service` depends on the repository **interfaces**, never on an implementation — so the arrow from
`redis/` points *up* into `repository`, not sideways into `service`. That inversion is the only
reason the services are testable.

Five invariants carry the whole thing, and **`cmd/grove-gateway/architecture_test.go` enforces
them** — each is one import statement away from being broken, with nothing failing and nothing
looking wrong:

| Invariant | Protects |
|---|---|
| only `transport/http/**` imports `net/http` | swapping the router stays a one-package rewrite |
| only `repository/redis` imports `go-redis` | swapping the store stays a one-folder change |
| `domain` imports nothing from this module, and does no I/O | the rules stay runnable in another process |
| `service` never imports a repository *implementation* | the services stay testable — tests exempt, that is what `memory` is for |
| `repository` knows domain types and nothing else | imports point inward, including where the compiler would not catch it |

Stated as tests rather than as habits, because the thing they prevent is silent: the service goes on
working while the property that made it testable quietly goes away.

### What each ring is for

**`domain` — the rules, with the I/O taken out.** `PickRoute` gets a slice of routes and returns
one; it does not know where routes come from. `Evaluate` gets three records and returns a status.
`ParseUsage` gets bytes. Every hard decision in this service is one of these, and every one is a
pure function — which is why they carry the most test cases and the fewest mocks. It is also what
lets a *different process* run the identical rule: an ingress does, and a remote router could.

**`repository` — what storage must answer, not how.** The interfaces speak domain types and plain
values; no Redis vocabulary crosses the line. Deliberately coarse: `Usage.Add(prefix, fields)`
takes a map the service built, rather than the service knowing about `HINCRBY`. `InFlight.Counts`
returns numbers, not routes — the repository does not know what a Route is.

**`service` — the orchestration between them.** Fetch, apply a domain rule, act on the result. Each
service is a plain struct holding the repositories it needs, constructed once at startup. They
return `domain.Denial` — one refusal vocabulary — so no service invents its own error taxonomy for
a handler to translate.

**`transport/http` — everything HTTP, and nothing else.** Routing, decoding, status codes, TLS,
the reverse proxy, the signal loop. It is the thickest ring because that is where the framework
lives, and thick is fine as long as no decision hides in it.

### Design decisions, and what was rejected

| Decision | Rejected | Why |
|---|---|---|
| Repository interfaces in one `repository` package | one interface per consumer, Go-style | Nine repositories with one implementation each. Consumer-side interfaces would have meant nine near-duplicate declarations and no reader able to see the storage contract in one place |
| Services are concrete structs | an interface per service | An interface with one implementation is a lie about the design. The extension point is the middleware chain — see below |
| One `middleware.State` per request | a context key per field | The stages are ordered and each reads what the ones above wrote. That *is* a per-request record; N context keys would be the same object with the type safety spread thin |
| The chain is a list of names | a hardcoded handler tree | Order is the semantics here. `meter` below `route` is a correctness requirement, not a style choice, and a list makes it reviewable in one line |
| A separate transform registry | folding body rewrites into middleware | Different lifecycle: a transform is gated per endpoint and answers "did I change anything", so the body is re-encoded only when something touched it |
| `domain.Denial` as the one error type | per-package error types | Every gate speaks 401/403/429/503 already. One `errors.As` at the edge beats a translation table |
| The proxy is its own package | inline in the handler | It owns a transport pool and a response tee — real state with a lifecycle, and the only part that would change wholesale for HTTP/3 |
| Config split by **lifetime** | one config file, or all env | Identity needs a restart; tunables must not. Splitting on that axis makes "does this need a restart?" answerable by looking at where the value lives |

### The three seams

Extending this service means picking one of three, and the choice is not a matter of taste:

| Add a… | When | Costs |
|---|---|---|
| **middleware** | it needs the HTTP request or response — headers, body, timing, or the ability to refuse | one file + one `Register` + a name in the chain |
| **transform** | it rewrites the request body for particular endpoints | one file + one `Register` + a name in the list |
| **repository impl** | it changes *where* data lives, not what happens | one folder + one line in `main.go` |

The test: does it need to be on the request path, or is it a destination for data? Metering already
receives everything a usage row contains, so archiving usage to SQLite is a **repository**, not a
middleware — a middleware there would re-derive the same record from `State` and leave two places
that know how to read it.

### What is deliberately not here

No plugin system, no dependency-injection container, no code generation, no `interface{}` registry
of everything. Extension is a compiled-in file plus a name in a list, which is enough for a service
whose extensions ship in the same binary.

No abstraction over HTTP itself. `transport/http` is allowed to be net/http-shaped; hiding that
behind a "framework-agnostic" layer would buy portability nobody needs at the cost of every reader
having to learn a second vocabulary.

No metrics-per-anything. The access log carries what a request did; the process log carries why.
Warnings and worse are mirrored into `error.log` as well, pinned at Warn so moving the process log
level while hunting a failure never changes what that file holds.
A Prometheus surface is a middleware whenever someone wants one.

### State, and who owns it

The rule everywhere: **one owner per piece of state that can drift.**

| State | Lives | Lifetime | Owner |
|---|---|---|---|
| `middleware.State` | request context | one request | the chain |
| identity, decision, usage line | inside that State | one request | whichever stage wrote it |
| resolved tunables | `config.Live` | until SIGUSR1 | `built.reload` |
| the middleware chain | `atomic.Pointer` in `Server` | until SIGUSR1 | `Server.SetChain` |
| transport pool | `proxy.Proxy` | until a tunable changes | `Proxy.Reconfigure` |
| certificate | `certLoader` | until the file's mtime moves | the loader |
| sticky, in-flight, health, usage | local Redis | minutes to a pull cycle | this box |
| keys, users, groups, routes | local Redis | until the next push | **the control plane** |

The last row is the important one. Everything pushed is a *projection*: the gateway never edits it,
never merges into it, and never treats a local change as authoritative. Anything it does own is
either derivable (in-flight, health) or drained (usage).

### Concurrency

One goroutine per request, as net/http gives it. What is shared between them:

- **`config.Live`** — `atomic.Pointer`, swapped whole. A request that started under the old values
  finishes under them, which is correct for every knob here.
- **The middleware chain** — one `atomic.Pointer[http.Handler]`, so a reload never rebuilds the mux
  or interrupts a request mid-chain.
- **`transform.Chain`** — `RWMutex`, read per request and replaced in place, so the middleware's
  pointer to it stays valid across a reload.
- **The transport pool** — `RWMutex` with double-checked insert; replaced wholesale on
  `Reconfigure`, so in-flight requests finish on the transport they started with.
- **The drain flag** — `atomic.Bool`.
- **Redis** — `go-redis` pools connections; every multi-key read is a pipeline, and the usage write
  is a transaction so a drain never sees half a request.

Nothing takes a lock across an I/O call, and no request-scoped value is shared between requests.

### Why the layering earns its keep here

It is not architecture for its own sake — it bought three specific things:

1. **The decisions became testable without Redis.** `PickRoute` and `Evaluate` were always pure;
   the layering is what stopped them being reachable only through a live store. The services got
   their first tests at all, via `repository/memory`.
2. **The data path became testable without nginx.** When Lua owned the bytes, "does a stream reach
   the client unbuffered" was not a question any test could ask. It is now one `httptest` case.
3. **The seams are where change actually arrives.** Every request since this was built — SQLite for
   usage, offloading routing, a config file — landed on one of the three seams without touching
   `domain` or `service`.

### Where things live

| Question | File |
|---|---|
| Which engine gets this request? | `domain/route.go` — `PickRoute` |
| May this caller use this model? | `domain/access.go` — `CanUse`, `Evaluate` |
| How many tokens did that cost? | `domain/usage.go` — `ParseUsage` |
| Is this target broken? | `domain/health.go` |
| What does Redis look like? | `repository/redis/` — every key name and TTL |
| The request pipeline | `transport/http/middleware/builtin.go` |
| The proxy itself | `transport/http/proxy/` |
| Shutdown, drain, binary upgrade | `transport/http/lifecycle.go` |
| How a reload reaches running state | `cmd/grove-gateway/main.go` — `built.reload` |

---

## The request path

```
client
  │ TLS, HTTP/2
  ▼
recover → accesslog → drain → auth → quota → body → modelaccess → route → meter → transform → upstreamauth
  │
  ▼
proxy ──► engine (or ingress ──► engine)
```

| Stage | Does |
|---|---|
| `recover` | panic → 500, so nothing below can drop a connection |
| `accesslog` | times the request, writes the one durable line per request |
| `drain` | while shutting down: 503 + `Retry-After` + "gateway is restarting" |
| `auth` | bearer → key → user → group, once, into the request state |
| `quota` | the monthly budget flag the control plane pushed → 429 |
| `body` | bounded read + JSON decode, or a streaming form parse; `model` and the session hint come out here |
| `modelaccess` | `CanUse` → 403 |
| `route` | sticky / region / capacity / least-in-flight; claims an in-flight slot; mints the request id |
| `meter` | **deferred** release + usage record — runs on disconnect, panic and dead upstream alike |
| `transform` | the registered body rewrites; re-encodes only if one changed something |
| `upstreamauth` | swaps in the engine's internal key, sets the forwarding and ingress headers |

Order is load-bearing in two places. `drain` sits above `auth`, so a restarting gateway answers the
same way whether or not the caller's key is any good. `meter` sits directly below `route`, because
`route` claims a slot and everything below it must give that slot back.

The ingress chain is the same machinery, six entries: `recover`, `accesslog`, `drain`,
`ingressauth`, `pick`, `upstreamauth`.

#### Bodies that are not JSON

`model` decides routing and access, and on the multipart endpoints — `/v1/audio/transcriptions`,
`/v1/audio/translations`, `/v1/images/edits`, `/v1/files` — it arrives as a form field rather than a
JSON key. `body` branches on the content type and reads the form through a tee, stopping at `model`,
then hands the request on as the exact bytes the client sent: same boundary, same part ordering, same
encoding. A form is never re-encoded, and never becomes a `transform.Body`, so the transform stage
skips it rather than trying to serialise a form as JSON.

Two consequences worth knowing:

- **No priority injection.** vLLM priority is written *into* the JSON body, and a form has nowhere to
  put it, so these requests run at the engine's default priority.
- **Memory is bounded by a threshold, not by file size.** A client that sends `model` before its file
  captures almost nothing. One that sends it last has to capture everything it walked past, and that
  goes to memory up to 1 MiB and to a temp file beyond — nginx's `client_body_buffer_size` split,
  for the same reason. The file is created in `os.TempDir()` and **unlinked immediately**, so the
  descriptor is the only handle and the space returns when the request ends, including if the
  process dies first. Set `TMPDIR` on the unit to move it. An unwritable spill directory logs once
  and falls back to memory, which `max_body_bytes` still bounds — an upload is not worth refusing
  over a full disk.
- A request that announces a `Content-Length` over `max_body_bytes` is refused before anything is
  read; a chunked one is caught mid-stream instead, after the route was already picked.

#### Realtime sessions

A WebSocket upgrade — `/v1/realtime`, a live transcription session — is a `GET` with no body at all,
so `body` takes the model from the **query string**, which is where the OpenAI realtime API puts it
(`?model=…`, and `?user=` for the session hint). It reads and restores nothing: stamping a
`Content-Length` on an upgrade breaks the handshake before it reaches an engine.

Everything else applies unchanged — the key is resolved, the grant is checked, a route is picked,
and `/v1/realtime` is not a modality-claimed path so any model may serve it. Two consequences:

- **Usage lands at disconnect, not at connect.** `meter` is deferred, and for a hijacked connection
  the handler does not return until the session ends, so an open session is unbilled for as long as
  it stays open — and it is one `request_count` however long it ran. There are no token counts: once
  the connection is hijacked the usage tee never sees a frame.
- **The in-flight slot is held for the whole session.** The capacity gate was written for requests
  measured in seconds, so a long realtime session makes its engine look busier than it is.

`ModifyResponse` deliberately skips the usage tee on a `101`: ReverseProxy needs that body to stay an
`io.ReadWriteCloser` to write back to the engine, and wrapping it fails the handshake outright.

### A request, layer by layer

What each layer contributes, for one `POST /v1/chat/completions`:

| | Layer | |
|---|---|---|
| 1 | `transport/http` | TLS handshake, `ServeMux` matches host + path |
| 2 | `middleware/auth` | reads the `Authorization` header |
| 3 | `service/admission` | `Identify` → three store reads |
| 4 | `repository/redis` | `HGETALL key:… user:… group:…` |
| 5 | `domain` | `Evaluate` — pure, no I/O |
| 6 | `middleware/body` | bounded read, JSON decode, model out |
| 7 | `service/routing` | `Pick` — sticky read, in-flight counts, health |
| 8 | `domain` | `PickRoute` — pure again; the whole selection rule |
| 9 | `service/routing` | claim the slot, mint the request id |
| 10 | `middleware/transform` | body rewrites, re-encode if changed |
| 11 | `transport/http/proxy` | forward, stream back, scrape the usage frame |
| 12 | `middleware/meter` (deferred) | release the slot, record usage and outcome |

Two things fall out of that. The decisions — steps 5 and 8 — are pure functions over data someone
else fetched, which is why they are the parts with real test coverage. And nothing below step 1
knows it is speaking HTTP.

### Streaming

The response is never buffered. `FlushInterval: -1` pushes each write straight through, and the
usage scraper (`proxy/usagetee.go`) reads the bytes it is already copying and writes nothing back —
so the client sees exactly the stream the engine produced, at the engine's own pace.

---

## Routing

`domain.PickRoute` decides, in this order. Every step narrows the set the next one sees.

1. **Healthy and routable.** A row must be `healthy` (pushed by the control plane, then possibly
   flipped off by passive ejection) and have a non-empty `engine_url`. None left → **503**.
2. **Region tier.** If the gateway has a region and any row shares it, everything else is dropped.
   Two tiers only, no ranking within them: a cross-region hop costs so much more than the gap
   between two remote regions that ordering the far ones is precision nobody can feel. A row with
   no region counts as remote — the safe reading of "unknown".
3. **Capacity.** A row is out if `in_flight >= capacity` (the engine's `--max-num-seqs`). Capacity 0
   means uncapped. None left → **429**, deliberately not 503: the model is up, come back shortly.
4. **Stickiness.** If the caller's session is pinned to a row still in the set, that row wins.
   Stickiness loses to capacity by construction — a warm prefix cache is not worth queueing behind
   a full engine when a replica is idle.
5. **Least in flight.** Otherwise the row with the fewest live requests. A tie keeps the first.

### Session affinity

A caller names its session with the `X-Grove-Session` header or the body's `user` field; the header
wins. The pin lives at `sticky:<session>` for 30 minutes and holds a caller to one engine so its
prefix cache stays warm.

A caller that names nothing is **balanced** by default. Setting `synthetic_session_ttl` synthesises
one from `sha256(meter_id|model)`, which pins a whole API key to one engine — what a
single-placement fleet always did, and the lever to pull if balancing goes wrong.

Behind an ingress the same rule runs one tier down, keyed on `sha256(session)`. The gateway's pin
selects a *network*; the ingress's selects a *box*, which is the one that actually keeps a cache
warm. The hash is the boundary: the gateway never learns which box, the ingress never learns which
tenant.

### Passive ejection

No prober. Every request reports how its hop went, and three consecutive failures take a target out
for 60 seconds. A success clears the count outright, so it is three **in a row**, not three ever.

What counts as a failure is the part that matters: a connection error, a 502 or a 504 mean the hop
is broken. A 503 carrying `X-Grove-Reason: no-replica` means an ingress answered perfectly well and
one model has nowhere to go behind it — counting that would let a single unplaced model pull an
ingress out of rotation for every other model on it.

This is cheaper than active probing and strictly better informed: a probe tests a path no customer
is on.

---

## Admission

Three records, resolved in order — `key:` → `user:` → `group:`.

The split is deliberate. A credential's only fact of its own is whether it has been revoked;
**who holds it, what they may call and whether they are over budget are facts about the person.**
So one leaked key dies without touching the rest, and a budget flip is one write however many keys
that person holds.

`Evaluate` then runs three gates, in this order:

| Gate | Status | Why in this position |
|---|---|---|
| key is `active` | 401 | Checked first: a revoked key is 401 even for a holder who is also over quota, because the key is the thing that is wrong |
| holder not over budget | 429 | A pushed flag, not a counter — the gateway keeps no usage state of its own |
| `CanUse(model)` | 403 | |

`CanUse` is the whole access rule and fails closed:

```
deny wins over everything          usr.Deny[model]        → false
otherwise, group grant ∪ user's own allow                 → true
nothing granted it                                        → false
```

`/v1/models` filters its list with the *same function*, so the catalogue can never advertise
something the inference path would refuse.

### Rate limiting

There is exactly one limiter, and it is not in this process: a per-user **monthly token budget**.
The control plane sums `total_tokens - cached_tokens` when it pulls usage, flips `limited` on the
user, and pushes it. The gateway honours the flag and keeps no counters. Prefix caching is why the
subtraction exists — billing flat tokens when most of a prompt was a cache hit is a lie.

---

## Metering

One usage record per request, written in a single transaction so a control-plane drain never sees
half of one.

**Where the numbers come from.** The response's last newline-delimited line containing `"usage"` —
the final frame of an OpenAI stream, or the whole of a non-streaming body. Streaming requests only
have one because the `streamusage` transform forces `stream_options.include_usage` on the way in.

**Two engine shapes, one meaning.** vLLM answers OpenAI-shaped on `/v1/chat/completions` and
Anthropic-shaped on `/v1/messages`, and they disagree about what "input tokens" means:

| | OpenAI | Anthropic |
|---|---|---|
| prompt | `prompt_tokens` — **includes** cache | `input_tokens` — **excludes** it |
| cached | `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` |
| total | `total_tokens` | absent |

Both are normalised to: `Prompt` = the full input processed, `Total` = `Prompt + Completion`,
`Cached ⊆ Prompt`. Cache **creation** is a write, so it lands in `Prompt` but not in `Cached`.
Billable is then `Total - Cached` on either shape.

`cached_tokens` is only non-zero when the engine was started with
`--enable-prompt-tokens-details`. Without it every request bills as uncached.

**Each metric is written three times** into the same hash — flat, `m:<metric>:<model>`, and
`m:<metric>:<deployment>` — so one drain carries the aggregate and both breakdowns. Zero values are
skipped entirely; a field that never moved should not appear.

### Correlation

Every admitted request gets `gr-<gateway>-<deployment>-<key prefix>-<32 hex>`, canonical and
overriding whatever the client sent. vLLM adopts `X-Request-Id` as its own request id and
OpenAI-aware tooling reads it back, so one grep for it crosses the gateway log, the ingress log and
the engine log.

The target names the **Model Deployment**, not the box: one box can serve the same model from two
deployments, and naming the box makes those two requests indistinguishable.

---

## How to do things

### Add a middleware

One file in `transport/http/middleware/`, one `Register`, one name in the chain.

```go
func init() { Register("guardrails", newGuardrails) }

func newGuardrails(deps Deps) (Middleware, error) {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			state := From(r)          // Identity, Model, Body, Decision — whatever ran above
			if bad(state.Body) {
				deny(w, r, domain.Deny(http.StatusForbidden, "blocked by policy"))
				return
			}
			next.ServeHTTP(w, r)
		})
	}, nil
}
```

Then name it in `config.json` → `middleware`, and SIGUSR1. Nothing else changes. A name that is not
registered is refused and the running chain is kept.

If it needs something not on `Deps`, that is the signal to add it there — and to ask whether the
thing it needs belongs on the request path at all.

### Add a request transform

Body rewrites specifically. Same shape, in `service/transform/`:

```go
func init() { Register(alias{}) }

type alias struct{}

func (alias) Name() string        { return "alias" }
func (alias) Endpoints() []string { return nil }  // nil = every path
func (alias) Apply(ctx Context, body Body) (bool, error) { … }  // bool = "I changed something"
```

Each transform declares its own endpoint gate, so adding one for `/v1/messages` does not mean
editing a shared check. Return `false` when nothing changed — a body no transform touched is
forwarded byte-for-byte rather than re-encoded.

### Add a storage backend

Implement the interface in `repository/repository.go` and wire it in `cmd/grove-gateway/main.go`.
Nothing in `service/` knows which one it got. For a second destination (say a local SQLite archive
of usage), a decorator holding two `repository.Usage` values is the whole change — the primary's
error is returned, the archive's is logged.

### Offload a decision to another service

A middleware, not a new abstraction: `offload("route")` above `route`, POSTing the decision inputs
under a deadline. On 200 it puts the answer in the state and `route` no-ops; on timeout or error it
logs and falls through. Fallback is chain order.

**The invariant if you build this: the remote decides, the gateway always claims.** A slow-but-alive
remote that answers after you gave up would otherwise claim a slot nothing ever releases, and that
engine leaves rotation permanently.

---

## Configuration

Split by **lifetime**, and disjoint — nothing appears in both halves.

### Environment — identity, secrets, sockets, paths (`/etc/grove-gateway/agent.env`)

| | |
|---|---|
| `GROVE_ADMIN_TOKEN` | **required**; the process refuses to start without it |
| `GROVE_GATEWAY_ID` / `GROVE_INGRESS_ID` | which plane; both set is a refusal |
| `GROVE_INGRESS_TOKEN` | required on an ingress; blank there refuses every gateway |
| `GROVE_GATEWAY_REGION` | this gateway's region, which `PickRoute` prefers |
| `GROVE_REDIS_ADDR` | default `127.0.0.1:6379` |
| `GROVE_LISTEN_HTTP` / `GROVE_LISTEN_HTTPS` | the data path; at least one is required |
| `GROVE_PUBLIC_HOST` | the shared name customer traffic arrives on |
| `GROVE_SELF_HOST` | this box's own name — carries `/grove-admin` and the scrape |
| `GROVE_TLS_CERT` / `GROVE_TLS_KEY` | fleet wildcard; hot-reloaded from disk on change |
| `GROVE_HTPASSWD` | bcrypt htpasswd for `/metrics/node` |
| `GROVE_NODE_EXPORTER_URL` | default `http://127.0.0.1:9100/metrics` |
| `GROVE_ACCESS_LOG` | file for the per-request line; blank → stdout |
| `GROVE_ERROR_LOG` | file mirroring Warn and above out of the process log; blank → stdout only |
| `GROVE_CONFIG` | tunables path; default `/etc/grove-gateway/config.json` |
| `GROVE_PID_FILE` | optional |

### File — tunables, re-read on **SIGUSR1**

```json
{
  "log_level": "info",
  "middleware": ["recover", "accesslog", "drain", "auth", "quota", "body",
                 "modelaccess", "route", "meter", "transform", "upstreamauth"],
  "transforms": ["streamusage", "priority"],
  "synthetic_session_ttl": "0s",
  "max_body_bytes": 33554432,
  "upstream_read_timeout": "600s",
  "upstream_tls_verify": false,
  "drain_timeout": "630s",
  "lame_duck": "5s",
  "upgrade_timeout": "30s"
}
```

Every field is optional; an omitted one keeps its default, and a missing file is all defaults.

**A rejected file changes nothing.** Bad JSON, an unknown key, a bad duration, or an unregistered
middleware name → one error line and the running configuration is kept, applied whole or not at all.
Parsing is strict on purpose: a knob that silently became its default is a knob the operator
believes they turned.

`synthetic_session_ttl` is the one worth knowing. `0s` balances every caller that names no session
of its own; `30m` pins each API key to one engine, which is what a single-placement fleet always
did and the lever to pull if balancing goes wrong.

---

## Signals

| | |
|---|---|
| `SIGHUP` | **upgrade** — fork a child on the current binary, hand it the listening sockets |
| `SIGUSR1` | **reload** — re-read `config.json` |
| `SIGTERM` | **drain** — stop serving, finish what is in flight, exit |


### Upgrade

The child inherits the listening file descriptors and starts accepting immediately, while the parent
drains behind it. No connection is refused and none is queued unanswered — which matters here
because a streaming completion runs for minutes, so a stop/start would leave clients waiting that
long.

**A bad binary is a no-op.** If the child fails to start or never signals ready, the parent logs it
and keeps serving on the old binary.

Two things to respect:
- Replace the binary by **rename**, never by truncation — overwriting a running executable fails
  with `text file busy`. `ansible.builtin.copy` already writes-then-renames.
- Start it by **absolute path**. The child is spawned from `os.Args[0]`; a relative one resolves
  against the working directory. The process warns once at startup if it sees one.

### Drain

The flag flips first and the process keeps *accepting* for `lame_duck` (default 5s), answering 503.
That window is what lets a health check notice: `Server.Shutdown` closes the listener at once, so
without it a fresh connection gets refused rather than an answer, and only an already-open keep-alive
connection would ever see the 503. Then live handlers get `drain_timeout` (default 630s — longer
than the upstream read timeout, so a stream the engine would have finished is never cut here first),
and anything past that is closed. Closed handlers still run their defers, so every in-flight slot is
released and whatever usage was captured is recorded.

No lame-duck on an *upgrade* handover: the child is already accepting on the same socket.

---

## The contract with the control plane

Grove pushes state; the gateway projects it into local Redis and never calls back. **These shapes
are the interface — changing one means changing `agent_sync.py` and `usage_pull.py` too.**

| Key | Type | Written by |
|---|---|---|
| `key:<sha256(secret)>` | hash | `PUT /grove-admin/keys` |
| `user:<Grove User>` | hash | `PUT /grove-admin/users` |
| `group:<Grove User Group>` | hash | `PUT /grove-admin/groups` |
| `deploy:<model>` | JSON array of routes | `PUT /grove-admin/routes` |
| `catalog:public` | comma list | `PUT /grove-admin/groups` |
| `usage:<key prefix>` | hash | the gateway; drained by `GET /grove-admin/usage` |
| `sticky:<session>` | string, 30m | the gateway |
| `inflight:<engine>` | sorted set, member = request id | the gateway |
| `health:<target>` | counter, 60s | the gateway |

Every admin endpoint is gated on `X-Grove-Admin-Token`, compared in constant time, and mounted on
`GROVE_SELF_HOST` only — a push has to reach **one** gateway, and the public name means all of them.

`GET /grove-admin/usage` **reads and deletes** in one step. The returned snapshot is the only copy,
so it never double-counts and a control-plane crash loses at most one cycle.

### The records themselves

```
key:<sha256(secret)>      status  user  prefix
user:<Grove User>         email  group  allow  deny  limited
group:<Grove User Group>  models  priority
catalog:public            "model-a,model-b"
usage:<key prefix>        request_count  prompt_tokens  completion_tokens  total_tokens
                          cached_tokens  m:<metric>:<model>  m:<metric>:<deployment>
```

`allow` / `deny` / `models` are comma lists; blank parses to a map that answers false to
everything, which is the fail-closed default. `priority` is already sign-flipped by Grove into
vLLM's convention (lowest served first), so the gateway never reasons about the sign.

`deploy:<model>` is a JSON array, replaced whole:

```json
[{
  "engine_url":   "https://10.0.0.9/e/md-00007",
  "internal_key": "<the engine's own key>",
  "healthy":      true,
  "region":       "ap-south-1",
  "capacity":     1024,
  "deployment":   "MD-00007",
  "server":       "INF-1",
  "kind":         "direct"
}]
```

`engine_url` is a **base**; the path the client asked for is appended to it. `in_flight` is
computed here and never pushed — the control plane has no view of what is running right now.

### Backwards compatibility that is still load-bearing

- A key record written before access moved off the credential carries `group`/`allow`/`deny`/
  `models` itself. If `user:<name>` is missing, those are used. This is what makes the control
  plane and the gateway deployable in **either order**.
- A *current* key with no user record resolves to nothing rather than falling back to something.
  Fail closed — the difference between the two is the whole quarantine.
- `status: "rate_limited"` was once a third value on the credential. It is lifted off on read, so
  `status` means only "is this live".
- A route with no `kind` is `direct`, which is what every route pushed before the split was.

### Public endpoints

| | |
|---|---|
| `POST /v1/*` | the data path |
| `GET /v1/models` | answered here, never forwarded — an engine only knows its own model. With a key: what that key may use. Without one: the public catalogue |
| `GET /healthz` | 200, or 503 while draining |
| `GET /metrics/node` | node_exporter behind bcrypt basic auth |

---

## When things break

The rule everywhere: **degrade toward serving.** A gateway that refuses traffic because a counter
was unreadable turns one broken dependency into an outage.

| What fails | What happens |
|---|---|
| Redis unreachable at **startup** | refuses to start — a gateway that cannot read its keys serves nothing, and finding out on the first customer request would report it as a routing fault |
| `key:` / `user:` / `group:` read fails | **503**, never 401 — "we cannot read your key" must not send someone to rotate a credential that was fine |
| in-flight counts unreadable | every count reads 0, so the pick degrades to first-healthy. Balancing is an optimisation on a table that is already correct |
| health counters unreadable | every route stays as the control plane pushed it. Ejection is an optimisation too |
| sticky read/write fails | one cold prefix cache, not a wrong answer |
| usage write fails | logged; the request already succeeded and failing it retroactively helps nobody |
| the engine is dead | **502**, and the hop counts against the target — three in a row and it leaves rotation |
| every replica is full | **429**, distinct from 503 on purpose: the model is up |
| the model has no placement | **503** |
| the control plane is down | nothing happens. The gateway serves the last table it was pushed, indefinitely |
| the tunables file is bad | the running configuration is kept, one error line says why |
| a new binary will not start | the old process keeps serving; the upgrade is a no-op |
| the certificate file is unreadable | the certificate already in memory keeps being served |

### Status vocabulary

Every refusal is OpenAI-shaped — `{"error":{"message":…,"type":"grove_gateway"}}` — so a client
parses them the same way whichever gate produced it.

| | Means | Client should |
|---|---|---|
| 401 | no key, unknown key, revoked key | fix the credential |
| 403 | the key exists but may not use this model | ask for access |
| 413 | body over `max_body_bytes` | send less |
| 429 | over monthly budget, **or** every replica at capacity | back off and retry |
| 502 | the engine could not be reached or failed the hop | retry; another replica may take it |
| 503 | the model has no healthy placement, a store is unreadable, or the gateway is draining | retry with backoff — `Retry-After` is set when draining |

---

## Running it locally

```sh
go build ./... && go vet ./... && go test ./...
go test -race ./internal/transport/http
```

The tests need nothing running: `repository/memory` is a behaving in-memory store, and
`internal/transport/http/dataplane_test.go` drives the whole chain against a fake vLLM over
`httptest`. That is the file to read first — it shows the request as the engine receives it.

Against a real Redis:

```sh
redis-server --port 6399 --save '' --daemonize yes
GROVE_ADMIN_TOKEN=tok GROVE_GATEWAY_ID=gw-dev \
GROVE_REDIS_ADDR=127.0.0.1:6399 GROVE_LISTEN_HTTP=127.0.0.1:8080 \
go run ./cmd/grove-gateway
```

Seed it the way the control plane does, then call it:

```sh
curl -XPUT localhost:8080/grove-admin/groups -H 'X-Grove-Admin-Token: tok' \
  -d '{"groups":[{"name":"acme","priority":-10,"models":"qwen3-4b"}]}'
curl -XPUT localhost:8080/grove-admin/users -H 'X-Grove-Admin-Token: tok' \
  -d '{"users":[{"name":"you","group":"acme"}]}'
curl -XPUT localhost:8080/grove-admin/keys -H 'X-Grove-Admin-Token: tok' \
  -d "{\"keys\":[{\"key_hash\":\"$(printf gr_sk_demo | sha256sum | cut -d' ' -f1)\",\"prefix\":\"dev\",\"user\":\"you\",\"status\":\"active\"}]}"
curl -XPUT localhost:8080/grove-admin/routes -H 'X-Grove-Admin-Token: tok' \
  -d '{"routes":{"qwen3-4b":[{"engine_url":"http://127.0.0.1:8000","internal_key":"k","healthy":true,"deployment":"MD-1","kind":"direct"}]}}'

curl localhost:8080/v1/chat/completions -H 'Authorization: Bearer gr_sk_demo' \
  -d '{"model":"qwen3-4b","messages":[]}'
redis-cli -p 6399 HGETALL usage:dev
```

With nothing listening on `:8000` that last call is a 502 and only `request_count` accrues — which
is itself the correct behaviour, and worth seeing once: an abandoned or failed request still counts
as a request, and the failure counts against the target. Point `engine_url` at a real vLLM (or the
fake in `dataplane_test.go`) for token counts.

---

## Deploying

Built on the target box by `playbooks/roles/build_gateway_agent` (`go build ./cmd/grove-gateway`),
installed to `/usr/local/bin/grove-gateway`, run under systemd with
`Type=notify` + `NotifyAccess=all` — required, because the PID changes on an upgrade and systemd has
to follow the child's `MAINPID`.

- **New binary** → copy + `systemctl reload` (SIGHUP). No dropped connections.
- **Changed tunable** → write `config.json` + SIGUSR1. No restart.
- **Changed `agent.env`** → restart. The child reads the environment from systemd, so a reload would
  not pick it up. This is the only case that drains, and it is rare.
- **Renewed certificate** → copy the file. Nothing else: the loader watches its mtime.

---

## Things worth knowing before changing something

- **`domain/` must not grow an import.** Its purity is what lets a remote router, or a second
  process, run the identical rule.
- **Anything that claims an in-flight slot must release it.** `meter` does it in a `defer` for
  exactly this reason. One owner for that counter, always.
- **The usage scraper must never modify the response.** It reads what it is already copying. A
  metering feature that reordered or buffered a token stream would be worse than no metering.
- **`upstream_tls_verify` is per target, not per listener.** That is the thing nginx could not do —
  `proxy_ssl_verify` is a per-location directive, so one self-signed box pinned verification off for
  every target sharing that location. Defaulted `false` for parity; it is a default, not a ceiling.
- **Least-in-flight balances concurrent traffic, not sequential.** A sequential caller releases its
  slot before the next pick, so both replicas read zero and the tie takes the first. Fine for one
  chatty client — it keeps a prefix cache warm — but a fleet of many sequential clients pins them
  all to one engine.
