package domain

// Denial carries the status to answer with — 401 credential, 403 grant, 429 budget or full engine,
// 503 model or store — so a handler translates one type instead of each service's own errors. A
// store failure is a 503 Denial too: to the caller it is the same answer, and the why goes in the log.
type Denial struct {
	Status int
	Reason string
}

func (d Denial) Error() string { return d.Reason }

func Deny(status int, reason string) Denial { return Denial{Status: status, Reason: reason} }
