package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
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
