package domain

// Denial is a refusal with the status the caller should be answered with. Every gate in the
// admission path speaks this vocabulary — 401 the credential, 403 the grant, 429 the budget or a
// full engine, 503 the model or the store — so a handler translates one type rather than mapping
// each service's own errors.
//
// A store failure is a Denial too (503): from the caller's side "we cannot read your key" and "we
// cannot reach an engine" are the same answer, and the difference belongs in the log, not the body.
type Denial struct {
	Status int
	Reason string
}

func (d Denial) Error() string { return d.Reason }

func Deny(status int, reason string) Denial { return Denial{Status: status, Reason: reason} }
