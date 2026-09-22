package main

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/charmbracelet/huh"
	"golang.org/x/term"
)

// width is the usable line width for option labels.
func width() int {
	w, _, err := term.GetSize(int(os.Stdout.Fd()))
	if err != nil || w <= 0 {
		w = 80
	}
	return w
}

func fit(s string, n int) string {
	s = strings.Join(strings.Fields(s), " ")
	if n < 4 {
		n = 4
	}
	if len(s) <= n {
		return s
	}
	return s[:n-3] + "..."
}

// label lays out "name  description" within the terminal: huh prints each
// option on one line, so a long description is cut, never wrapped off-screen.
func label(name string, nameW int, desc string) string {
	room := width() - nameW - 12 // cursor, checkbox, gaps, margins
	if desc == "" {
		return name
	}
	return fmt.Sprintf("%-*s  %s", nameW, name, fit(desc, room))
}

// Interview runs the multi-page form. Enter/Tab moves forward, Shift+Tab
// back, on every page; Ctrl+C cancels without starting anything.
func Interview(env Env, plugins []Plugin, servers map[string]json.RawMessage, reg Registry) (Choice, error) {
	var (
		task       string
		selPlugins []string
		selHooks   []string
		selMCP     []string
		model      string
		start      = true
	)
	key := env.APIKey()
	base := env.DefaultModel()

	type pre struct {
		model string
		add   []string
	}
	cache := map[string]pre{}
	preselect := func() pre {
		if p, ok := cache[task]; ok {
			return p
		}
		m, a := Preselect(task, plugins, reg.Criteria, key)
		cache[task] = pre{m, a}
		return cache[task]
	}

	nameW := 4
	for _, p := range plugins {
		if len(p.Name) > nameW {
			nameW = len(p.Name)
		}
	}
	pluginOptions := func() []huh.Option[string] {
		p := preselect()
		var opts []huh.Option[string]
		for _, pl := range plugins {
			desc := pl.Desc
			if contains(p.add, pl.ID) {
				desc = "[Jev: this task needs it] " + desc
			}
			opts = append(opts, huh.NewOption(label(pl.Name, nameW, desc), pl.ID).
				Selected(pl.On || contains(p.add, pl.ID)))
		}
		return opts
	}

	hookW := 4
	for _, h := range reg.Hooks {
		if len(h) > hookW {
			hookW = len(h)
		}
	}
	var hookOptions []huh.Option[string]
	for _, h := range reg.Hooks {
		// The score leads, so the page reads best-first at a glance.
		name := reg.Badge(h) + "  " + h
		hookOptions = append(hookOptions, huh.NewOption(label(name, hookW+4, reg.HookDesc[h]), h).Selected(true))
	}

	names := make([]string, 0, len(servers))
	for n := range servers {
		names = append(names, n)
	}
	sort.Strings(names)
	serverW := len(Connectors)
	for _, n := range names {
		if len(n) > serverW {
			serverW = len(n)
		}
	}
	mcpOptions := []huh.Option[string]{huh.NewOption(label(Connectors, serverW,
		"Gmail, Drive, Calendar, Docs... (your claude.ai account)"), Connectors).Selected(true)}
	for _, n := range names {
		mcpOptions = append(mcpOptions, huh.NewOption(label(n, serverW, ServerSummary(servers[n])), n).Selected(true))
	}

	modelOptions := func() []huh.Option[string] {
		pick := preselect().model
		want := base
		if pick != "" {
			want = pick
		}
		blurb := func(m string) string {
			c := reg.Criteria[strings.SplitN(m, "[", 2)[0]]
			return strings.SplitN(c, ":", 2)[0]
		}
		opts := []huh.Option[string]{huh.NewOption(label(base, 9, "your settings default"), base).Selected(want == base)}
		for _, m := range Models {
			if m == base {
				continue
			}
			d := blurb(m)
			if m == pick {
				d = "[Jev] " + d
			}
			opts = append(opts, huh.NewOption(label(m, 9, d), m).Selected(m == want))
		}
		return opts
	}

	summary := func() string {
		var b strings.Builder
		fmt.Fprintf(&b, "Model:   %s\n", model)
		var ps []string
		for _, id := range selPlugins {
			ps = append(ps, strings.SplitN(id, "@", 2)[0])
		}
		fmt.Fprintf(&b, "Plugins: %s\n", orNone(strings.Join(ps, ", ")))
		if hasJevHooks(selPlugins) && reg.Available {
			var off []string
			for _, h := range reg.Hooks {
				if !contains(selHooks, h) {
					off = append(off, h)
				}
			}
			fmt.Fprintf(&b, "Hooks off: %s\n", orNone(strings.Join(off, ", ")))
		}
		local := []string{}
		for _, n := range selMCP {
			if n != Connectors {
				local = append(local, n)
			}
		}
		if contains(selMCP, Connectors) && len(local) == len(names) {
			b.WriteString("MCP:     all servers, as normal")
		} else {
			fmt.Fprintf(&b, "MCP:     %s  (strict mode: claude.ai connectors OFF)", orNone(strings.Join(local, ", ")))
		}
		return b.String()
	}

	help := "Enter/Tab: next  ·  Shift+Tab: back  ·  Space: toggle  ·  Ctrl+C: cancel"
	hookHelp := help
	if reg.RankedAsOf != "" {
		hookHelp = "Ranked by observed usefulness (" + reg.RankedAsOf + "): +2 valuable · +1 useful · 0 mixed · -1 costly · ? no data\n" + help
	}
	form := huh.NewForm(
		huh.NewGroup(
			huh.NewInput().Title("What are you working on?").
				Description("Optional. Jev pre-selects the model and any plugin the task needs.\n"+help).
				Value(&task),
		),
		huh.NewGroup(
			huh.NewMultiSelect[string]().Title("Plugins").Description(help).
				OptionsFunc(pluginOptions, &task).Value(&selPlugins).Height(len(plugins)+2),
		),
		huh.NewGroup(
			huh.NewMultiSelect[string]().Title("jev-hooks: which hooks run").Description(hookHelp).
				Options(hookOptions...).Value(&selHooks).Height(len(hookOptions)+2),
		).WithHideFunc(func() bool { return !reg.Available || !hasJevHooks(selPlugins) }),
		huh.NewGroup(
			huh.NewMultiSelect[string]().Title("MCP servers").
				Description("Turning ANY off starts strict mode, which also drops the claude.ai connectors.\n"+help).
				Options(mcpOptions...).Value(&selMCP).Height(len(mcpOptions)+2),
		),
		huh.NewGroup(
			huh.NewSelect[string]().Title("Model").Description(help).
				OptionsFunc(modelOptions, &task).Value(&model),
		),
		huh.NewGroup(
			huh.NewConfirm().Title("Start Claude with this loadout?").
				DescriptionFunc(summary, []any{&task, &selPlugins, &selHooks, &selMCP, &model}).
				Affirmative("Start").Negative("Cancel").Value(&start),
		),
	)
	if err := form.Run(); err != nil {
		return Choice{}, err
	}
	if !start {
		return Choice{}, huh.ErrUserAborted
	}
	c := Choice{Plugins: selPlugins, Model: model}
	if hasJevHooks(selPlugins) && reg.Available {
		for _, h := range reg.Hooks {
			if !contains(selHooks, h) {
				c.DisabledHooks = append(c.DisabledHooks, h)
			}
		}
	}
	for _, n := range selMCP {
		if n != Connectors {
			c.MCP = append(c.MCP, n)
		}
	}
	c.KeepConnectors = contains(selMCP, Connectors) && len(c.MCP) == len(names)
	return c, nil
}

func orNone(s string) string {
	if s == "" {
		return "none"
	}
	return s
}
