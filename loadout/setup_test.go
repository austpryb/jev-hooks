package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const testKit = `{"name":"instruxi","marketplace":"o/claude-plugins",
 "plugins":[{"id":"jev-hooks@instruxi","default":true,"needs":["TYPESAFE_API_KEY"],"summary":"hooks"},
            {"id":"enforcer-graph@instruxi","default":false,"needs":["GRAPH_API_KEY"],"summary":"planner"}],
 "env":[{"key":"TYPESAFE_API_KEY","title":"Jev key","where":"console","sensitive":true},
        {"key":"GRAPH_API_KEY","title":"graph key","where":"an admin","sensitive":true}],
 "mcpServers":[{"name":"enforcer-graph","default":false,"transport":"http","url":"https://x/mcp",
                "header":"X-API-Key","secret":"GRAPH_API_KEY","summary":"graph tools"}],
 "notes":["restart after"]}`

// kitFixture: a home where the marketplace is already added (its clone holds
// kit.json), jev-hooks is installed, and the Jev key is already in settings.
func kitFixture(t *testing.T) (Env, Kit) {
	t.Helper()
	e := fixture(t)
	d := filepath.Join(e.Home, ".claude", "plugins", "marketplaces", "instruxi")
	if err := os.MkdirAll(d, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(d, "kit.json"), []byte(testKit), 0o644); err != nil {
		t.Fatal(err)
	}
	kit, ok := e.LoadKit("instruxi")
	if !ok {
		t.Fatal("kit.json not read from the marketplace clone")
	}
	return e, kit
}

func kinds(plan []Action) map[string]bool {
	m := map[string]bool{}
	for _, a := range plan {
		m[a.Kind+":"+a.Name] = a.Done
	}
	return m
}

func TestPlanMarksWhatIsAlreadyThere(t *testing.T) {
	e, kit := kitFixture(t)
	// fixture has jev-hooks@jev-hooks installed, not @instruxi, and no graph server.
	plan := Plan(e, kit, "o/claude-plugins", []string{"jev-hooks@instruxi", "enforcer-graph@instruxi"},
		[]string{"enforcer-graph"}, map[string]string{}, false)
	k := kinds(plan)
	if done, ok := k["marketplace:instruxi"]; !ok || !done {
		t.Error("the marketplace is present in the fixture; it should be done")
	}
	for _, want := range []string{"plugin:jev-hooks@instruxi", "plugin:enforcer-graph@instruxi",
		"env:TYPESAFE_API_KEY", "env:GRAPH_API_KEY", "mcp:enforcer-graph", "shell:claude --loadout"} {
		if _, ok := k[want]; !ok {
			t.Errorf("plan is missing %s: %v", want, k)
		}
	}
	if k["env:TYPESAFE_API_KEY"] != true {
		t.Error("the Jev key is already in the fixture's settings; it should be done")
	}
	if k["env:GRAPH_API_KEY"] != false || k["mcp:enforcer-graph"] != false {
		t.Error("the graph key and server are absent; they should be pending")
	}
	if n := len(Pending(plan)); n != 5 {
		t.Errorf("pending = %d, want 5 (2 plugins, the graph key, the server, the shell)", n)
	}
}

func TestPlanOnlyAsksForKeysTheChoiceNeeds(t *testing.T) {
	e, kit := kitFixture(t)
	plan := Plan(e, kit, "o/x", []string{"jev-hooks@instruxi"}, nil, map[string]string{}, true)
	for _, a := range plan {
		if a.Kind == "env" && a.Name == "GRAPH_API_KEY" {
			t.Fatal("asked for the graph key with neither the graph plugin nor its server chosen")
		}
		if a.Kind == "mcp" {
			t.Fatal("planned an MCP server that was not chosen")
		}
	}
}

func TestSettingsEnvMergeKeepsEverythingElse(t *testing.T) {
	e, _ := kitFixture(t)
	p := filepath.Join(e.Home, ".claude", "settings.json")
	before, _ := os.ReadFile(p)
	var pre map[string]any
	json.Unmarshal(before, &pre)
	if err := WriteSettingsEnv(e, map[string]string{"GRAPH_API_KEY": "g-secret"}); err != nil {
		t.Fatal(err)
	}
	var post map[string]any
	b, _ := os.ReadFile(p)
	if err := json.Unmarshal(b, &post); err != nil {
		t.Fatalf("settings.json is no longer valid JSON: %v", err)
	}
	for k := range pre {
		if _, ok := post[k]; !ok {
			t.Errorf("writing a key dropped the %q setting", k)
		}
	}
	env := post["env"].(map[string]any)
	if env["GRAPH_API_KEY"] != "g-secret" || env["TYPESAFE_API_KEY"] != "k-from-settings" {
		t.Errorf("env merge lost a key: %v", env)
	}
	if info, _ := os.Stat(p); info.Mode().Perm() != 0o600 {
		t.Errorf("settings.json holds keys; mode is %v, want 0600", info.Mode().Perm())
	}
}

func TestShellBlockIsIdempotent(t *testing.T) {
	rc := filepath.Join(t.TempDir(), ".bashrc")
	os.WriteFile(rc, []byte("export PATH=$PATH:/opt/bin\n"), 0o644)
	if ShellInstalled(rc) {
		t.Fatal("reported installed before anything was written")
	}
	for i := 0; i < 3; i++ {
		if err := InstallShell(rc); err != nil {
			t.Fatal(err)
		}
	}
	b, _ := os.ReadFile(rc)
	s := string(b)
	if n := strings.Count(s, shellMarkerStart); n != 1 {
		t.Errorf("block written %d times, want 1", n)
	}
	if !strings.Contains(s, "export PATH=$PATH:/opt/bin") {
		t.Error("rewriting the block dropped the rest of the file")
	}
	if !ShellInstalled(rc) {
		t.Error("not detected after install")
	}
}

func TestMarketplaceName(t *testing.T) {
	for repo, want := range map[string]string{
		"instruxi-io/claude-plugins": "instruxi", "acme/team-plugins": "team-plugins",
		"acme/team-plugins.git": "team-plugins"} {
		if got := marketplaceName(repo); got != want {
			t.Errorf("%s -> %s, want %s", repo, got, want)
		}
	}
}

func TestShellConflictWithAHandRolledFunction(t *testing.T) {
	rc := filepath.Join(t.TempDir(), ".bashrc")
	os.WriteFile(rc, []byte("claude() {\n  command claude \"$@\"\n}\n"), 0o644)
	if !ShellConflict(rc) {
		t.Fatal("an existing claude() must be reported, not appended beside")
	}
	if err := InstallShell(rc); err != nil {
		t.Fatal(err)
	}
	if ShellConflict(rc) {
		t.Error("once the marked block is in place, it is not a conflict")
	}
}

func TestYesModeStillWantsTheShellFunction(t *testing.T) {
	// The shell step is the one thing --yes used to skip: `shell` started as
	// "is it already installed", which is false exactly when it must be done.
	e, kit := kitFixture(t)
	plan := Plan(e, kit, "o/x", nil, nil, map[string]string{}, false)
	var shell *Action
	for i := range plan {
		if plan[i].Kind == "shell" {
			shell = &plan[i]
		}
	}
	if shell == nil || shell.Done {
		t.Fatalf("a shell step must be pending when it is not installed: %+v", shell)
	}
}
