// claude-loadout picks a session's plugins, jev-hooks hooks, MCP servers and
// model, then starts Claude Code with them.
//
//	claude-loadout [claude args...]
//
// Plugins load at startup and no hook can change them in a running session, so
// the choice is made before `claude` starts:
//
//	plugins  --settings '{"enabledPlugins": {...}}'  (a flag setting beats user settings)
//	hooks    JEV_HOOKS_DISABLE in claude's environment, which every jev-hook honours
//	model    --model <alias>
//	MCP      nothing when every server stays on; otherwise --strict-mcp-config and a
//	         0600 file of the kept servers. Strict mode starts ONLY what is listed,
//	         and the claude.ai connectors cannot be listed, so turning any server off
//	         drops them for the session - the MCP page says so.
//
// A subcommand, -p, --resume/--continue or a non-terminal runs claude exactly
// as asked. Scripting and tests, no UI:
//
//	claude-loadout --dry-run --yes [--task T] [--pick-plugins a,b] [--pick-hooks h,..]
//	               [--pick-mcp x,y] [--pick-model m] [claude args...]
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/charmbracelet/huh"
	"golang.org/x/term"
)

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	own := map[string]string{}
	var rest []string
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch a {
		case "--dry-run", "--yes", "--loadout":
			own[a] = "1"
		case "--task", "--pick-plugins", "--pick-hooks", "--pick-mcp", "--pick-model":
			if i+1 < len(args) {
				own[a] = args[i+1]
				i++
			}
		default:
			rest = append(rest, a)
		}
	}
	real, err := exec.LookPath("claude")
	if err != nil {
		fmt.Fprintln(os.Stderr, "claude-loadout: claude not found on PATH")
		return 127
	}
	_, scripted := own["--yes"]
	tty := term.IsTerminal(int(os.Stdin.Fd())) && term.IsTerminal(int(os.Stdout.Fd()))
	if !scripted && (Passthrough(rest) || !tty) {
		return launch(append([]string{real}, rest...), nil, own)
	}

	home := os.Getenv("CLAUDE_LOADOUT_HOME")
	if home == "" {
		home, _ = os.UserHomeDir()
	}
	cwd, _ := os.Getwd()
	env := Env{Home: home, Cwd: cwd}
	plugins, servers, reg := env.Plugins(), env.MCPServers(), env.LoadRegistry()

	var c Choice
	if scripted {
		c = scriptedChoice(env, own, plugins, servers, reg)
	} else {
		c, err = Interview(env, plugins, servers, reg)
		if errors.Is(err, huh.ErrUserAborted) {
			fmt.Fprintln(os.Stderr, "claude-loadout: cancelled")
			return 130
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, "claude-loadout:", err)
			return 1
		}
	}
	if c.Model == env.DefaultModel() {
		c.Model = "default"
	}
	runtime := os.Getenv("XDG_RUNTIME_DIR")
	if runtime == "" {
		runtime = filepath.Join(home, ".cache")
	}
	argv, extra, err := Build(real, rest, plugins, servers, c, filepath.Join(runtime, "claude-loadout"))
	if err != nil {
		fmt.Fprintln(os.Stderr, "claude-loadout:", err)
		return 1
	}
	if _, dry := own["--dry-run"]; !dry {
		fmt.Fprintln(os.Stderr, "loadout:", describe(c, argv))
	}
	return launch(argv, extra, own)
}

func scriptedChoice(env Env, own map[string]string, plugins []Plugin, servers map[string]json.RawMessage, reg Registry) Choice {
	pick, add := Preselect(own["--task"], plugins, reg.Criteria, env.APIKey())
	var c Choice
	if v, ok := own["--pick-plugins"]; ok {
		c.Plugins = splitList(v)
	} else {
		for _, p := range plugins {
			if p.On || contains(add, p.ID) {
				c.Plugins = append(c.Plugins, p.ID)
			}
		}
	}
	hooks := reg.Hooks
	if v, ok := own["--pick-hooks"]; ok && hasJevHooks(c.Plugins) {
		kept := splitList(v)
		for _, h := range hooks {
			if !contains(kept, h) {
				c.DisabledHooks = append(c.DisabledHooks, h)
			}
		}
	}
	if v, ok := own["--pick-mcp"]; ok {
		c.MCP = splitList(v)
	} else {
		for n := range servers {
			c.MCP = append(c.MCP, n)
		}
		c.KeepConnectors = true
	}
	c.Model = own["--pick-model"]
	if c.Model == "" {
		c.Model = pick
	}
	if c.Model == "" {
		c.Model = env.DefaultModel()
	}
	return c
}

func splitList(s string) []string {
	var out []string
	for _, x := range strings.Split(s, ",") {
		if x = strings.TrimSpace(x); x != "" {
			out = append(out, x)
		}
	}
	return out
}

func describe(c Choice, argv []string) string {
	var ps []string
	for _, id := range c.Plugins {
		ps = append(ps, strings.SplitN(id, "@", 2)[0])
	}
	mcp := "all"
	if contains(argv, "--strict-mcp-config") {
		mcp = orNone(strings.Join(c.MCP, ", ")) + " (strict)"
	}
	hooks := ""
	if len(c.DisabledHooks) > 0 {
		hooks = " · hooks off: " + strings.Join(c.DisabledHooks, ", ")
	}
	return fmt.Sprintf("model %s · plugins %s%s · mcp %s", c.Model, orNone(strings.Join(ps, ", ")), hooks, mcp)
}

// launch execs claude, replacing this process. --dry-run prints what would run.
func launch(argv, extra []string, own map[string]string) int {
	if _, dry := own["--dry-run"]; dry {
		b, _ := json.Marshal(map[string]any{"argv": argv, "env": extra})
		fmt.Println(string(b))
		return 0
	}
	if err := syscall.Exec(argv[0], argv, append(os.Environ(), extra...)); err != nil {
		fmt.Fprintln(os.Stderr, "claude-loadout:", err)
		return 1
	}
	return 0
}
