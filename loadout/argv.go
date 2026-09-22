package main

import (
	"encoding/json"
	"os"
	"strings"
	"time"
)

// Connectors is the checklist entry for the claude.ai connectors. They are
// proxied through the account and cannot be listed in an MCP config, so the
// only way to drop one is strict mode, which drops them all.
const Connectors = "claude.ai connectors"

// Choice is a finished loadout.
type Choice struct {
	Plugins        []string // plugin ids to enable
	DisabledHooks  []string // jev-hooks hook names to switch off
	MCP            []string // local server names to keep
	KeepConnectors bool
	Model          string // alias, or "default"
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

func sameSet(a []string, keys map[string]json.RawMessage) bool {
	if len(a) != len(keys) {
		return false
	}
	for _, x := range a {
		if _, ok := keys[x]; !ok {
			return false
		}
	}
	return true
}

// Build turns a Choice into the claude argv and the extra environment for it.
// Flags only where the choice differs from what a plain `claude` would do.
func Build(real string, rest []string, plugins []Plugin, servers map[string]json.RawMessage,
	c Choice, runtimeDir string) (argv []string, env []string, err error) {
	argv = []string{real}
	changed := false
	enabled := map[string]bool{}
	for _, p := range plugins {
		on := contains(c.Plugins, p.ID)
		enabled[p.ID] = on
		if on != p.On {
			changed = true
		}
	}
	if changed {
		b, _ := json.Marshal(map[string]any{"enabledPlugins": enabled})
		argv = append(argv, "--settings", string(b))
	}
	if c.Model != "" && c.Model != "default" {
		argv = append(argv, "--model", c.Model)
	}
	if !sameSet(c.MCP, servers) || !c.KeepConnectors {
		path, err := writeMCP(plugins, servers, c, runtimeDir)
		if err != nil {
			return nil, nil, err
		}
		argv = append(argv, "--strict-mcp-config", "--mcp-config", path)
	}
	argv = append(argv, rest...)
	if len(c.DisabledHooks) > 0 && contains(c.Plugins, JevHooksID) {
		// Hooks inherit claude's environment - verified in a live session -
		// and each jev-hook honours its own name in JEV_HOOKS_DISABLE.
		env = append(env, "JEV_HOOKS_DISABLE="+strings.Join(c.DisabledHooks, ","))
	}
	return argv, env, nil
}

// writeMCP writes the kept servers, plus each kept plugin's own servers, to a
// fresh 0600 file: server configs carry tokens and must never reach argv.
func writeMCP(plugins []Plugin, servers map[string]json.RawMessage, c Choice, dir string) (string, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	if entries, err := os.ReadDir(dir); err == nil { // a day is long enough for any session to have read its file
		for _, e := range entries {
			if info, err := e.Info(); err == nil && time.Since(info.ModTime()) > 24*time.Hour {
				os.Remove(dir + "/" + e.Name())
			}
		}
	}
	kept := map[string]json.RawMessage{}
	for _, p := range plugins {
		if contains(c.Plugins, p.ID) {
			for n, cfg := range PluginServers(p.Root) {
				kept[n] = cfg
			}
		}
	}
	for _, n := range c.MCP {
		if cfg, ok := servers[n]; ok {
			kept[n] = cfg
		}
	}
	f, err := os.CreateTemp(dir, "mcp-*.json") // CreateTemp is 0600
	if err != nil {
		return "", err
	}
	defer f.Close()
	b, _ := json.Marshal(map[string]any{"mcpServers": kept})
	_, err = f.Write(b)
	return f.Name(), err
}

// Passthrough flags and subcommands: not an interactive session start.
var passFlags = map[string]bool{"-p": true, "--print": true, "-r": true, "--resume": true, "-c": true,
	"--continue": true, "-h": true, "--help": true, "-v": true, "--version": true, "--settings": true,
	"--model": true, "--mcp-config": true, "--strict-mcp-config": true, "--output-format": true,
	"--input-format": true, "--from-pr": true, "--teleport": true}
var passWords = map[string]bool{"plugin": true, "plugins": true, "mcp": true, "config": true, "update": true,
	"doctor": true, "install": true, "migrate-installer": true, "setup-token": true, "auth": true,
	"login": true, "logout": true, "agents": true, "api-key": true, "help": true}

// Passthrough reports whether these claude args mean "not an interactive
// session start", in which case claude runs exactly as asked.
func Passthrough(rest []string) bool {
	if len(rest) > 0 && passWords[rest[0]] {
		return true
	}
	for _, a := range rest {
		if passFlags[strings.SplitN(a, "=", 2)[0]] {
			return true
		}
	}
	return false
}
