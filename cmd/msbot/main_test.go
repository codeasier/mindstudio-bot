package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync/atomic"
	"testing"
)

func TestRegistrationPersistsBeforeDispatch(t *testing.T) {
	directory := t.TempDir()
	const identity = "a6a8f9d4-203a-4388-bc52-493959578b18"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, err := os.Stat(filepath.Join(directory, identity+".json")); err != nil {
			t.Error("operation receipt must exist before request")
		}
		if r.Header.Get("Authorization") != "Bearer synthetic-admin" || r.URL.Path != "/api/repos" {
			t.Error("incorrect management request")
		}
		var request map[string]any
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request["repository"] != "owner/repo" || request["create_webhook"] != false {
			t.Error("unexpected registration parameters")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"data":{"status":"succeeded"}}`))
	}))
	defer server.Close()
	env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin", "MSBOT_OPERATION_DIR": directory}
	var output, logs bytes.Buffer
	err := run([]string{"repo", "add", "owner/repo", "--manual-webhook", "--operation-id", identity}, func(k string) string { return env[k] }, &output, &logs)
	if err != nil || !json.Valid(output.Bytes()) || !strings.Contains(logs.String(), identity) {
		t.Fatalf("registration failed: %v", err)
	}
	receipt, _ := os.ReadFile(filepath.Join(directory, identity+".json"))
	if bytes.Contains(receipt, []byte("synthetic-admin")) {
		t.Fatal("credentials leaked to operation receipt")
	}
}

func TestUnknownRegistrationIsNotSuccess(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true,"data":{"status":"blocked"}}`))
	}))
	defer server.Close()
	env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin", "MSBOT_OPERATION_DIR": t.TempDir()}
	var output, logs bytes.Buffer
	if err := run([]string{"repo", "add", "owner/repo"}, func(k string) string { return env[k] }, &output, &logs); err == nil {
		t.Fatal("blocked registration must return an error")
	}
}

func TestOperationGetDoesNotFailForBackgroundFailure(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"ok":true,"data":{"status":"failed"}}`))
	}))
	defer server.Close()
	env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin"}
	var output, logs bytes.Buffer
	if err := run([]string{"operation", "get", "original-id"}, func(k string) string { return env[k] }, &output, &logs); err != nil {
		t.Fatal(err)
	}
}

func TestInvalidOperationIDDoesNotCreateReceipt(t *testing.T) {
	env := map[string]string{"MSBOT_ADMIN_TOKEN": "synthetic", "MSBOT_OPERATION_DIR": t.TempDir()}
	var output, logs bytes.Buffer
	err := run([]string{"repo", "add", "owner/repo", "--operation-id", "../../invalid"}, func(k string) string { return env[k] }, &output, &logs)
	if err == nil || !strings.Contains(err.Error(), "UUIDv4") {
		t.Fatal("invalid operation identity was accepted")
	}
}

func TestTaskRetryPersistsTargetBeforeDispatch(t *testing.T) {
	directory := t.TempDir()
	const identity = "a6a8f9d4-203a-4388-bc52-493959578b18"
	const taskID = "task/with space?and=query"
	const token = "synthetic-admin-do-not-store"
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.Method != http.MethodPost || r.URL.EscapedPath() != "/api/tasks/"+url.PathEscape(taskID)+"/retry" || r.URL.RawQuery != "" {
			t.Errorf("incorrect retry target: %s %s", r.Method, r.URL)
		}
		if r.Header.Get("Authorization") != "Bearer "+token || r.Header.Get("Content-Type") != "application/json" {
			t.Error("missing management authentication or content type")
		}
		var body map[string]string
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body) != 1 || body["operation_id"] != identity {
			t.Errorf("incorrect retry body: %v (%v)", body, err)
		}
		content, err := os.ReadFile(filepath.Join(directory, identity+".json"))
		if err != nil {
			t.Errorf("operation receipt missing before dispatch: %v", err)
		} else {
			var receipt struct {
				OperationID string            `json:"operation_id"`
				Instance    string            `json:"instance"`
				Request     map[string]string `json:"request"`
			}
			if err := json.Unmarshal(content, &receipt); err != nil || receipt.OperationID != identity || receipt.Instance != "http://"+r.Host || receipt.Request["task_id"] != taskID || receipt.Request["operation_id"] != identity {
				t.Errorf("incorrect persisted retry receipt: %s (%v)", content, err)
			}
			if bytes.Contains(content, []byte(token)) {
				t.Error("credentials leaked to retry receipt")
			}
		}
		_, _ = w.Write([]byte(`{"ok":true,"data":{"status":"succeeded","result":{"task_id":"new-task","retry_of":"original-task"}}}`))
	}))
	defer server.Close()
	env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": token, "MSBOT_OPERATION_DIR": directory}
	for attempt := 0; attempt < 2; attempt++ {
		var output, logs bytes.Buffer
		err := run([]string{"task", "retry", taskID, "--operation-id", identity}, func(k string) string { return env[k] }, &output, &logs)
		if err != nil || !json.Valid(output.Bytes()) || !strings.Contains(logs.String(), "msbot operation get "+identity) {
			t.Fatalf("retry failed: %v, output=%s, logs=%s", err, &output, &logs)
		}
		if strings.Contains(output.String()+logs.String(), token) {
			t.Fatal("credentials leaked to CLI output")
		}
	}
	if calls.Load() != 2 {
		t.Fatalf("same target and operation ID should be reusable, calls=%d", calls.Load())
	}
	info, err := os.Stat(filepath.Join(directory, identity+".json"))
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("receipt must be private: %v", err)
	}
}

func TestTaskRetryGeneratesOperationID(t *testing.T) {
	directory := filepath.Join(t.TempDir(), "operations")
	identities := make(chan string, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		operationID := body["operation_id"]
		if !regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`).MatchString(operationID) {
			t.Errorf("not a generated UUIDv4: %q", operationID)
		}
		if _, err := os.Stat(filepath.Join(directory, operationID+".json")); err != nil {
			t.Errorf("generated receipt must exist before dispatch: %v", err)
		}
		identities <- operationID
		_, _ = w.Write([]byte(`{"ok":true,"data":{"status":"succeeded","result":{"task_id":"new-task","retry_of":"original-task"}}}`))
	}))
	defer server.Close()
	env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin", "MSBOT_OPERATION_DIR": directory}
	var output, logs bytes.Buffer
	if err := run([]string{"task", "retry", "original-task"}, func(k string) string { return env[k] }, &output, &logs); err != nil {
		t.Fatal(err)
	}
	operationID := <-identities
	if !strings.Contains(logs.String(), "msbot operation get "+operationID) {
		t.Fatal("generated operation ID was not displayed")
	}
	info, err := os.Stat(directory)
	if err != nil || info.Mode().Perm() != 0o700 {
		t.Fatalf("operation directory must be private: %v", err)
	}
}

func TestTaskRetryRejectsReceiptCollisionsBeforeDispatch(t *testing.T) {
	const identity = "a6a8f9d4-203a-4388-bc52-493959578b18"
	cases := []struct {
		name string
		base string
		body map[string]string
	}{
		{"other task", "", map[string]string{"operation_id": identity, "task_id": "another-task"}},
		{"other command", "", map[string]string{"operation_id": identity, "repository": "owner/repo"}},
		{"other instance", "https://other.example.com", map[string]string{"operation_id": identity, "task_id": "original-task"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			directory := t.TempDir()
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				calls.Add(1)
				_, _ = w.Write([]byte(`{"ok":true,"data":{"status":"succeeded"}}`))
			}))
			defer server.Close()
			base := tc.base
			if base == "" {
				base = server.URL
			}
			body, _ := json.Marshal(tc.body)
			if err := persistOperation(directory, identity, base, body); err != nil {
				t.Fatal(err)
			}
			before, _ := os.ReadFile(filepath.Join(directory, identity+".json"))
			env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin", "MSBOT_OPERATION_DIR": directory}
			var output, logs bytes.Buffer
			err := run([]string{"task", "retry", "original-task", "--operation-id", identity}, func(k string) string { return env[k] }, &output, &logs)
			if err == nil || !strings.Contains(err.Error(), "different parameters") || calls.Load() != 0 {
				t.Fatalf("collision must fail before dispatch: err=%v, calls=%d", err, calls.Load())
			}
			after, _ := os.ReadFile(filepath.Join(directory, identity+".json"))
			if !bytes.Equal(before, after) {
				t.Fatal("collision overwrote the original receipt")
			}
		})
	}
}

func TestTaskRetryValidationBeforeDispatch(t *testing.T) {
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"missing task", []string{"task", "retry"}, "requires a task ID"},
		{"empty task", []string{"task", "retry", " "}, "requires a task ID"},
		{"flag without task", []string{"task", "retry", "--operation-id", "invalid"}, "requires a task ID"},
		{"invalid identity", []string{"task", "retry", "original-task", "--operation-id", "../../invalid"}, "UUIDv4"},
		{"wrong UUID version", []string{"task", "retry", "original-task", "--operation-id", "a6a8f9d4-203a-1388-bc52-493959578b18"}, "UUIDv4"},
		{"unexpected positional", []string{"task", "retry", "original-task", "extra"}, "unexpected arguments"},
		{"unknown flag", []string{"task", "retry", "original-task", "--force"}, "flag provided but not defined"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			directory := t.TempDir()
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				calls.Add(1)
				_, _ = w.Write([]byte(`{}`))
			}))
			defer server.Close()
			env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin", "MSBOT_OPERATION_DIR": directory}
			var output, logs bytes.Buffer
			err := run(tc.args, func(k string) string { return env[k] }, &output, &logs)
			if err == nil || !strings.Contains(err.Error(), tc.want) || calls.Load() != 0 {
				t.Fatalf("invalid invocation was not rejected: err=%v, calls=%d", err, calls.Load())
			}
			files, _ := os.ReadDir(directory)
			if len(files) != 0 {
				t.Fatal("invalid invocation created a receipt")
			}
		})
	}
}

func TestTaskRetryRequiresSafeURLAndAuthentication(t *testing.T) {
	var calls atomic.Int32
	previous := http.DefaultTransport
	http.DefaultTransport = roundTripFunc(func(_ *http.Request) (*http.Response, error) {
		calls.Add(1)
		return nil, errors.New("unexpected dispatch")
	})
	t.Cleanup(func() { http.DefaultTransport = previous })
	cases := []struct {
		name  string
		base  string
		token string
		want  string
	}{
		{"missing authentication", "http://127.0.0.1:8080", "", "set MSBOT_ADMIN_TOKEN"},
		{"remote cleartext", "http://example.com", "synthetic-admin", "use HTTPS"},
		{"URL credentials", "https://user:synthetic-admin@example.com", "synthetic-admin", "without credentials or query"},
		{"URL query", "https://example.com?token=synthetic-admin", "synthetic-admin", "without credentials or query"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			directory := t.TempDir()
			env := map[string]string{"MSBOT_URL": tc.base, "MSBOT_ADMIN_TOKEN": tc.token, "MSBOT_OPERATION_DIR": directory}
			var output, logs bytes.Buffer
			err := run([]string{"task", "retry", "original-task"}, func(k string) string { return env[k] }, &output, &logs)
			if err == nil || !strings.Contains(err.Error(), tc.want) || calls.Load() != 0 {
				t.Fatalf("unsafe invocation was not rejected: %v", err)
			}
			if strings.Contains(err.Error()+output.String()+logs.String(), "synthetic-admin") {
				t.Fatal("credentials leaked in validation output")
			}
			files, _ := os.ReadDir(directory)
			if len(files) != 0 {
				t.Fatal("unsafe invocation created a receipt")
			}
		})
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

func TestTaskRetryPersistenceFailurePreventsDispatch(t *testing.T) {
	file := filepath.Join(t.TempDir(), "not-a-directory")
	if err := os.WriteFile(file, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()
	env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin", "MSBOT_OPERATION_DIR": file}
	var output, logs bytes.Buffer
	err := run([]string{"task", "retry", "original-task"}, func(k string) string { return env[k] }, &output, &logs)
	if err == nil || !strings.Contains(err.Error(), "cannot persist operation before dispatch") || calls.Load() != 0 {
		t.Fatalf("persistence failure must prevent dispatch: err=%v, calls=%d", err, calls.Load())
	}
}

func TestTaskRetryFailureKeepsReceiptAndDoesNotResubmit(t *testing.T) {
	const identity = "a6a8f9d4-203a-4388-bc52-493959578b18"
	cases := []struct {
		name   string
		status int
		body   string
		want   string
	}{
		{"unauthorized", 401, `{"detail":"Invalid administrator credentials"}`, "HTTP 401"},
		{"unknown task", 404, `{"detail":"Unknown audit"}`, "HTTP 404"},
		{"ineligible task", 409, `{"ok":false,"error":"Task cannot be retried"}`, "HTTP 409"},
		{"unavailable", 503, `{"ok":false,"error":"configuration_unavailable"}`, "HTTP 503"},
		{"blocked operation", 200, `{"ok":true,"data":{"status":"blocked"}}`, "not confirmed successful"},
		{"failed operation", 200, `{"ok":true,"data":{"status":"failed"}}`, "not confirmed successful"},
		{"unsuccessful envelope", 200, `{"ok":false,"data":{"status":"succeeded"}}`, "not confirmed successful"},
		{"incomplete envelope", 200, `{"ok":true}`, "not confirmed successful"},
		{"invalid JSON", 200, `not JSON`, "invalid or oversized management response"},
		{"oversized response", 200, strings.Repeat(" ", 1_048_577), "invalid or oversized management response"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			directory := t.TempDir()
			var calls atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				calls.Add(1)
				w.WriteHeader(tc.status)
				_, _ = w.Write([]byte(tc.body))
			}))
			defer server.Close()
			env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin", "MSBOT_OPERATION_DIR": directory}
			var output, logs bytes.Buffer
			err := run([]string{"task", "retry", "original-task", "--operation-id", identity}, func(k string) string { return env[k] }, &output, &logs)
			if err == nil || !strings.Contains(err.Error(), tc.want) || calls.Load() != 1 {
				t.Fatalf("failed retry must not resubmit: err=%v, calls=%d", err, calls.Load())
			}
			if _, err := os.Stat(filepath.Join(directory, identity+".json")); err != nil {
				t.Fatal("failed retry lost its receipt")
			}
			if !strings.Contains(logs.String(), "msbot operation get "+identity) {
				t.Fatal("failed retry must preserve the operation query hint")
			}
			if strings.Contains(err.Error()+output.String()+logs.String(), "synthetic-admin") {
				t.Fatal("credentials leaked in failure output")
			}
		})
	}
}

func TestTaskRetryInterruptedRequestKeepsReceipt(t *testing.T) {
	const identity = "a6a8f9d4-203a-4388-bc52-493959578b18"
	directory := t.TempDir()
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls.Add(1)
		connection, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Error(err)
			return
		}
		_ = connection.Close()
	}))
	defer server.Close()
	env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin", "MSBOT_OPERATION_DIR": directory}
	var output, logs bytes.Buffer
	err := run([]string{"task", "retry", "original-task", "--operation-id", identity}, func(k string) string { return env[k] }, &output, &logs)
	if err == nil || !strings.Contains(err.Error(), "request interrupted") || calls.Load() != 1 {
		t.Fatalf("interrupted retry must not resubmit: err=%v, calls=%d", err, calls.Load())
	}
	if _, err := os.Stat(filepath.Join(directory, identity+".json")); err != nil {
		t.Fatal("interrupted retry lost its receipt")
	}
	if !strings.Contains(logs.String(), "msbot operation get "+identity) || strings.Contains(err.Error()+output.String()+logs.String(), "synthetic-admin") {
		t.Fatal("unsafe or missing interrupted-operation diagnostics")
	}
}

func TestTaskRetryDoesNotFollowRedirects(t *testing.T) {
	var redirected atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		redirected.Add(1)
		_, _ = w.Write([]byte(`{"ok":true,"data":{"status":"succeeded"}}`))
	}))
	defer target.Close()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Location", target.URL)
		w.WriteHeader(http.StatusTemporaryRedirect)
		_, _ = w.Write([]byte(`{"ok":false,"error":"redirect"}`))
	}))
	defer server.Close()
	env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin", "MSBOT_OPERATION_DIR": t.TempDir()}
	var output, logs bytes.Buffer
	err := run([]string{"task", "retry", "original-task"}, func(k string) string { return env[k] }, &output, &logs)
	if err == nil || !strings.Contains(err.Error(), "HTTP 307") || redirected.Load() != 0 {
		t.Fatalf("retry must not follow redirects with credentials: err=%v, redirected=%d", err, redirected.Load())
	}
}

func TestTaskStats(t *testing.T) {
	directory := t.TempDir()
	const response = `{"ok":true,"data":{"counts":{"queued":1,"running":2,"failed":3,"blocked":4,"passed":5,"rejected":6,"ignored":7},"failed":3,"blocked":4}}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/tasks/stats" || r.Header.Get("Authorization") != "Bearer synthetic-admin" {
			t.Errorf("incorrect authenticated stats request: %s %s", r.Method, r.URL)
		}
		if r.ContentLength != 0 {
			t.Error("stats must not send a request body")
		}
		_, _ = w.Write([]byte(response))
	}))
	defer server.Close()
	env := map[string]string{"MSBOT_URL": server.URL, "MSBOT_ADMIN_TOKEN": "synthetic-admin", "MSBOT_OPERATION_DIR": directory}
	var output, logs bytes.Buffer
	if err := run([]string{"task", "stats"}, func(k string) string { return env[k] }, &output, &logs); err != nil {
		t.Fatal(err)
	}
	var result struct {
		Data struct {
			Counts map[string]int
			Failed int
		}
	}
	if err := json.Unmarshal(output.Bytes(), &result); err != nil || result.Data.Counts["failed"] != 3 || result.Data.Failed != 3 {
		t.Fatalf("stats response changed: %s (%v)", &output, err)
	}
	files, _ := os.ReadDir(directory)
	if len(files) != 0 || logs.Len() != 0 {
		t.Fatal("read-only stats should not create an operation receipt")
	}
}
