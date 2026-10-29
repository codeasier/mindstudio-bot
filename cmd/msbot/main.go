package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

func newID() (string, error) {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		return "", err
	}
	value[6] = value[6]&0x0f | 0x40
	value[8] = value[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", value[:4], value[4:6], value[6:8], value[8:10], value[10:]), nil
}

func persistOperation(directory string, operationID string, base string, body []byte) error {
	if directory == "" {
		cache, err := os.UserCacheDir()
		if err != nil {
			return err
		}
		directory = filepath.Join(cache, "msbot", "operations")
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	path := filepath.Join(directory, operationID+".json")
	receipt, _ := json.Marshal(map[string]any{
		"operation_id": operationID, "instance": base, "request": json.RawMessage(body),
	})
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if errors.Is(err, os.ErrExist) {
		existing, readErr := os.ReadFile(path)
		if readErr != nil || !bytes.Equal(existing, receipt) {
			return errors.New("operation receipt already exists with different parameters")
		}
		return nil
	}
	if err != nil {
		return err
	}
	defer file.Close()
	if _, err = file.Write(receipt); err != nil {
		return err
	}
	return file.Sync()
}

func run(args []string, getenv func(string) string, stdout io.Writer, stderr io.Writer) error {
	if len(args) < 2 {
		return errors.New("usage: msbot repo add OWNER/REPO [flags] | repo list | task list/get ID | operation get ID")
	}
	base := strings.TrimRight(getenv("MSBOT_URL"), "/")
	if base == "" {
		base = "http://127.0.0.1:8080"
	}
	parsed, err := url.Parse(base)
	if err != nil || parsed.Host == "" || parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return errors.New("MSBOT_URL must be an HTTP(S) instance URL without credentials or query")
	}
	if parsed.Scheme != "https" && !(parsed.Scheme == "http" && (parsed.Hostname() == "127.0.0.1" || parsed.Hostname() == "localhost" || parsed.Hostname() == "::1")) {
		return errors.New("use HTTPS for a remote instance")
	}
	token := getenv("MSBOT_ADMIN_TOKEN")
	if token == "" {
		return errors.New("set MSBOT_ADMIN_TOKEN")
	}
	method := http.MethodGet
	path := ""
	var body []byte
	operationID := ""
	if args[0] == "repo" && args[1] == "add" {
		if len(args) < 3 {
			return errors.New("repo add requires OWNER/REPO")
		}
		flags := flag.NewFlagSet("repo add", flag.ContinueOnError)
		flags.SetOutput(stderr)
		tokenEnv := flags.String("token-env", "MSBOT_GITCODE_TOKEN", "server-side GitCode token environment name")
		secretEnv := flags.String("webhook-secret-env", "MSBOT_GITCODE_WEBHOOK_SECRET", "server-side webhook secret environment name")
		manual := flags.Bool("manual-webhook", false, "return callback URL for manual Webhook setup")
		signature := flags.Bool("signature", false, "use GitCode signature-key mode")
		identity := flags.String("operation-id", "", "reuse the UUIDv4 of an interrupted operation")
		if err := flags.Parse(args[3:]); err != nil {
			return err
		}
		if flags.NArg() != 0 {
			return errors.New("unexpected arguments")
		}
		operationID = *identity
		if operationID == "" {
			operationID, err = newID()
			if err != nil {
				return err
			}
		}
		if !regexp.MustCompile(`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`).MatchString(operationID) {
			return errors.New("operation-id must be a UUIDv4")
		}
		mode := "password"
		if *signature {
			mode = "signature"
		}
		body, _ = json.Marshal(map[string]any{
			"operation_id": operationID, "repository": args[2], "token_env": *tokenEnv,
			"webhook_secret_env": *secretEnv, "webhook_mode": mode, "create_webhook": !*manual,
		})
		if err := persistOperation(getenv("MSBOT_OPERATION_DIR"), operationID, base, body); err != nil {
			return fmt.Errorf("cannot persist operation before dispatch: %w", err)
		}
		fmt.Fprintf(stderr, "operation_id=%s; query this ID if interrupted\n", operationID)
		method = http.MethodPost
		path = "/api/repos"
	} else if len(args) == 2 && args[1] == "list" && (args[0] == "repo" || args[0] == "task") {
		if args[0] == "repo" {
			path = "/api/repos"
		} else {
			path = "/api/tasks"
		}
	} else if len(args) == 3 && args[1] == "get" && (args[0] == "task" || args[0] == "operation") {
		if args[0] == "task" {
			path = "/api/tasks/" + url.PathEscape(args[2])
		} else {
			path = "/api/operations/" + url.PathEscape(args[2])
		}
	} else {
		return errors.New("unknown command")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, method, base+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Content-Type", "application/json")
	client := &http.Client{CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(request)
	if err != nil {
		return errors.New("request interrupted; query the original operation_id before submitting again")
	}
	defer response.Body.Close()
	content, err := io.ReadAll(io.LimitReader(response.Body, 1_048_577))
	if err != nil || len(content) > 1_048_576 || !json.Valid(content) {
		return errors.New("invalid or oversized management response; query the original operation_id")
	}
	var result any
	if err := json.Unmarshal(content, &result); err != nil {
		return err
	}
	encoder := json.NewEncoder(stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(result); err != nil {
		return err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("management request returned HTTP %d", response.StatusCode)
	}
	if method == http.MethodPost {
		var envelope struct {
			Data struct{ Status string }
		}
		if err := json.Unmarshal(content, &envelope); err != nil || envelope.Data.Status != "succeeded" {
			return fmt.Errorf("operation %s is not confirmed successful; query its result", operationID)
		}
	}
	return nil
}

func main() {
	if err := run(os.Args[1:], os.Getenv, os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}
