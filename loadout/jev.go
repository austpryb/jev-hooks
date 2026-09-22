package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"
)

// Models offered for the session, after the settings default.
var Models = []string{"opus", "opus[1m]", "fable", "sonnet", "haiku"}

var judgedModels = []string{"opus", "fable", "sonnet", "haiku"}

// Preselect asks Jev, in one bounded call, which model suits the task and
// which normally-off plugins it clearly needs. It never proposes turning a
// plugin OFF: a safety hook should not vanish because a task description did
// not mention it. Any failure - no key, a timeout, a bad answer - returns
// nothing, and the defaults stand.
func Preselect(task string, plugins []Plugin, criteria map[string]string, key string) (model string, add []string) {
	if strings.TrimSpace(task) == "" || key == "" || len(criteria) == 0 {
		return "", nil
	}
	modelCrit := map[string]string{}
	for _, m := range judgedModels {
		if c, ok := criteria[m]; ok {
			modelCrit[m] = c
		}
	}
	questions := map[string]any{"model": map[string]any{"type": "choice",
		"instructions": "Which model should do `task`? Judge the WORK it asks for, not how long the text is.",
		"criteria":     modelCrit}}
	var off []Plugin
	for _, p := range plugins {
		if !p.On {
			off = append(off, p)
		}
	}
	for i, p := range off {
		desc := p.Desc
		if len(desc) > 300 {
			desc = desc[:300]
		}
		questions[fmt.Sprintf("p%d", i)] = map[string]any{"type": "noul",
			"instructions": fmt.Sprintf("Would `task` clearly USE the plugin `%s`: %s?", p.ID, desc),
			"criteria": map[string]string{"true": "the task is squarely what this plugin exists for",
				"false": "the task is unrelated, or would only possibly touch it"}}
	}
	if len(task) > 2000 {
		task = task[:2000]
	}
	body, _ := json.Marshal(map[string]any{"state": map[string]string{"task": task}, "model": jevModel(),
		"questions": questions})
	req, err := http.NewRequest("POST", jevBase()+"/v1/systemone", bytes.NewReader(body))
	if err != nil {
		return "", nil
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Timeout: 4 * time.Second}).Do(req)
	if err != nil {
		return "", nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return "", nil
	}
	var out struct {
		Answers map[string]struct {
			Choice        string             `json:"choice"`
			Probabilities map[string]float64 `json:"probabilities"`
			Noul          float64            `json:"noul"`
		} `json:"answers"`
	}
	if json.NewDecoder(resp.Body).Decode(&out) != nil {
		return "", nil
	}
	if a, ok := out.Answers["model"]; ok {
		model = topPick(a.Probabilities, a.Choice, 0.5)
	}
	for i, p := range off {
		if out.Answers[fmt.Sprintf("p%d", i)].Noul >= 0.7 {
			add = append(add, p.ID)
		}
	}
	return model, add
}

// topPick is the most probable model when it clears floor; "" otherwise.
func topPick(probs map[string]float64, choice string, floor float64) string {
	if len(probs) == 0 {
		if contains(judgedModels, choice) {
			return choice
		}
		return ""
	}
	type kv struct {
		k string
		v float64
	}
	var ranked []kv
	for k, v := range probs {
		if contains(judgedModels, k) {
			ranked = append(ranked, kv{k, v})
		}
	}
	sort.Slice(ranked, func(i, j int) bool { return ranked[i].v > ranked[j].v })
	if len(ranked) > 0 && ranked[0].v >= floor {
		return ranked[0].k
	}
	return ""
}

func jevBase() string {
	if b := os.Getenv("TYPESAFE_BASE_URL"); b != "" {
		return strings.TrimRight(b, "/")
	}
	return "https://api.typesafe.ai"
}

func jevModel() string {
	if m := os.Getenv("JEV_HOOKS_MODEL"); m != "" {
		return m
	}
	return "jev-latest"
}
