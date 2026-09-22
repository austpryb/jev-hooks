package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// JevHooksID is jev-hooks from its own marketplace. The plugin is the same
// from any marketplace - the instruxi kit lists it as jev-hooks@instruxi - so
// code asks isJevHooks / hasJevHooks, never compares against this id.
const JevHooksID = "jev-hooks@jev-hooks"

// isJevHooks reports whether a plugin id is jev-hooks, from any marketplace.
func isJevHooks(id string) bool { return strings.HasPrefix(id, "jev-hooks@") }

// hasJevHooks reports whether jev-hooks is among these plugin ids.
func hasJevHooks(ids []string) bool {
	for _, id := range ids {
		if isJevHooks(id) {
			return true
		}
	}
	return false
}

// Plugin is one installed plugin and whether a plain `claude` here loads it.
type Plugin struct {
	ID, Name, Desc, Root string
	On                   bool
}

// Registry is lib/registry.json from the installed jev-hooks: the hook list and
// what each model is for. Read at runtime so the binary always matches the
// hooks actually installed.
type Registry struct {
	Hooks     []string // ranked order, best first; unranked hooks after, in file order
	HookDesc  map[string]string
	Criteria  map[string]string
	Available bool
	// Rank is each ranked hook's usefulness score (registry.json "ranking").
	// A hook absent here is unranked; one present with a nil Score is ranked
	// but not yet scorable.
	Rank map[string]Ranking
	// RankedAsOf is the date the ranking was last derived from data.
	RankedAsOf string
}

// Ranking is one hook's place in registry.json's "ranking".
type Ranking struct {
	Score *int   `json:"score"` // +2 .. -1; nil = not yet scorable
	Why   string `json:"why"`
}

// Env is where discovery looks: a home directory and a working directory.
type Env struct{ Home, Cwd string }

func loadJSON(path string, v any) bool {
	b, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	return json.Unmarshal(b, v) == nil
}

// ancestors is dir and every parent up to /, nearest first.
func ancestors(dir string) []string {
	var out []string
	d := filepath.Clean(dir)
	for {
		out = append(out, d)
		up := filepath.Dir(d)
		if up == d {
			return out
		}
		d = up
	}
}

func (e Env) claudeDir() string { return filepath.Join(e.Home, ".claude") }

type installedFile struct {
	Plugins map[string][]struct {
		InstallPath string `json:"installPath"`
	} `json:"plugins"`
}

func (e Env) installed() map[string]string {
	var f installedFile
	loadJSON(filepath.Join(e.claudeDir(), "plugins", "installed_plugins.json"), &f)
	out := map[string]string{}
	for id, versions := range f.Plugins {
		if len(versions) > 0 {
			out[id] = versions[0].InstallPath
		}
	}
	return out
}

type settingsFile struct {
	Model          string            `json:"model"`
	EnabledPlugins map[string]bool   `json:"enabledPlugins"`
	Env            map[string]string `json:"env"`
}

// Plugins lists every installed plugin with the state a plain `claude` in Cwd
// would give it: user settings, then project and local settings from the
// farthest ancestor to the nearest.
func (e Env) Plugins() []Plugin {
	var user settingsFile
	loadJSON(filepath.Join(e.claudeDir(), "settings.json"), &user)
	enabled := map[string]bool{}
	for k, v := range user.EnabledPlugins {
		enabled[k] = v
	}
	anc := ancestors(e.Cwd)
	for i := len(anc) - 1; i >= 0; i-- {
		if anc[i] == filepath.Clean(e.Home) {
			continue // ~/.claude/settings.json is the USER file, already read
		}
		for _, name := range []string{"settings.json", "settings.local.json"} {
			var s settingsFile
			if loadJSON(filepath.Join(anc[i], ".claude", name), &s) {
				for k, v := range s.EnabledPlugins {
					enabled[k] = v
				}
			}
		}
	}
	var out []Plugin
	for id, root := range e.installed() {
		var manifest struct {
			Description string `json:"description"`
		}
		loadJSON(filepath.Join(root, ".claude-plugin", "plugin.json"), &manifest)
		out = append(out, Plugin{ID: id, Name: strings.SplitN(id, "@", 2)[0], Desc: manifest.Description,
			Root: root, On: enabled[id]})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// MCPServers is what Claude Code would start here from local config: user
// scope in ~/.claude.json, that file's per-project entries for Cwd and its
// parents, and .mcp.json files. Plugin servers follow their plugin's toggle.
func (e Env) MCPServers() map[string]json.RawMessage {
	var cfg struct {
		MCPServers map[string]json.RawMessage `json:"mcpServers"`
		Projects   map[string]struct {
			MCPServers map[string]json.RawMessage `json:"mcpServers"`
		} `json:"projects"`
	}
	loadJSON(filepath.Join(e.Home, ".claude.json"), &cfg)
	out := map[string]json.RawMessage{}
	for k, v := range cfg.MCPServers {
		out[k] = v
	}
	anc := ancestors(e.Cwd)
	for i := len(anc) - 1; i >= 0; i-- {
		for k, v := range cfg.Projects[anc[i]].MCPServers {
			out[k] = v
		}
		var local struct {
			MCPServers map[string]json.RawMessage `json:"mcpServers"`
		}
		if loadJSON(filepath.Join(anc[i], ".mcp.json"), &local) {
			for k, v := range local.MCPServers {
				out[k] = v
			}
		}
	}
	return out
}

// ServerSummary is a server's url or command, for its checklist line.
func ServerSummary(raw json.RawMessage) string {
	var c struct{ URL, Command, Type string }
	json.Unmarshal(raw, &c)
	for _, s := range []string{c.URL, c.Command, c.Type} {
		if s != "" {
			return s
		}
	}
	return "-"
}

// PluginServers is the MCP servers a plugin declares - in
// .claude-plugin/plugin.json (inline, or a path to a JSON file) or a root
// .mcp.json - with ${CLAUDE_PLUGIN_ROOT} resolved. Strict mode starts only
// what its file lists, and that drops plugin servers too, so a kept plugin's
// servers are copied in.
func PluginServers(root string) map[string]json.RawMessage {
	if root == "" {
		return nil
	}
	var manifest struct {
		MCPServers json.RawMessage `json:"mcpServers"`
	}
	loadJSON(filepath.Join(root, ".claude-plugin", "plugin.json"), &manifest)
	decl := manifest.MCPServers
	var path string
	if json.Unmarshal(decl, &path) == nil && path != "" {
		b, _ := os.ReadFile(filepath.Join(root, path))
		var wrapped struct {
			MCPServers json.RawMessage `json:"mcpServers"`
		}
		if json.Unmarshal(b, &wrapped) == nil && len(wrapped.MCPServers) > 0 {
			decl = wrapped.MCPServers
		} else {
			decl = b
		}
	}
	if len(decl) == 0 || decl[0] != '{' {
		var dot struct {
			MCPServers json.RawMessage `json:"mcpServers"`
		}
		loadJSON(filepath.Join(root, ".mcp.json"), &dot)
		decl = dot.MCPServers
	}
	decl = bytes.ReplaceAll(decl, []byte("${CLAUDE_PLUGIN_ROOT}"), []byte(root))
	out := map[string]json.RawMessage{}
	json.Unmarshal(decl, &out)
	return out
}

// DefaultModel is the settings model, or "default".
func (e Env) DefaultModel() string {
	var s settingsFile
	loadJSON(filepath.Join(e.claudeDir(), "settings.json"), &s)
	if s.Model == "" {
		return "default"
	}
	return s.Model
}

// APIKey is TYPESAFE_API_KEY from the environment, else from Claude's user
// settings - where it usually lives, and where a shell never sees it.
func (e Env) APIKey() string {
	if k := os.Getenv("TYPESAFE_API_KEY"); k != "" {
		return k
	}
	var s settingsFile
	loadJSON(filepath.Join(e.claudeDir(), "settings.json"), &s)
	return s.Env["TYPESAFE_API_KEY"]
}

// LoadRegistry reads lib/registry.json from the installed jev-hooks.
func (e Env) LoadRegistry() Registry {
	r := Registry{HookDesc: map[string]string{}, Criteria: map[string]string{}}
	root := ""
	for id, path := range e.installed() {
		if isJevHooks(id) {
			root = path
			break
		}
	}
	b, err := os.ReadFile(filepath.Join(root, "lib", "registry.json"))
	if err != nil {
		return r
	}
	var f struct {
		Hooks    json.RawMessage   `json:"hooks"`
		Criteria map[string]string `json:"criteria"`
		Ranking  struct {
			AsOf  string `json:"as_of"`
			Order []struct {
				Hook string `json:"hook"`
				Ranking
			} `json:"order"`
		} `json:"ranking"`
	}
	if json.Unmarshal(b, &f) != nil {
		return r
	}
	json.Unmarshal(f.Hooks, &r.HookDesc)
	r.Criteria = f.Criteria
	r.RankedAsOf = f.Ranking.AsOf
	r.Rank = map[string]Ranking{}
	var ranked []string
	for _, o := range f.Ranking.Order {
		if _, known := r.HookDesc[o.Hook]; !known {
			continue // a ranking for a hook this plugin no longer has
		}
		if _, dup := r.Rank[o.Hook]; dup {
			continue
		}
		r.Rank[o.Hook] = o.Ranking
		ranked = append(ranked, o.Hook)
	}
	// Ranked hooks first, best first; any hook the ranking does not name yet (a
	// new one) keeps its file position after them, so a hook is never hidden
	// by a stale ranking.
	r.Hooks = ranked
	for _, h := range orderedKeys(f.Hooks) {
		if _, ok := r.Rank[h]; !ok {
			r.Hooks = append(r.Hooks, h)
		}
	}
	r.Available = len(r.Hooks) > 0
	return r
}

// Badge is a hook's score as the hooks page shows it: "+2", "+1", " 0", "-1",
// " ?" for ranked-but-unscorable, and blank for a hook the ranking omits.
func (r Registry) Badge(hook string) string {
	rk, ok := r.Rank[hook]
	switch {
	case !ok:
		return "  "
	case rk.Score == nil:
		return " ?"
	case *rk.Score > 0:
		return fmt.Sprintf("+%d", *rk.Score)
	case *rk.Score == 0:
		return " 0"
	default:
		return fmt.Sprintf("%d", *rk.Score)
	}
}

// orderedKeys returns a JSON object's keys in file order; a Go map loses it,
// and the hook list is meant to read in the order it was written.
func orderedKeys(raw json.RawMessage) []string {
	dec := json.NewDecoder(bytes.NewReader(raw))
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return nil
	}
	var keys []string
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return keys
		}
		if k, ok := t.(string); ok {
			keys = append(keys, k)
		}
		var skip json.RawMessage
		if dec.Decode(&skip) != nil {
			return keys
		}
	}
	return keys
}
