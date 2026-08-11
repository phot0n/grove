package proxy

import (
	"bytes"
	"io"
)

// carryLimit bounds the partial line held across reads — a single huge non-streaming body. The tail
// is what matters, because the usage object is at the end of it.
const carryLimit = 256 << 10

// usageTee keeps the last newline-delimited line containing "usage" — the final frame of a stream,
// or a whole non-streaming body. It reads what it is already copying and writes nothing back, so
// the stream reaches the client byte-for-byte and on time. That property is the contract.
type usageTee struct {
	body  io.ReadCloser
	carry []byte
	line  []byte
}

func newUsageTee(body io.ReadCloser) *usageTee { return &usageTee{body: body} }

func (t *usageTee) Read(p []byte) (int, error) {
	n, err := t.body.Read(p)
	if n > 0 {
		t.scan(p[:n])
	}
	if err == io.EOF {
		t.flush()
	}
	return n, err
}

func (t *usageTee) Close() error {
	// A client that hung up mid-stream still leaves a partial line worth reading: it may be the
	// usage frame of a response the engine finished generating.
	t.flush()
	return t.body.Close()
}

// Usage is the captured line, or empty if the response never carried one.
func (t *usageTee) Usage() string { return string(t.line) }

func (t *usageTee) scan(chunk []byte) {
	data := chunk
	if len(t.carry) > 0 {
		data = append(t.carry, chunk...)
		t.carry = nil
	}
	for {
		newline := bytes.IndexByte(data, '\n')
		if newline < 0 {
			break
		}
		t.keep(data[:newline])
		data = data[newline+1:]
	}
	if len(data) > carryLimit {
		data = data[len(data)-carryLimit:]
	}
	// Copied rather than aliased: `p` belongs to the caller and is reused on the next Read.
	t.carry = append([]byte(nil), data...)
}

func (t *usageTee) flush() {
	t.keep(t.carry)
	t.carry = nil
}

func (t *usageTee) keep(line []byte) {
	if bytes.Contains(line, []byte(`"usage"`)) {
		t.line = append([]byte(nil), line...)
	}
}
