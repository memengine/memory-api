Output docs/assets/demo.gif

Require python
Set Shell "bash"
Set FontSize 25
Set Width 1440
Set Height 710
Set Theme "Dracula"
Set TypingSpeed 6ms
Env MEMORYOS_DEMO_MOCK "1"

Hide
Type "PS1='$ '"
Enter
Sleep 500ms
Show

Type "python examples/demo/billing_says_growth.py"
Enter
Sleep 2s
Type "python examples/demo/crm_says_scale.py"
Enter
Sleep 2s
Type "python examples/demo/ask_agent.py"
Enter
Sleep 5s
