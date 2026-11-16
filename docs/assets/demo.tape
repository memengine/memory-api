Output docs/assets/demo.gif

Require python
Set Shell "bash"
Set FontSize 17
Set Padding 30
Set Width 1000
Set Height 600
Set WindowBar ""
Set Theme "Dracula"
Set Framerate 12
Set TypingSpeed 40ms
Env MEMORYOS_DEMO_MOCK "1"

Hide
Type "PS1='$ '; clear"
Enter
Sleep 300ms
Show

Type "python examples/demo/1_write.py"
Sleep 330ms
Enter
Wait+Screen /job status.*completed/
Sleep 1.2s
Ctrl+L

Type "python examples/demo/2_conflict.py"
Sleep 1.17s
Enter
Wait+Screen /operator actions.*apply/
Sleep 1.2s
Ctrl+L

Type "python examples/demo/3_ask_agent.py"
Sleep 1.08s
Enter
Wait+Screen /degraded read.*True/
Sleep 1.2s
Ctrl+L

Type "python examples/demo/4_operator_resolve.py"
Sleep 670ms
Enter
Wait+Screen /audit timestamp/
Sleep 1.2s
Ctrl+L

Type "python examples/demo/5_history.py"
Sleep 1.5s
Enter
Wait+Screen /cites.*evt_crm_002/
Sleep 2.5s
