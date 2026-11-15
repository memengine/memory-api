Output docs/assets/demo.gif

Set Shell "bash"
Set FontSize 25
Set Width 1120
Set Height 620
Set Theme "Dracula"
Set TypingSpeed 6ms

Hide
Type "export MEMORYOS_DEMO_MOCK=1; PS1='$ '"
Enter
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

