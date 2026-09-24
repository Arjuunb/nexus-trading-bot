package nexus

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"math"
	"strconv"
	"strings"
	"time"
)

// VerifyWebhook checks a webhook's Nexus-Signature header against the raw
// request body. Call it before parsing and reject the request when it is false.
// tolerance is how old the signature's timestamp may be (use 5*time.Minute).
func VerifyWebhook(secret string, body []byte, header string, tolerance time.Duration, now time.Time) bool {
	var t int64
	var sig string
	for _, part := range strings.Split(header, ",") {
		k, v, ok := strings.Cut(part, "=")
		if !ok {
			return false
		}
		switch k {
		case "t":
			n, err := strconv.ParseInt(v, 10, 64)
			if err != nil {
				return false
			}
			t = n
		case "v1":
			sig = v
		}
	}
	if t == 0 || sig == "" || math.Abs(float64(now.Unix()-t)) > tolerance.Seconds() {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(strconv.FormatInt(t, 10) + "."))
	mac.Write(body)
	expected := hex.EncodeToString(mac.Sum(nil))
	return hmac.Equal([]byte(expected), []byte(sig))
}
