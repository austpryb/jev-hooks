package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fixture builds a home with two plugins (jev-hooks on, "b" off and declaring
// an MCP server), user settings, and two local MCP servers - one carrying a
// token that must never reach argv.
func fixture(t *testing.T) Env {
	t.Helper()
	h := t.TempDir()
	w := func(rel, body string) {
		p := filepath.Join(h, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(strings.ReplaceAll(body, "HOME", h)), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	w("jh/.claude-plugin/plugin.json", `{"description":"safety hooks"}`)
	w("jh/lib/registry.json", `{"hooks":{"bash_gate":"asks before risky commands","stop_check":"stops empty promises","triage":"keeps decisions"},
	  "criteria":{"opus":"implementation: code","fable":"planning: design","sonnet":"mechanical: search","haiku":"lookup: small"}}`)
	w("pb/.claude-plugin/plugin.json", `{"description":"browser debugging","mcpServers":{"bsrv":{"command":"${CLAUDE_PLUGIN_ROOT}/run.sh"}}}`)
	w(".claude/plugins/installed_plugins.json", `{"plugins":{"jev-hooks@jev-hooks":[{"installPath":"HOME/jh"}],"b@m":[{"installPath":"HOME/pb"}]}}`)
	w(".claude/settings.json", `{"model":"opus","enabledPlugins":{"jev-hooks@jev-hooks":true,"b@m":false},"env":{"TYPESAFE_API_KEY":"k-from-settings"}}`)
	w(".claude.json", `{"mcpServers":{"s1":{"type":"http","url":"https://x/mcp","headers":{"Authorization":"Bearer SECRET-TOKEN"}},"s2":{"command":"s2"}}}`)
	return Env{Home: h, Cwd: h}
}

func all(servers map[string]json.RawMessage) []string {
	var out []string
	for n := range servers {
		out = append(out, n)
	}
	return out
}

func TestDefaultsAddNoFlags(t *testing.T) {
	e := fixture(t)
	p, s := e.Plugins(), e.MCPServers()
	argv, env, err := Build("claude", nil, p, s, Choice{Plugins: []string{JevHooksID}, MCP: all(s), KeepConnectors: true, Model: "default"}, t.TempDir())
	if err != nil || len(argv) != 1 || len(env) != 0 {
		t.Fatalf("defaults should start a bare claude: %v %v %v", argv, env, err)
	}
}

func TestUntickedPluginIsDisabled(t *testing.T) {
	e := fixture(t)
	p, s := e.Plugins(), e.MCPServers()
	argv, _, _ := Build("claude", nil, p, s, Choice{MCP: all(s), KeepConnectors: true, Model: "default"}, t.TempDir())
	if len(argv) < 3 || argv[1] != "--settings" || !strings.Contains(argv[2], `"jev-hooks@jev-hooks":false`) {
		t.Fatalf("unticking jev-hooks should disable it: %v", argv)
	}
}

func TestModelFlag(t *testing.T) {
	e := fixture(t)
	p, s := e.Plugins(), e.MCPServers()
	argv, _, _ := Build("claude", []string{"fix it"}, p, s, Choice{Plugins: []string{JevHooksID}, MCP: all(s), KeepConnectors: true, Model: "fable"}, t.TempDir())
	if strings.Join(argv, " ") != "claude --model fable fix it" {
		t.Fatalf("got %v", argv)
	}
}

func TestStrictModeFileKeepsChosenAndPluginServersPrivately(t *testing.T) {
	e := fixture(t)
	p, s := e.Plugins(), e.MCPServers()
	argv, _, err := Build("claude", nil, p, s, Choice{Plugins: []string{JevHooksID, "b@m"}, MCP: []string{"s1"}, Model: "default"}, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	joined := strings.Join(argv, " ")
	if !strings.Contains(joined, "--strict-mcp-config --mcp-config ") {
		t.Fatalf("dropping a server should start strict mode: %v", argv)
	}
	if strings.Contains(joined, "SECRET-TOKEN") {
		t.Fatal("a server token reached the command line")
	}
	path := argv[len(argv)-1]
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("mcp file mode %v, want 0600", info.Mode().Perm())
	}
	var f struct {
		MCPServers map[string]struct{ Command string } `json:"mcpServers"`
	}
	b, _ := os.ReadFile(path)
	json.Unmarshal(b, &f)
	if _, ok := f.MCPServers["s2"]; ok {
		t.Fatal("s2 was dropped but is in the file")
	}
	if _, ok := f.MCPServers["s1"]; !ok {
		t.Fatal("s1 was kept but is missing")
	}
	if f.MCPServers["bsrv"].Command != filepath.Join(e.Home, "pb")+"/run.sh" {
		t.Fatalf("plugin server not copied with its root resolved: %+v", f.MCPServers["bsrv"])
	}
}

func TestDisabledHooksReachEnvOnlyWithJevHooks(t *testing.T) {
	e := fixture(t)
	p, s := e.Plugins(), e.MCPServers()
	_, env, _ := Build("claude", nil, p, s, Choice{Plugins: []string{JevHooksID}, DisabledHooks: []string{"stop_check", "triage"}, MCP: all(s), KeepConnectors: true}, t.TempDir())
	if len(env) != 1 || env[0] != "JEV_HOOKS_DISABLE=stop_check,triage" {
		t.Fatalf("got %v", env)
	}
	_, env, _ = Build("claude", nil, p, s, Choice{DisabledHooks: []string{"stop_check"}, MCP: all(s), KeepConnectors: true}, t.TempDir())
	if len(env) != 0 {
		t.Fatalf("no jev-hooks, no switches: %v", env)
	}
}

func TestRegistryKeepsHookOrderAndKeyFallsBackToSettings(t *testing.T) {
	e := fixture(t)
	r := e.LoadRegistry()
	if strings.Join(r.Hooks, ",") != "bash_gate,stop_check,triage" || !r.Available {
		t.Fatalf("hooks out of file order: %v", r.Hooks)
	}
	t.Setenv("TYPESAFE_API_KEY", "")
	if e.APIKey() != "k-from-settings" {
		t.Fatal("the key lives in Claude's settings, where a shell never sees it")
	}
}

func TestPassthrough(t *testing.T) {
	for _, a := range [][]string{{"-p", "hi"}, {"--resume"}, {"plugin", "list"}, {"--model=opus"}} {
		if !Passthrough(a) {
			t.Errorf("%v should pass through", a)
		}
	}
	for _, a := range [][]string{nil, {"fix the bug"}, {"--verbose"}} {
		if Passthrough(a) {
			t.Errorf("%v should be interviewed", a)
		}
	}
}

func TestPreselectOnlyAddsAndPicksModel(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer k" {
			w.WriteHeader(401)
			return
		}
		json.NewDecoder(r.Body).Decode(&got)
		w.Write([]byte(`{"answers":{"model":{"choice":"fable","probabilities":{"fable":0.8,"opus":0.2}},"p0":{"noul":0.9}}}`))
	}))
	defer srv.Close()
	t.Setenv("TYPESAFE_BASE_URL", srv.URL)
	e := fixture(t)
	r := e.LoadRegistry()
	model, add := Preselect("debug the page in the browser", e.Plugins(), r.Criteria, "k")
	if model != "fable" || len(add) != 1 || add[0] != "b@m" {
		t.Fatalf("got model=%q add=%v", model, add)
	}
	qs := got["questions"].(map[string]any)
	if len(qs) != 2 {
		t.Fatalf("only normally-OFF plugins are asked about (plus the model): %v", qs)
	}
	if m, a := Preselect("", e.Plugins(), r.Criteria, "k"); m != "" || a != nil {
		t.Fatal("no task, no call")
	}
	if m, a := Preselect("x", e.Plugins(), r.Criteria, ""); m != "" || a != nil {
		t.Fatal("no key, no call")
	}
}

func TestFitNeverOverflows(t *testing.T) {
	long := strings.Repeat("word ", 60)
	if got := fit(long, 40); len(got) != 40 || !strings.HasSuffix(got, "...") {
		t.Fatalf("got %d chars: %q", len(got), got)
	}
}

func TestRankingOrdersTheHooksPageBestFirst(t *testing.T) {
	e := fixture(t)
	root := filepath.Join(e.Home, "jh", "lib", "registry.json")
	body := `{"hooks":{"bash_gate":"a","stop_check":"b","triage":"c","new_hook":"d"},
	  "ranking":{"as_of":"2026-09-22","order":[
	    {"hook":"triage","score":null,"why":"no data"},
	    {"hook":"gone_hook","score":2,"why":"removed from the plugin"},
	    {"hook":"stop_check","score":-1,"why":"noisy"},
	    {"hook":"bash_gate","score":2,"why":"valuable"}]}}`
	if err := os.WriteFile(root, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	r := e.LoadRegistry()
	// Ranked hooks in ranking order; a stale entry for a hook the plugin no
	// longer has is dropped; a new hook the ranking does not name yet is kept,
	// after them, rather than hidden by a stale ranking.
	if got := strings.Join(r.Hooks, ","); got != "triage,stop_check,bash_gate,new_hook" {
		t.Fatalf("hooks page order = %s", got)
	}
	if r.RankedAsOf != "2026-09-22" {
		t.Fatalf("as_of = %q", r.RankedAsOf)
	}
	for h, want := range map[string]string{"bash_gate": "+2", "stop_check": "-1", "triage": " ?", "new_hook": "  "} {
		if got := r.Badge(h); got != want {
			t.Fatalf("Badge(%s) = %q, want %q", h, got, want)
		}
	}
}

func TestShippedRegistryRanksEveryHook(t *testing.T) {
	// The registry this repo ships: every hook it declares is ranked, so the
	// hooks page never shows an unscored hook by accident.
	b, err := os.ReadFile(filepath.Join("..", "lib", "registry.json"))
	if err != nil {
		t.Fatal(err)
	}
	e := Env{Home: t.TempDir()}
	dir := filepath.Join(e.Home, "jh", "lib")
	os.MkdirAll(dir, 0o755)
	os.WriteFile(filepath.Join(dir, "registry.json"), b, 0o644)
	os.MkdirAll(filepath.Join(e.Home, ".claude", "plugins"), 0o755)
	os.WriteFile(filepath.Join(e.Home, ".claude", "plugins", "installed_plugins.json"),
		[]byte(`{"plugins":{"jev-hooks@jev-hooks":[{"installPath":"`+filepath.Join(e.Home, "jh")+`"}]}}`), 0o644)
	r := e.LoadRegistry()
	if len(r.Hooks) == 0 {
		t.Fatal("shipped registry has no hooks")
	}
	for _, h := range r.Hooks {
		if _, ok := r.Rank[h]; !ok {
			t.Fatalf("hook %s is not ranked in lib/registry.json", h)
		}
	}
}

// The same plugin from another marketplace. The instruxi kit lists jev-hooks
// as jev-hooks@instruxi; an exact-id check found neither its registry (so the
// hooks page vanished) nor its switches (so JEV_HOOKS_DISABLE was dropped).
func TestJevHooksFromAnotherMarketplace(t *testing.T) {
	e := fixture(t)
	ip := filepath.Join(e.Home, ".claude", "plugins", "installed_plugins.json")
	b, _ := os.ReadFile(ip)
	if err := os.WriteFile(ip, []byte(strings.ReplaceAll(string(b), "jev-hooks@jev-hooks", "jev-hooks@instruxi")), 0o644); err != nil {
		t.Fatal(err)
	}
	if r := e.LoadRegistry(); !r.Available || len(r.Hooks) != 3 {
		t.Fatalf("registry not found for jev-hooks@instruxi: %+v", r)
	}
	p, s := e.Plugins(), e.MCPServers()
	_, env, _ := Build("claude", nil, p, s, Choice{Plugins: []string{"jev-hooks@instruxi"},
		DisabledHooks: []string{"stop_check"}, MCP: all(s), KeepConnectors: true}, t.TempDir())
	if len(env) != 1 || env[0] != "JEV_HOOKS_DISABLE=stop_check" {
		t.Fatalf("hook switches lost for jev-hooks@instruxi: %v", env)
	}
}
