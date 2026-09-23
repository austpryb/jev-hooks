package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// DefaultKitRepo is the marketplace that carries the kit definition.
const DefaultKitRepo = "instruxi-io/claude-plugins"

// Kit is kit.json from the marketplace repo: which plugins a new developer
// gets, what each person supplies themselves, and the MCP servers no plugin
// can carry because they authenticate per developer.
type Kit struct {
	Name        string `json:"name"`
	Marketplace string `json:"marketplace"`
	Plugins     []struct {
		ID      string   `json:"id"`
		Default bool     `json:"default"`
		Needs   []string `json:"needs"`
		Summary string   `json:"summary"`
	} `json:"plugins"`
	Env []struct {
		Key       string `json:"key"`
		Title     string `json:"title"`
		Where     string `json:"where"`
		Sensitive bool   `json:"sensitive"`
	} `json:"env"`
	MCPServers []struct {
		Name      string `json:"name"`
		Default   bool   `json:"default"`
		Transport string `json:"transport"`
		URL       string `json:"url"`
		Header    string `json:"header"`
		Secret    string `json:"secret"`
		Summary   string `json:"summary"`
	} `json:"mcpServers"`
	Notes []string `json:"notes"`
}

// MarketplaceDir is where Claude Code clones a marketplace. The kit is read
// from that clone, so setup needs no credentials of its own.
func (e Env) MarketplaceDir(name string) string {
	return filepath.Join(e.claudeDir(), "plugins", "marketplaces", name)
}

// LoadKit reads kit.json from an added marketplace's clone. Returns false when
// the marketplace has not been added yet - the wizard adds it, then re-reads.
func (e Env) LoadKit(name string) (Kit, bool) {
	var k Kit
	b, err := os.ReadFile(filepath.Join(e.MarketplaceDir(name), "kit.json"))
	if err != nil || json.Unmarshal(b, &k) != nil || len(k.Plugins) == 0 {
		return k, false
	}
	return k, true
}

// SettingsEnv is the `env` block of the user's settings - where Claude Code
// keeps keys that hooks and tools read.
func (e Env) SettingsEnv() map[string]string {
	var s settingsFile
	loadJSON(filepath.Join(e.claudeDir(), "settings.json"), &s)
	if s.Env == nil {
		return map[string]string{}
	}
	return s.Env
}

// Action is one step of a setup plan. Done actions are shown as already
// satisfied and never run, so re-running setup only fills gaps.
type Action struct {
	Kind string // marketplace | plugin | env | mcp | shell
	Name string // what it acts on
	Why  string // one line for the screen
	Done bool
	Cmd  []string // the command to run, when it is one
}

// Plan is what setup would do, in order. Everything already satisfied is
// marked Done rather than left out, so a re-run reads as a checklist.
func Plan(e Env, kit Kit, repo string, wantPlugins, wantMCP []string, haveSecrets map[string]string, shellInstalled bool) []Action {
	var out []Action
	_, kitPresent := e.LoadKit(kit.Name)
	out = append(out, Action{Kind: "marketplace", Name: kit.Name, Done: kitPresent,
		Why: "the catalog every plugin below comes from",
		Cmd: []string{"claude", "plugin", "marketplace", "add", repo}})
	installed := e.installed()
	for _, p := range kit.Plugins {
		if !contains(wantPlugins, p.ID) {
			continue
		}
		_, have := installed[p.ID]
		out = append(out, Action{Kind: "plugin", Name: p.ID, Done: have, Why: p.Summary,
			Cmd: []string{"claude", "plugin", "install", p.ID, "-s", "user"}})
	}
	// Keys: only those a chosen plugin or server actually needs.
	needed := map[string]bool{}
	for _, p := range kit.Plugins {
		if contains(wantPlugins, p.ID) {
			for _, k := range p.Needs {
				needed[k] = true
			}
		}
	}
	for _, s := range kit.MCPServers {
		if contains(wantMCP, s.Name) && s.Secret != "" {
			needed[s.Secret] = true
		}
	}
	have := e.SettingsEnv()
	for _, v := range kit.Env {
		if !needed[v.Key] {
			continue
		}
		_, set := have[v.Key]
		if !set && haveSecrets[v.Key] != "" {
			set = false // supplied in this run: still an action, it must be written
		}
		out = append(out, Action{Kind: "env", Name: v.Key, Done: set, Why: v.Title + " — " + v.Where})
	}
	servers := e.MCPServers()
	for _, s := range kit.MCPServers {
		if !contains(wantMCP, s.Name) {
			continue
		}
		_, have := servers[s.Name]
		cmd := []string{"claude", "mcp", "add", "--scope", "user", "--transport", s.Transport, s.Name, s.URL}
		if s.Header != "" {
			cmd = append(cmd, "--header", s.Header+": <"+s.Secret+">")
		}
		out = append(out, Action{Kind: "mcp", Name: s.Name, Done: have, Why: s.Summary, Cmd: cmd})
	}
	out = append(out, Action{Kind: "shell", Name: "claude --loadout", Done: shellInstalled,
		Why: "a shell function so `claude --loadout` opens the loadout pages"})
	return out
}

// Pending is the actions a plan still has to run.
func Pending(plan []Action) []Action {
	var out []Action
	for _, a := range plan {
		if !a.Done {
			out = append(out, a)
		}
	}
	return out
}

// ShellFunction is the ~/.bashrc block, written between markers so a re-run
// replaces it instead of stacking copies.
const shellMarkerStart = "# >>> claude-loadout (instruxi kit) >>>"
const shellMarkerEnd = "# <<< claude-loadout (instruxi kit) <<<"

func ShellFunction() string {
	return strings.Join([]string{shellMarkerStart,
		"# `claude --loadout` picks this session's plugins, hooks, MCP servers and model.",
		"# A plain `claude` never goes through the launcher.",
		"claude() {",
		"  local a want= args=()",
		"  for a in \"$@\"; do if [ \"$a\" = --loadout ]; then want=1; else args+=(\"$a\"); fi; done",
		"  if [ -n \"$want\" ] && command -v claude-loadout >/dev/null; then claude-loadout \"${args[@]}\"; return; fi",
		"  command claude \"${args[@]}\"",
		"}", shellMarkerEnd, ""}, "\n")
}

// ShellConflict reports a `claude` function this block did not write - an
// earlier hand-rolled one, say. Appending beside it would leave two
// definitions in one file, where the last one silently wins.
func ShellConflict(rc string) bool {
	b, err := os.ReadFile(rc)
	if err != nil || strings.Contains(string(b), shellMarkerStart) {
		return false
	}
	for _, l := range strings.Split(string(b), "\n") {
		t := strings.TrimSpace(l)
		if strings.HasPrefix(t, "claude()") || strings.HasPrefix(t, "function claude") || strings.HasPrefix(t, "alias claude=") {
			return true
		}
	}
	return false
}

// ShellInstalled reports whether the block is already in the file.
func ShellInstalled(rc string) bool {
	b, err := os.ReadFile(rc)
	return err == nil && strings.Contains(string(b), shellMarkerStart)
}

// InstallShell appends or replaces the block. The rest of the file is kept.
func InstallShell(rc string) error {
	b, _ := os.ReadFile(rc)
	s := string(b)
	if i := strings.Index(s, shellMarkerStart); i >= 0 {
		j := strings.Index(s[i:], shellMarkerEnd)
		if j < 0 {
			return fmt.Errorf("%s: the start marker has no matching end marker; fix it by hand", rc)
		}
		s = s[:i] + ShellFunction() + s[i+j+len(shellMarkerEnd)+1:]
	} else {
		if s != "" && !strings.HasSuffix(s, "\n") {
			s += "\n"
		}
		s += "\n" + ShellFunction()
	}
	return os.WriteFile(rc, []byte(s), 0o644)
}

// WriteSettingsEnv merges keys into the user's settings `env`, keeping every
// other setting byte-for-byte where possible: the file is the user's, and a
// rewrite that drops an unknown field would be a silent loss.
func WriteSettingsEnv(e Env, add map[string]string) error {
	if len(add) == 0 {
		return nil
	}
	p := filepath.Join(e.claudeDir(), "settings.json")
	var doc map[string]json.RawMessage
	if b, err := os.ReadFile(p); err == nil {
		json.Unmarshal(b, &doc)
	}
	if doc == nil {
		doc = map[string]json.RawMessage{}
	}
	env := map[string]string{}
	if raw, ok := doc["env"]; ok {
		json.Unmarshal(raw, &env)
	}
	for k, v := range add {
		env[k] = v
	}
	raw, err := json.Marshal(env)
	if err != nil {
		return err
	}
	doc["env"] = raw
	keys := make([]string, 0, len(doc))
	for k := range doc {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	b.WriteString("{\n")
	for i, k := range keys {
		kb, _ := json.Marshal(k)
		vb, _ := json.MarshalIndent(json.RawMessage(doc[k]), "  ", "  ")
		b.WriteString("  " + string(kb) + ": " + string(vb))
		if i < len(keys)-1 {
			b.WriteString(",")
		}
		b.WriteString("\n")
	}
	b.WriteString("}\n")
	if err := os.WriteFile(p, []byte(b.String()), 0o600); err != nil {
		return err
	}
	// WriteFile leaves an existing file's mode alone, and this one now holds
	// API keys. Tighten it - a world-readable key file is the kind of thing
	// nobody notices until it matters.
	return os.Chmod(p, 0o600)
}
