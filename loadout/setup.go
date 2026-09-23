package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/charmbracelet/huh"
)

// runSetup installs the kit: the marketplace, the plugins a developer picks,
// the keys only they can supply, the MCP servers no plugin can carry, and the
// shell function. Everything already in place is shown as done and skipped, so
// re-running it fills gaps - which is also how someone picks up kit changes.
//
//	claude-loadout setup [--kit-repo owner/repo] [--dry-run] [--yes]
func runSetup(args []string) int {
	repo, dry, yes := DefaultKitRepo, false, false
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--dry-run":
			dry = true
		case "--yes":
			yes = true
		case "--kit-repo":
			if i+1 < len(args) {
				repo = args[i+1]
				i++
			}
		}
	}
	home := os.Getenv("CLAUDE_LOADOUT_HOME")
	if home == "" {
		home, _ = os.UserHomeDir()
	}
	cwd, _ := os.Getwd()
	e := Env{Home: home, Cwd: cwd}
	name := marketplaceName(repo)

	kit, ok := e.LoadKit(name)
	if !ok {
		// The kit lives in the marketplace clone, so the marketplace comes first.
		if dry {
			fmt.Printf("would add marketplace %s, then read its kit.json\n", repo)
			return 0
		}
		if !yes && !confirm(fmt.Sprintf("Add the %s marketplace (%s)?", name, repo)) {
			fmt.Fprintln(os.Stderr, "setup: cancelled")
			return 130
		}
		if err := runCmd("claude", "plugin", "marketplace", "add", repo); err != nil {
			fmt.Fprintf(os.Stderr, "setup: could not add the marketplace: %v\n"+
				"  You need access to %s, and git credentials Claude Code can use.\n", err, repo)
			return 1
		}
		if kit, ok = e.LoadKit(name); !ok {
			fmt.Fprintf(os.Stderr, "setup: %s has no kit.json at its root\n", repo)
			return 1
		}
	}

	rc := filepath.Join(home, ".bashrc")
	installed := e.installed()
	wantPlugins := []string{}
	for _, p := range kit.Plugins {
		if _, have := installed[p.ID]; have || p.Default {
			wantPlugins = append(wantPlugins, p.ID)
		}
	}
	servers := e.MCPServers()
	wantMCP := []string{}
	for _, s := range kit.MCPServers {
		if _, have := servers[s.Name]; have || s.Default {
			wantMCP = append(wantMCP, s.Name)
		}
	}
	secrets := map[string]string{}
	shell := ShellInstalled(rc)

	if !yes && !dry {
		var err error
		wantPlugins, wantMCP, secrets, shell, err = setupForm(e, kit, wantPlugins, wantMCP, shell)
		if err != nil {
			fmt.Fprintln(os.Stderr, "setup: cancelled")
			return 130
		}
	}

	plan := Plan(e, kit, repo, wantPlugins, wantMCP, secrets, shell || ShellInstalled(rc))
	if dry {
		b, _ := json.MarshalIndent(plan, "", "  ")
		fmt.Println(string(b))
		return 0
	}
	todo := Pending(plan)
	if len(todo) == 0 {
		fmt.Println("setup: everything in the kit is already in place.")
		return 0
	}
	fmt.Printf("setup: %d step(s) to run\n", len(todo))
	failed := 0
	for _, a := range todo {
		fmt.Printf("  %-12s %-28s ", a.Kind, a.Name)
		var err error
		switch a.Kind {
		case "env":
			err = WriteSettingsEnv(e, map[string]string{a.Name: secrets[a.Name]})
			if secrets[a.Name] == "" {
				err = fmt.Errorf("no value given; set it in ~/.claude/settings.json env")
			}
		case "mcp":
			err = addMCP(kit, a.Name, secrets, e.SettingsEnv())
		case "shell":
			if shell && ShellConflict(rc) {
				err = fmt.Errorf("%s already defines `claude` some other way; remove that first, then re-run setup", rc)
			} else if shell {
				err = InstallShell(rc)
			} else {
				fmt.Println("skipped")
				continue
			}
		default:
			err = runCmd(a.Cmd...)
		}
		if err != nil {
			failed++
			fmt.Printf("FAILED — %v\n", err)
			continue
		}
		fmt.Println("ok")
	}
	fmt.Println()
	for _, n := range kit.Notes {
		fmt.Println("note:", n)
	}
	if failed > 0 {
		fmt.Printf("setup: %d step(s) failed; fix those and run setup again.\n", failed)
		return 1
	}
	fmt.Println("setup: done. Restart Claude Code, then `claude --loadout` to pick a session's loadout.")
	return 0
}

// addMCP registers a server with the developer's own key, taking it from this
// run or from settings. The key goes in the header argument, so it never
// appears in the kit or in any repo.
func addMCP(kit Kit, name string, secrets, envKeys map[string]string) error {
	for _, s := range kit.MCPServers {
		if s.Name != name {
			continue
		}
		args := []string{"mcp", "add", "--scope", "user", "--transport", s.Transport, s.Name, s.URL}
		if s.Header != "" {
			v := secrets[s.Secret]
			if v == "" {
				v = envKeys[s.Secret]
			}
			if v == "" {
				return fmt.Errorf("%s needs %s, which is not set", s.Name, s.Secret)
			}
			args = append(args, "--header", s.Header+": "+v)
		}
		return runCmd(append([]string{"claude"}, args...)...)
	}
	return fmt.Errorf("%s is not in the kit", name)
}

func runCmd(args ...string) error {
	cmd := exec.Command(args[0], args[1:]...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		line := strings.TrimSpace(string(out))
		if i := strings.LastIndex(line, "\n"); i >= 0 {
			line = line[i+1:]
		}
		return fmt.Errorf("%s: %s", err, line)
	}
	return nil
}

func confirm(q string) bool {
	yes := true
	if huh.NewConfirm().Title(q).Value(&yes).Run() != nil {
		return false
	}
	return yes
}

// marketplaceName is the last path element of owner/repo, which is the name
// Claude Code gives the clone unless the catalog names itself otherwise.
func marketplaceName(repo string) string {
	parts := strings.Split(strings.TrimSuffix(repo, ".git"), "/")
	last := parts[len(parts)-1]
	if last == "claude-plugins" && len(parts) > 1 {
		return "instruxi" // this catalog names itself
	}
	return last
}

// setupForm asks what to install and collects the keys. Enter/Tab forward,
// Shift+Tab back, Space toggles.
func setupForm(e Env, kit Kit, wantPlugins, wantMCP []string, shell bool) ([]string, []string, map[string]string, bool, error) {
	installed := e.installed()
	haveEnv := e.SettingsEnv()
	secrets := map[string]string{}
	nameW := 4
	for _, p := range kit.Plugins {
		if n := len(strings.SplitN(p.ID, "@", 2)[0]); n > nameW {
			nameW = n
		}
	}
	var popts []huh.Option[string]
	for _, p := range kit.Plugins {
		short := strings.SplitN(p.ID, "@", 2)[0]
		d := p.Summary
		if _, have := installed[p.ID]; have {
			d = "[installed] " + d
		}
		popts = append(popts, huh.NewOption(label(short, nameW, d), p.ID).Selected(contains(wantPlugins, p.ID)))
	}
	var mopts []huh.Option[string]
	servers := e.MCPServers()
	for _, s := range kit.MCPServers {
		d := s.Summary
		if _, have := servers[s.Name]; have {
			d = "[configured] " + d
		}
		mopts = append(mopts, huh.NewOption(label(s.Name, nameW, d), s.Name).Selected(contains(wantMCP, s.Name)))
	}
	groups := []*huh.Group{
		huh.NewGroup(huh.NewMultiSelect[string]().Title("Plugins to install").
			Description("Enter/Tab: next · Shift+Tab: back · Space: toggle").
			Options(popts...).Value(&wantPlugins).Height(len(popts) + 2)),
	}
	if len(mopts) > 0 {
		groups = append(groups, huh.NewGroup(huh.NewMultiSelect[string]().Title("MCP servers").
			Description("Each uses your own key, asked for next.").
			Options(mopts...).Value(&wantMCP).Height(len(mopts)+2)))
	}
	for _, v := range kit.Env {
		key, title, where, sensitive := v.Key, v.Title, v.Where, v.Sensitive
		if _, set := haveEnv[key]; set {
			continue // already in settings; never shown, never overwritten
		}
		val := ""
		secrets[key] = ""
		in := huh.NewInput().Title(title).Description(where + "\nLeave empty to skip; setup will tell you what is missing.").
			Value(&val)
		if sensitive {
			in = in.EchoMode(huh.EchoModePassword)
		}
		groups = append(groups, huh.NewGroup(in).WithHideFunc(func() bool { return !needsKey(kit, key, wantPlugins, wantMCP) }))
		defer func() { secrets[key] = val }() // read after the form returns
	}
	groups = append(groups, huh.NewGroup(huh.NewConfirm().
		Title("Add the `claude --loadout` shell function to ~/.bashrc?").Value(&shell)))
	if err := huh.NewForm(groups...).Run(); err != nil {
		return nil, nil, nil, false, err
	}
	return wantPlugins, wantMCP, secrets, shell, nil
}

// needsKey reports whether anything chosen actually needs this key.
func needsKey(kit Kit, key string, plugins, mcp []string) bool {
	for _, p := range kit.Plugins {
		if contains(plugins, p.ID) {
			for _, k := range p.Needs {
				if k == key {
					return true
				}
			}
		}
	}
	for _, s := range kit.MCPServers {
		if contains(mcp, s.Name) && s.Secret == key {
			return true
		}
	}
	return false
}
