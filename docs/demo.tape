# Regenerate with: vhs docs/demo.tape  (from the repo root)
Output docs/demo.gif
Set FontSize 13
Set Width 1100
Set Height 720
Set Padding 14
Set TypingSpeed 30ms

Type "uv run python evals/run_evals.py --list"
Enter
Sleep 4s
Type "clear"
Enter
Type `echo "── Reliability: 5 full passes of all 12 scenarios ──"`
Enter
Sleep 500ms
Type `jq -r '.per_scenario | to_entries[] | "  \(.key)\t\(.value.passed)/\(.value.of)"' evals/results/reliability.json`
Enter
Sleep 3s
Type `jq -r '"  aggregate: \(.aggregate_passed)/\(.aggregate_of) scenario-runs passed"' evals/results/reliability.json`
Enter
Sleep 4s
